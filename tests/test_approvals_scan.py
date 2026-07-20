"""ScanWorker — Approval-log windowed discovery + recent-tx lag patch.

Drives the worker's run() synchronously (no thread) so emitted signals land in
lists; fetch_allowances + metadata are stubbed so no network/chain is touched.
"""

from types import SimpleNamespace

import qeth.plugins.approvals as ap
from qeth.plugins.approvals import ScanWorker
from qeth.plugins.approvals.discovery import _APPROVAL_TOPIC0
from qeth.transactions import Transaction

CHAIN = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
A = "0x" + "a1" * 20
TOKEN = "0x" + "cc" * 20
SPENDER = "0x" + "ee" * 20


def _approve(spender):
    return "0x095ea7b3" + spender[2:].lower().rjust(64, "0") + "0" * 64


def _tx(nonce, block, data="0x", h=None, frm=A):
    return Transaction(
        chain_id=1, hash=h or ("0x" + format(nonce, "064x")), block_number=block,
        timestamp=block, nonce=nonce, from_addr=frm, to_addr=TOKEN,
        value_wei=0, gas_used=0, gas_price_wei=0,
        method_id=data[:10], input_data=data, success=True)


def _log(token, owner, spender, block):
    return {"address": token, "blockNumber": hex(block),
            "topics": [_APPROVAL_TOPIC0, "0x" + "00" * 12 + owner[2:],
                       "0x" + "00" * 12 + spender[2:], None]}   # Blockscout None-pad


class _FakeLogSource:
    """Returns pre-canned windows of Approval log rows; records from_block."""

    def __init__(self, windows):
        self._windows = list(windows)
        self.from_blocks: list = []

    def fetch(self, chain, address, from_block=0):
        self.from_blocks.append(from_block)
        return self._windows.pop(0) if self._windows else []


class _FakeTxSource:
    def __init__(self, pages=None):
        self._pages = list(pages or [])
        self.cursors: list = []

    def list_transactions(self, chain, address, page=1, limit=100, before_block=None):
        self.cursors.append(before_block)
        return self._pages.pop(0) if self._pages else []


class _FakeClient:
    def __init__(self, chain):
        pass


class _FakeMeta:
    def missing(self, cid, tokens):
        return []

    def get(self, cid, token):
        return {"symbol": "TK", "name": "Tok", "decimals": 18}

    def put_many(self, cid, items):
        pass


def _run(worker):
    got: dict = {"batch": [], "rows": [], "progress": [], "done": [], "head": []}
    worker.batch_fetched.connect(lambda c, a, t: got["batch"].append(list(t)))
    worker.rows_ready.connect(lambda c, a, r: got["rows"].append(list(r)))
    worker.progress.connect(lambda c, a, s, t: got["progress"].append((s, t)))
    worker.scan_done.connect(
        lambda c, a, ok, head: (got["done"].append(ok), got["head"].append(head)))
    worker.run()
    return got


def _worker(log_src, tx_src=None, snapshot=None, *, from_block=0, **kw):
    return ScanWorker(CHAIN, A, log_src, tx_src or _FakeTxSource(),
                      list(snapshot or []), _FakeMeta(),
                      from_block=from_block, client_factory=_FakeClient, **kw)


def _pairs(got):
    return {(r.token.lower(), r.spender.lower())
            for batch in got["rows"] for r in batch}


def test_log_window_discovers_a_pair(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances",
                        lambda client, owner, pairs, **k: dict.fromkeys(pairs, 42))
    log_src = _FakeLogSource([[_log(TOKEN, A, SPENDER, 500)]])   # short window → end
    got = _run(_worker(log_src))
    assert (TOKEN.lower(), SPENDER.lower()) in _pairs(got)
    assert got["done"] == [True]
    assert got["head"] == [500]                    # logs_head = max block seen
    assert any(r.allowance == 42 for b in got["rows"] for r in b)


