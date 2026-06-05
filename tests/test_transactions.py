"""Hermetic tests for qeth.transactions — parser + source dispatch.

Network goes through an injected transport callable, so these tests
never hit Blockscout. See ``test_network_transactions.py`` for the
live integration test.
"""

import json

import pytest

from qeth.chains import Chain, DEFAULT_CHAINS
from qeth.transactions import (
    BlockscoutTransactionSource,
    Transaction,
    TransactionSourceError,
    TxDirection,
    UnsupportedChain,
    _parse_blockscout_tx,
)


ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
ADDR = "0x7a16ff8270133f063aab6c9977183d9e72835428"


# --- _parse_blockscout_tx -------------------------------------------------

# Real shape from `?module=account&action=txlist`, one ERC-20 transfer row.
SAMPLE_ROW = {
    "blockNumber": "25164561",
    "timeStamp": "1779618611",
    "hash": "0xec3decdbe0cfc1d2ec1c67899f77b861d060a785d931b240429f0b36be6e62d2",
    "nonce": "17180",
    "from": "0x7a16ff8270133f063aab6c9977183d9e72835428",
    "to": "0xdac17f958d2ee523a2206206994597c13d831ec7",
    "value": "0",
    "gas": "64031",
    "gasPrice": "103828909",
    "gasUsed": "63197",
    "input": "0xa9059cbb0000000000000000000000005d6a4ba137d77df7c3cdd7131c430da5497c7ace000000000000000000000000000000000000000000000000000000001dcd6500",
    "methodId": "0xa9059cbb",
    "isError": "0",
    "txreceipt_status": "1",
}


class TestParseBlockscoutTx:
    def test_full_row(self):
        tx = _parse_blockscout_tx(SAMPLE_ROW, chain_id=1)
        assert tx is not None
        assert tx.chain_id == 1
        assert tx.hash.startswith("0xec3decd")
        assert tx.block_number == 25164561
        assert tx.timestamp == 1779618611
        assert tx.nonce == 17180
        assert tx.from_addr == ADDR
        assert tx.to_addr == "0xdac17f958d2ee523a2206206994597c13d831ec7"
        assert tx.value_wei == 0
        assert tx.gas_used == 63197
        assert tx.gas_price_wei == 103828909
        assert tx.method_id == "0xa9059cbb"
        assert tx.success is True

    def test_addresses_are_lowercased(self):
        row = {**SAMPLE_ROW,
               "from": "0xABCDEF0000000000000000000000000000000001",
               "to":   "0xABCDEF0000000000000000000000000000000002"}
        tx = _parse_blockscout_tx(row, chain_id=1)
        assert tx.from_addr == "0xabcdef0000000000000000000000000000000001"
        assert tx.to_addr == "0xabcdef0000000000000000000000000000000002"

    def test_contract_creation_has_to_none(self):
        row = {**SAMPLE_ROW, "to": "", "input": "0x6080604052..."}
        tx = _parse_blockscout_tx(row, chain_id=1)
        assert tx.to_addr is None

    def test_failed_tx(self):
        row = {**SAMPLE_ROW, "txreceipt_status": "0", "isError": "1"}
        tx = _parse_blockscout_tx(row, chain_id=1)
        assert tx.success is False

    def test_method_id_derived_from_input_when_field_missing(self):
        row = {**SAMPLE_ROW, "methodId": "",
               "input": "0x23b872dd000000000000000000000000aaaa"}
        tx = _parse_blockscout_tx(row, chain_id=1)
        assert tx.method_id == "0x23b872dd"

    def test_plain_native_transfer_has_empty_method_id(self):
        row = {**SAMPLE_ROW, "methodId": "", "input": "0x", "value": "1000"}
        tx = _parse_blockscout_tx(row, chain_id=1)
        assert tx.method_id == ""
        assert tx.value_wei == 1000

    def test_missing_status_assumes_success(self):
        """Some Blockscout instances omit txreceipt_status on ancient txs.
        We fall back to isError, defaulting to success when both absent."""
        row = {**SAMPLE_ROW}
        row.pop("txreceipt_status")
        row.pop("isError")
        tx = _parse_blockscout_tx(row, chain_id=1)
        assert tx.success is True

    def test_huge_value_survives(self):
        """Native amount can exceed JS Number range; we keep it as int."""
        big = 10**25  # 10M ETH worth of wei
        row = {**SAMPLE_ROW, "value": str(big)}
        tx = _parse_blockscout_tx(row, chain_id=1)
        assert tx.value_wei == big

    def test_garbage_row_returns_none(self):
        # No hash → unparseable, drop the row rather than raising
        assert _parse_blockscout_tx({"foo": "bar"}, chain_id=1) is None

    def test_bad_integers_return_none(self):
        row = {**SAMPLE_ROW, "blockNumber": "not-a-number"}
        assert _parse_blockscout_tx(row, chain_id=1) is None


# --- Transaction.direction ------------------------------------------------

