"""Tron history: TronGrid rows → Transaction, the block-cursor → timestamp
paging, time ordering for nonce-less rows, and activities from the TRC-20
transfer index."""

from __future__ import annotations

import io
import json
import urllib.parse

import pytest

from qeth.address import tron_from_hex
from qeth.chains import DEFAULT_CHAINS, TRON
from qeth.transactions import Transaction
from qeth.transactions_cache import merge_txs
from qeth.tron.history import TronGridTransactionSource, parse_tron_tx

CHAIN = next(c for c in DEFAULT_CHAINS if c.family == TRON)
OWNER = "0x" + "d1c4bb7b2f39aba5707711719b2236b5b605af2e"
TO = "0x" + "e28b3cfd4e0e909077821478e9fcb86b84be786e"
USDT = "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c"

TRX_ROW = {
    "ret": [{"contractRet": "SUCCESS", "fee": 1100000}],
    "txID": "3DE2A2C2428E9DE45950F80AF75414A35965188E4ED47F2F0863B35143C4B7EF",
    "net_fee": 100000, "energy_fee": 0, "energy_usage_total": 0,
    "blockNumber": 55661323, "block_timestamp": 1697615427000,
    "raw_data": {"contract": [{"type": "TransferContract", "parameter": {"value": {
        "amount": 104565876865762, "owner_address": "41" + OWNER[2:],
        "to_address": "41" + TO[2:]}}}]},
}
USDT_ROW = {
    "ret": [{"contractRet": "SUCCESS", "fee": 0}],
    "txID": "aa" * 32, "net_fee": 345000, "energy_fee": 6428500,
    "energy_usage_total": 64285, "blockNumber": 55661320,
    "block_timestamp": 1697615418000,
    "raw_data": {"contract": [{"type": "TriggerSmartContract", "parameter": {"value": {
        "owner_address": "41" + OWNER[2:], "contract_address": "41" + USDT[2:],
        "data": "a9059cbb" + "00" * 12 + TO[2:] + "00" * 31 + "05"}}}]},
}


def test_trx_transfer_row():
    tx = parse_tron_tx(TRX_ROW, CHAIN.chain_id)
    assert tx.hash == "0x" + TRX_ROW["txID"].lower()
    assert (tx.from_addr, tx.to_addr, tx.value_wei) == (OWNER, TO, 104565876865762)
    assert tx.nonce == -1 and tx.timestamp == 1697615427
    assert tx.fee == 1100000 and tx.method_id == "" and tx.success


def test_trigger_row_keeps_calldata_and_fee():
    tx = parse_tron_tx(USDT_ROW, CHAIN.chain_id)
    assert tx.to_addr == USDT and tx.method_id == "0xa9059cbb"
    assert tx.fee == 345000 + 6428500 and tx.gas_used == 64285


def test_nonce_less_rows_order_by_time():
    older = parse_tron_tx(USDT_ROW, CHAIN.chain_id)
    newer = parse_tron_tx(TRX_ROW, CHAIN.chain_id)
    pending = Transaction(CHAIN.chain_id, "0x" + "bb" * 32, 0, 1790000000, -1,
                          OWNER, TO, 1, 0, 0, "", "0x", True, pending=True)
    merged = merge_txs([older], [pending, newer])
    assert [t.hash for t in merged] == [pending.hash, newer.hash, older.hash]


class FakeIndex:
    def __init__(self, pages):
        self.pages = pages
        self.urls: list[str] = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        if "/wallet/getblock" in url:
            body = json.loads(req.data)
            return io.BytesIO(json.dumps({"blockID": "00" * 32, "block_header": {
                "raw_data": {"number": int(body["id_or_num"]), "timestamp": 1690000000000}}}).encode())
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        page = self.pages[1] if "fingerprint" in q else self.pages[0]
        return io.BytesIO(json.dumps(page).encode())