def test_windows_walk_the_from_block_cursor(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances", lambda *a, **k: {})
    # a full window (1000 rows, all block 100) then a short one ends it
    full = [_log(TOKEN, A, "0x" + "%040x" % i, 100) for i in range(ScanWorker.LOG_PAGE)]
    tail = [_log(TOKEN, A, SPENDER, 150)]
    log_src = _FakeLogSource([full, tail])
    got = _run(_worker(log_src, from_block=7))
    assert log_src.from_blocks[0] == 7             # honours the incremental cursor
    assert log_src.from_blocks[1] == 101           # advanced past the full window's block
    assert got["done"] == [True]
    assert got["head"] == [150]


def test_incremental_from_block_passed_through(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances", lambda *a, **k: {})
    log_src = _FakeLogSource([[]])                  # nothing new since the cursor
    got = _run(_worker(log_src, from_block=999))
    assert log_src.from_blocks == [999]
    assert got["done"] == [True]
    assert got["head"] == [999]                    # cursor unchanged when no rows


def test_log_source_failure_reports_incomplete(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances", lambda *a, **k: {})

    class _Boom:
        def fetch(self, chain, address, from_block=0):
            raise RuntimeError("explorer down")

    got = _run(_worker(_Boom(), from_block=42))
    assert got["done"] == [False]
    assert got["head"] == [42]                     # cursor preserved for resume


def test_recent_tail_patches_the_indexer_gap(monkeypatch):
    # Logs cover up to block 500; a fresh approval at block 600 isn't indexed
    # yet but IS in the account's recent txs → the tail patch must catch it.
    monkeypatch.setattr(ap, "fetch_allowances",
                        lambda client, owner, pairs, **k: dict.fromkeys(pairs, 5))
    new_spender = "0x" + "bb" * 20
    log_src = _FakeLogSource([[_log(TOKEN, A, SPENDER, 500)]])
    tail = [_tx(2, 600, _approve(new_spender), h="0xfresh")]   # newer than logs_head=500
    tx_src = _FakeTxSource([tail])
    got = _run(_worker(log_src, tx_src))
    pairs = _pairs(got)
    assert (TOKEN.lower(), SPENDER.lower()) in pairs          # from logs
    assert (TOKEN.lower(), new_spender.lower()) in pairs      # from the tail patch
    assert [t.hash for t in got["batch"][0]] == ["0xfresh"]   # merged into tx cache


def test_recent_tail_stops_at_logs_head(monkeypatch):
    # The tail walk must NOT descend below logs_head (that region is covered by
    # the log walk) — one page whose oldest block <= head, then stop.
    monkeypatch.setattr(ap, "fetch_allowances",
                        lambda client, owner, pairs, **k: dict.fromkeys(pairs, 1))
    log_src = _FakeLogSource([[_log(TOKEN, A, SPENDER, 500)]])
    page = [_tx(3, 550, h="0x550"), _tx(2, 490, h="0x490")]   # oldest 490 <= head 500
    tx_src = _FakeTxSource([page, [_tx(1, 100, h="0xdeep")]])  # a 2nd page must not be read
    got = _run(_worker(log_src, tx_src))
    assert tx_src.cursors == [None]              # exactly one tail page, then stop
    assert got["done"] == [True]


def test_interruption_stops_before_fetching(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances", lambda *a, **k: {})
    log_src = _FakeLogSource([[_log(TOKEN, A, SPENDER, 100)]])
    w = _worker(log_src)
    monkeypatch.setattr(w, "isInterruptionRequested", lambda: True)
    got = _run(w)
    assert log_src.from_blocks == []             # log loop never ran
    assert got["done"] == [False]


# --- _emit_rows behaviour (source-agnostic: driven off the snapshot pair) ---

class _FakeLabels:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls: list = []

    def fetch_labels(self, cid, addresses):
        self.calls.append((cid, list(addresses)))
        return {a.lower(): self.mapping[a.lower()]
                for a in addresses if a.lower() in self.mapping}


def _snap_worker(monkeypatch, *, allowance, **kw):
    # A snapshot approve produces the candidate pair via the initial _emit_rows,
    # so these exercise row-building without needing any log windows.
    monkeypatch.setattr(ap, "fetch_allowances",
                        lambda client, owner, pairs, **k: dict.fromkeys(pairs, allowance))
    return _worker(_FakeLogSource([]),
                   snapshot=[_tx(0, 100, _approve(SPENDER))], **kw)


def test_spender_labels_populated_and_checksummed(monkeypatch):
    from eth_utils import to_checksum_address
    labels = _FakeLabels({SPENDER.lower(): "Uniswap: Router"})
    got = _run(_snap_worker(monkeypatch, allowance=7, label_source=labels))
    row = got["rows"][0][0]
    assert row.spender_label == "Uniswap: Router"
    assert row.spender == to_checksum_address(SPENDER)
    assert labels.calls


def test_label_source_failure_is_tolerated(monkeypatch):
    class _Boom:
        def fetch_labels(self, cid, addresses):
            raise RuntimeError("metadata service down")

    got = _run(_snap_worker(monkeypatch, allowance=7, label_source=_Boom()))
    assert got["rows"][0][0].spender_label == ""
    assert got["done"] == [True]


class _FakePrices:
    def __init__(self, unit):
        from decimal import Decimal
        self._unit = Decimal(unit)
        self.calls = []

    def fetch(self, chain, contracts, include_native=False):
        from qeth.pricing import Price
        self.calls.append(list(contracts))
        return {t: Price(self._unit, 0, "fake") for t in contracts}


def test_price_source_sets_unit_price_on_finite_rows(monkeypatch):
    from decimal import Decimal
    prices = _FakePrices("2")
    got = _run(_snap_worker(monkeypatch, allowance=5_000_000, price_source=prices))
    assert got["rows"][0][0].price_usd == Decimal("2")
    assert prices.calls == [[TOKEN.lower()]]


def test_unlimited_allowance_is_not_priced(monkeypatch):
    prices = _FakePrices("2")
    got = _run(_snap_worker(monkeypatch, allowance=(1 << 256) - 1, price_source=prices))
    assert got["rows"][0][0].price_usd is None
    assert prices.calls == []


def test_price_source_failure_leaves_rows_unpriced(monkeypatch):
    class _Boom:
        def fetch(self, chain, contracts, include_native=False):
            raise RuntimeError("defillama down")

    got = _run(_snap_worker(monkeypatch, allowance=5_000_000, price_source=_Boom()))
    assert got["rows"][0][0].price_usd is None
    assert got["done"] == [True]