class TestDirection:
    def _tx(self, **kw):
        defaults = dict(
            chain_id=1, hash="0x", block_number=1, timestamp=0,
            nonce=0, from_addr="", to_addr=None, value_wei=0,
            gas_used=0, gas_price_wei=0, method_id="", input_data="0x",
            success=True,
        )
        defaults.update(kw)
        return Transaction(**defaults)

    def test_sent(self):
        tx = self._tx(from_addr=ADDR, to_addr="0xbeef")
        assert tx.direction(ADDR) == TxDirection.SENT

    def test_received(self):
        tx = self._tx(from_addr="0xbeef", to_addr=ADDR)
        assert tx.direction(ADDR) == TxDirection.RECEIVED

    def test_self_transfer(self):
        tx = self._tx(from_addr=ADDR, to_addr=ADDR)
        assert tx.direction(ADDR) == TxDirection.SELF

    def test_case_insensitive(self):
        tx = self._tx(from_addr=ADDR, to_addr="0xbeef")
        assert tx.direction(ADDR.upper()) == TxDirection.SENT

    def test_contract_creation_treated_as_sent(self):
        # to_addr=None; if we're the from, it's still SENT.
        tx = self._tx(from_addr=ADDR, to_addr=None)
        assert tx.direction(ADDR) == TxDirection.SENT

    def test_unrelated(self):
        tx = self._tx(from_addr="0xaaa", to_addr="0xbbb")
        assert tx.direction(ADDR) == TxDirection.UNRELATED


# --- BlockscoutTransactionSource via injected transport -------------------

def _fake_transport(payload: dict, captured_urls: list[str]):
    def transport(url: str, timeout: float) -> bytes:
        captured_urls.append(url)
        return json.dumps(payload).encode()
    return transport


class TestBlockscoutSource:
    def test_happy_path(self):
        urls = []
        src = BlockscoutTransactionSource(
            transport=_fake_transport(
                {"status": "1", "message": "OK", "result": [SAMPLE_ROW]},
                urls,
            ),
        )
        out = src.list_transactions(ETH, ADDR, limit=1)
        assert len(out) == 1
        assert out[0].hash.startswith("0xec3decd")
        # URL should hit the eth.blockscout.com instance with desc sort.
        assert len(urls) == 1
        assert "eth.blockscout.com" in urls[0]
        assert "sort=desc" in urls[0]
        assert "offset=1" in urls[0]
        assert "endblock=" not in urls[0]   # no pagination cursor

    def test_page_index_passes_through(self):
        urls = []
        src = BlockscoutTransactionSource(
            transport=_fake_transport({"status": "1", "result": []}, urls),
        )
        src.list_transactions(ETH, ADDR, page=3, limit=10)
        # Walks page-by-page; the URL must carry the page index that
        # the worker asked for.
        assert "page=3" in urls[0]
        assert "offset=10" in urls[0]

    def test_no_transactions_is_empty_not_error(self):
        urls = []
        src = BlockscoutTransactionSource(
            transport=_fake_transport(
                {"status": "0", "message": "No transactions found", "result": []},
                urls,
            ),
        )
        assert src.list_transactions(ETH, ADDR) == []

    def test_non_ok_status_raises(self):
        urls = []
        src = BlockscoutTransactionSource(
            transport=_fake_transport(
                {"status": "0", "message": "Something broke"},
                urls,
            ),
        )
        with pytest.raises(TransactionSourceError):
            src.list_transactions(ETH, ADDR)

    def test_page_window_cap_is_end_not_error(self):
        # The explorer's `page × offset ≤ 10000` cap: hitting it on a deep
        # scroll is the end of pageable history, not a failure. The detail
        # comes back in `result` (a string), message is just "NOTOK".
        src = BlockscoutTransactionSource(
            transport=_fake_transport(
                {"status": "0", "message": "NOTOK",
                 "result": "Result window is too large, PageNo x Offset "
                           "size must be less than or equal to 10000"},
                [],
            ),
        )
        assert src.list_transactions(ETH, ADDR, page=300, limit=50) == []

    def test_unsupported_chain_raises(self):
        src = BlockscoutTransactionSource()
        fake = Chain(name="Fake", chain_id=999999, rpc_url="https://x")
        with pytest.raises(UnsupportedChain):
            src.list_transactions(fake, ADDR)

    def test_supports_check(self):
        src = BlockscoutTransactionSource()
        assert src.supports(ETH)
        fake = Chain(name="Fake", chain_id=999999, rpc_url="https://x")
        assert not src.supports(fake)

    def test_bad_row_skipped_not_fatal(self):
        urls = []
        src = BlockscoutTransactionSource(
            transport=_fake_transport(
                {"status": "1", "result": [
                    {"junk": "row"},          # unparseable
                    SAMPLE_ROW,                # good
                ]},
                urls,
            ),
        )
        out = src.list_transactions(ETH, ADDR)
        assert len(out) == 1
        assert out[0].hash.startswith("0xec3decd")

    def test_custom_instances_override(self):
        urls = []
        src = BlockscoutTransactionSource(
            instances={1: "https://my-blockscout.example"},
            transport=_fake_transport({"status": "1", "result": []}, urls),
        )
        src.list_transactions(ETH, ADDR)
        assert urls[0].startswith("https://my-blockscout.example/api?")