def test_source_pages_and_maps_the_block_cursor_to_a_timestamp(monkeypatch):
    fake = FakeIndex([
        {"success": True, "data": [TRX_ROW], "meta": {"fingerprint": "fp1"}},
        {"success": True, "data": [USDT_ROW], "meta": {}},
    ])
    monkeypatch.setattr("urllib.request.urlopen", fake)
    src = TronGridTransactionSource()
    first = src.list_transactions(CHAIN, OWNER, limit=50)
    assert [t.block_number for t in first] == [55661323]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(fake.urls[0]).query)
    assert q["only_from"] == ["true"] and q["limit"] == ["50"]
    assert fake.urls[0].startswith(
        f"https://api.trongrid.io/v1/accounts/{tron_from_hex(OWNER)}/transactions?")
    # A cursor at a block we've seen maps to its known time — no header read.
    src.list_transactions(CHAIN, OWNER, before_block=55661323)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(fake.urls[-1]).query)
    assert q["max_timestamp"] == ["1697615427000"]
    # An unseen block costs one header read.
    src.list_transactions(CHAIN, OWNER, before_block=123)
    assert any("/wallet/getblock" in u for u in fake.urls)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(fake.urls[-1]).query)
    assert q["max_timestamp"] == ["1690000000000"]
    # page 2 follows the fingerprint.
    assert [t.block_number for t in src.list_transactions(CHAIN, OWNER, page=2)] == [55661320]


def test_activities_come_from_the_trc20_index(monkeypatch):
    from qeth.tx_activity import fetch_activities
    trc20 = {"success": True, "data": [{
        "transaction_id": USDT_ROW["txID"],
        "token_info": {"symbol": "USDT", "address": tron_from_hex(USDT), "decimals": 6},
        "from": tron_from_hex(OWNER), "to": tron_from_hex(TO), "value": "5"}], "meta": {}}
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: io.BytesIO(json.dumps(trc20).encode()))
    txs = [parse_tron_tx(TRX_ROW, CHAIN.chain_id), parse_tron_tx(USDT_ROW, CHAIN.chain_id)]
    acts = fetch_activities(CHAIN, OWNER, txs)
    send, transfer = acts[txs[0].hash], acts[txs[1].hash]
    assert send.verb == "send" and [(l.symbol, l.contract) for l in send.out] == [("TRX", None)]
    assert transfer.verb == "transfer"
    assert [(l.symbol, l.contract) for l in transfer.out] == [("USDT", USDT)]


@pytest.mark.parametrize("nonces, full", [([2, 1, 0], True), ([-1, -1], False)])
def test_full_history_needs_nonces(nonces, full):
    from qeth.plugins.transactions import _is_full_history
    txs = [Transaction(1, f"0x{i}", 1, 1, n, OWNER, TO, 0, 0, 0, "", "0x", True)
           for i, n in enumerate(nonces)]
    assert _is_full_history(txs) is full


# --- decoding Tron calldata ---------------------------------------------------

DIRTY_TRANSFER = ("0xa9059cbb" + "00" * 11 + "41" + TO[2:]
                  + (5).to_bytes(32, "big").hex())


def test_tron_address_words_are_normalised_for_decoding():
    from qeth.tron.tx import strip_address_prefixes
    clean = strip_address_prefixes(DIRTY_TRANSFER)
    assert clean == "0xa9059cbb" + "00" * 12 + TO[2:] + (5).to_bytes(32, "big").hex()
    assert strip_address_prefixes("0x") == "0x"
    # A plain amount word is left alone.
    amt = "0xa9059cbb" + (0x41 << 100).to_bytes(32, "big").hex()
    assert strip_address_prefixes(amt) == amt


def test_the_standard_signature_beats_a_4byte_collision():
    """a9059cbb is also registered as workMyDirefulOwner(uint256,uint256):
    the standard transfer must win without asking the database."""
    from qeth.abi import decode_via_4byte

    def db(url, timeout):
        raise AssertionError("4byte.directory must not be consulted")
    clean = "0xa9059cbb" + "00" * 12 + TO[2:] + (5).to_bytes(32, "big").hex()
    d = decode_via_4byte(clean, transport=db)
    assert d["function"] == "transfer"
    assert d["args"][0]["value"].lower() == TO


def test_decoded_addresses_render_in_tron_form():
    from qeth.plugins.transactions import _addresses_in_family_form
    tree = {"function": "transfer", "args": [
        {"name": "arg0", "type": "address", "value": TO},
        {"name": "arg1", "type": "uint256", "value": "5"}]}
    out = _addresses_in_family_form(tree, CHAIN)
    assert out["args"][0]["value"] == tron_from_hex(TO)
    assert out["args"][1]["value"] == "5"


def test_no_evm_identity_row_on_tron():
    from qeth.plugins.transactions import _make_identity_row
    label, kick = _make_identity_row(
        to_addr=USDT, chain=CHAIN, identity_source=None, identity_cache=None,
        my_addresses=[], start_worker=lambda w: None)
    assert (label, kick) == (None, None)
