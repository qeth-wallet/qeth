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
    _parse_blockscout_v2_tx,
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


# --- BlockscoutTransactionSource (REST v2) via injected transport ---------

# Real shape from `/api/v2/addresses/{addr}/transactions?filter=from`, trimmed
# to the fields the parser reads (plus a nested `from`/`to` object as served).
SAMPLE_V2 = {
    "hash": "0xb4dcb99402c07b16b853f174f9f66c43a6c8048ed1838837c9aaca81b2b37610",
    "block_number": 25966921,
    "timestamp": "2026-09-13T07:09:23.000000Z",
    "nonce": 18148,
    "from": {"hash": "0x7a16fF8270133F063aAb6C9977183D9e72835428",
             "is_contract": False},
    "to": {"hash": "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E",
           "is_contract": True},
    "value": "0",
    "gas_used": "46318",
    "gas_price": "43360454",
    "method": "transfer",
    "raw_input": "0xa9059cbb000000000000000000000000b325c1ac788f02ff7997cf53c6ff40dd762897b30000000000000000000000000000000000000000000000b46b5d13682724d2c8",
    "status": "ok",
    "result": "success",
}


def _v2_row(i: int, **over) -> dict:
    return {**SAMPLE_V2, "hash": "0x" + format(i, "064x"),
            "block_number": 1_000_000 - i, "nonce": 5000 - i, **over}


def _v2_pages(pages: list[list[dict]], captured_urls: list[str]):
    """Serve ``pages`` in order, chaining them with a ``next_page_params``
    keyset cursor (the last page carries ``null``) — as Blockscout does."""
    def transport(url: str, timeout: float) -> bytes:
        n = len(captured_urls)
        captured_urls.append(url)
        items = pages[n]
        nxt = ({"block_number": items[-1]["block_number"], "index": 7,
                "filter": "from", "items_count": 50 * (n + 1)}
               if n + 1 < len(pages) else None)
        return json.dumps({"items": items, "next_page_params": nxt}).encode()
    return transport


class TestParseBlockscoutV2Tx:
    def test_full_row(self):
        tx = _parse_blockscout_v2_tx(SAMPLE_V2, chain_id=1)
        assert tx is not None
        assert tx.hash.startswith("0xb4dcb994")
        assert tx.block_number == 25966921
        assert tx.timestamp == 1789283363
        assert tx.nonce == 18148
        assert tx.from_addr == ADDR                      # lower-cased
        assert tx.to_addr == "0xf939e0a03fb07f59a73314e73794be0e57ac1b4e"
        assert tx.value_wei == 0
        assert tx.gas_used == 46318
        assert tx.gas_price_wei == 43360454
        assert tx.method_id == "0xa9059cbb"
        assert tx.success is True and tx.pending is False

    def test_reverted(self):
        tx = _parse_blockscout_v2_tx({**SAMPLE_V2, "status": "error"}, 1)
        assert tx is not None and tx.success is False

    def test_contract_creation_has_no_to(self):
        tx = _parse_blockscout_v2_tx(
            {**SAMPLE_V2, "to": None, "raw_input": "0x6080604052"}, 1)
        assert tx is not None and tx.to_addr is None

    def test_plain_send_has_no_method(self):
        tx = _parse_blockscout_v2_tx(
            {**SAMPLE_V2, "raw_input": "0x", "value": "1000"}, 1)
        assert tx is not None
        assert tx.method_id == "" and tx.value_wei == 1000

    def test_pending_row_skipped(self):
        # No block yet: our own broadcast already has a local pending row.
        assert _parse_blockscout_v2_tx(
            {**SAMPLE_V2, "block_number": None, "status": None}, 1) is None

    def test_junk_row_skipped(self):
        assert _parse_blockscout_v2_tx({"junk": "row"}, 1) is None


class TestBlockscoutSource:
    def test_happy_path_hits_v2_sent_filter(self):
        urls: list[str] = []
        src = BlockscoutTransactionSource(
            transport=_v2_pages([[SAMPLE_V2]], urls))
        out = src.list_transactions(ETH, ADDR, limit=1)
        assert [t.hash for t in out] == [SAMPLE_V2["hash"]]
        assert len(urls) == 1
        assert urls[0].startswith(
            f"https://eth.blockscout.com/api/v2/addresses/{ADDR}/transactions?")
        assert "filter=from" in urls[0]
        assert "module=" not in urls[0]          # never the v1 /api
        assert "block_number=" not in urls[0]    # no cursor

    def test_limit_walks_keyset_pages(self):
        # v2 pages are a fixed 50 rows: a 100-row page takes two requests,
        # the second carrying the first's next_page_params verbatim.
        urls: list[str] = []
        pages = [[_v2_row(i) for i in range(50)],
                 [_v2_row(i) for i in range(50, 100)],
                 [_v2_row(i) for i in range(100, 150)]]
        src = BlockscoutTransactionSource(transport=_v2_pages(pages, urls))
        out = src.list_transactions(ETH, ADDR, limit=100)
        assert [t.nonce for t in out] == [5000 - i for i in range(100)]
        assert len(urls) == 2                    # stops once the page is full
        assert f"block_number={pages[0][-1]['block_number']}" in urls[1]
        assert "items_count=50" in urls[1]

    def test_short_history_stops_at_null_cursor(self):
        urls: list[str] = []
        src = BlockscoutTransactionSource(
            transport=_v2_pages([[_v2_row(i) for i in range(3)]], urls))
        assert len(src.list_transactions(ETH, ADDR, limit=100)) == 3
        assert len(urls) == 1

    def test_page_index_skips_earlier_rows(self):
        urls: list[str] = []
        pages = [[_v2_row(i) for i in range(50)]]
        src = BlockscoutTransactionSource(transport=_v2_pages(pages, urls))
        out = src.list_transactions(ETH, ADDR, page=3, limit=10)
        assert [t.nonce for t in out] == [5000 - i for i in range(20, 30)]

    def test_pending_rows_do_not_shorten_the_page(self):
        # A pending row is skipped, but the page still fills to `limit` — a
        # short page would read as "end of history" and stop the older-walk.
        urls: list[str] = []
        first = [_v2_row(0, block_number=None)] + [_v2_row(i) for i in range(1, 50)]
        pages = [first, [_v2_row(i) for i in range(50, 100)]]
        src = BlockscoutTransactionSource(transport=_v2_pages(pages, urls))
        out = src.list_transactions(ETH, ADDR, limit=50)
        assert len(out) == 50 and len(urls) == 2

    def test_before_block_is_inclusive_keyset_cursor(self):
        urls: list[str] = []
        src = BlockscoutTransactionSource(transport=_v2_pages([[]], urls))
        src.list_transactions(ETH, ADDR, before_block=15_000_000)
        # (block N+1, index 0) excludes nothing at block N — v1 endblock=N.
        assert "block_number=15000001" in urls[0]
        assert "index=0" in urls[0]

    def test_no_transactions_is_empty_not_error(self):
        src = BlockscoutTransactionSource(transport=_v2_pages([[]], []))
        assert src.list_transactions(ETH, ADDR) == []

    def test_error_body_raises(self):
        def transport(url, timeout):
            return json.dumps({"message": "Invalid address hash"}).encode()
        src = BlockscoutTransactionSource(transport=transport)
        with pytest.raises(TransactionSourceError, match="Invalid address"):
            src.list_transactions(ETH, ADDR)

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
        src = BlockscoutTransactionSource(
            transport=_v2_pages([[{"junk": "row"}, SAMPLE_V2]], []))
        out = src.list_transactions(ETH, ADDR)
        assert [t.hash for t in out] == [SAMPLE_V2["hash"]]

    def test_custom_instances_override(self):
        urls: list[str] = []
        src = BlockscoutTransactionSource(
            instances={1: "https://my-blockscout.example"},
            transport=_v2_pages([[]], urls))
        src.list_transactions(ETH, ADDR)
        assert urls[0].startswith("https://my-blockscout.example/api/v2/")
