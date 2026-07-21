"""ScanWorker — Approval-log windowed discovery + recent-tx lag patch.

Drives the worker's run() synchronously (no thread) so emitted signals land in
lists; fetch_allowances + metadata are stubbed so no network/chain is touched.
"""

from types import SimpleNamespace

import pytest

import qeth.plugins.approvals as ap
from qeth.plugins.approvals import ScanWorker
from qeth.plugins.approvals.discovery import _APPROVAL_TOPIC0
from qeth.transactions import Transaction

CHAIN = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
A = "0x" + "a1" * 20
TOKEN = "0x" + "cc" * 20
SPENDER = "0x" + "ee" * 20


@pytest.fixture(autouse=True)
def _no_softname_network(monkeypatch):
    # The soft-label residual path calls Blockscout — keep the suite hermetic by
    # default; the dedicated soft-label tests override this with their own stub.
    monkeypatch.setattr(ap, "fetch_contract_display_name", lambda *a, **k: "")


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
    HEAD = 1000
    BALANCES: dict = {}          # token_lower -> balance, for the at-risk tag

    def __init__(self, chain):
        pass

    def get_block_number(self):
        return self.HEAD

    def multicall_erc20_balances(self, tokens, holder, **k):
        return {t.lower(): self.BALANCES[t.lower()]
                for t in tokens if t.lower() in self.BALANCES}

    # ERC-20 name/symbol probe for spender soft-labels; {} = "no spender is a
    # token" (a test opts in by setting ERC20_META).
    ERC20_META: dict = {}

    def multicall_erc20_metadata(self, tokens, **k):
        return {t.lower(): self.ERC20_META[t.lower()]
                for t in tokens if t.lower() in self.ERC20_META}


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


def test_row_carries_token_balance_for_the_at_risk_tag(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances",
                        lambda client, owner, pairs, **k: (dict.fromkeys(pairs, 5), set(pairs)))

    class _WithBalance(_FakeClient):
        BALANCES = {TOKEN.lower(): 7_000_000}

    w = ScanWorker(CHAIN, A, _FakeLogSource([[_log(TOKEN, A, SPENDER, 100)]]),
                   _FakeTxSource(), [], _FakeMeta(), client_factory=_WithBalance)
    got = _run(w)
    row = next(r for b in got["rows"] for r in b)
    assert row.token_balance == 7_000_000            # wallet balance read + attached


def test_balance_read_failure_is_tolerated(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances",
                        lambda client, owner, pairs, **k: (dict.fromkeys(pairs, 5), set(pairs)))

    class _Boom(_FakeClient):
        def multicall_erc20_balances(self, tokens, holder, **k):
            raise RuntimeError("rpc down")

    w = ScanWorker(CHAIN, A, _FakeLogSource([[_log(TOKEN, A, SPENDER, 100)]]),
                   _FakeTxSource(), [], _FakeMeta(), client_factory=_Boom)
    got = _run(w)
    row = next(r for b in got["rows"] for r in b)
    assert row.token_balance == 0                    # unknown, no crash
    assert got["done"] == [True]


def test_log_window_discovers_a_pair(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances",
                        lambda client, owner, pairs, **k: (dict.fromkeys(pairs, 42), set(pairs)))
    log_src = _FakeLogSource([[_log(TOKEN, A, SPENDER, 500)]])   # short window → end
    got = _run(_worker(log_src))
    assert (TOKEN.lower(), SPENDER.lower()) in _pairs(got)
    assert got["done"] == [True]
    assert got["head"] == [500]                    # logs_head = max block seen
    assert any(r.allowance == 42 for b in got["rows"] for r in b)


def test_windows_walk_the_from_block_cursor(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances", lambda *a, **k: ({}, set()))
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
    monkeypatch.setattr(ap, "fetch_allowances", lambda *a, **k: ({}, set()))
    log_src = _FakeLogSource([[]])                  # nothing new since the cursor
    got = _run(_worker(log_src, from_block=999))
    assert log_src.from_blocks == [999]
    assert got["done"] == [True]
    assert got["head"] == [999]                    # cursor unchanged when no rows


def test_log_source_failure_reports_incomplete(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances", lambda *a, **k: ({}, set()))

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
                        lambda client, owner, pairs, **k: (dict.fromkeys(pairs, 5), set(pairs)))
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
                        lambda client, owner, pairs, **k: (dict.fromkeys(pairs, 1), set(pairs)))
    log_src = _FakeLogSource([[_log(TOKEN, A, SPENDER, 500)]])
    page = [_tx(3, 550, h="0x550"), _tx(2, 490, h="0x490")]   # oldest 490 <= head 500
    tx_src = _FakeTxSource([page, [_tx(1, 100, h="0xdeep")]])  # a 2nd page must not be read
    got = _run(_worker(log_src, tx_src))
    assert tx_src.cursors == [None]              # exactly one tail page, then stop
    assert got["done"] == [True]


def test_progress_two_phase_discovery_then_verification(monkeypatch):
    # Discovery (log windows) fills 0.._DISCOVERY_FRAC via block-%; verification
    # fills the rest via checked/total. head=1000; a full window at block 500
    # then a short one at 900 discover 2 pairs, then both verify.
    monkeypatch.setattr(ap, "fetch_allowances",
                        lambda client, owner, pairs, **k: (dict.fromkeys(pairs, 1), set(pairs)))
    D = int(100 * ScanWorker._DISCOVERY_FRAC)
    full = [_log(TOKEN, A, "0x" + "%040x" % i, 500) for i in range(ScanWorker.LOG_PAGE)]
    tail = [_log(TOKEN, A, SPENDER, 900)]
    got = _run(_worker(_FakeLogSource([full, tail])))
    pcts = [s for (s, t) in got["progress"] if t == 100]
    assert pcts == sorted(pcts)                    # monotonic
    # discovery block-% is scaled under D: block 500/1000 → D*0.5, 900 → D*0.9
    assert int(D * 0.5) in pcts
    assert max(pcts) == 100                         # verification reaches 100%
    assert all(p <= 100 for p in pcts)
    assert any(D <= p < 100 for p in pcts)          # a mid-verification step


def test_progress_indeterminate_during_discovery_when_head_unknown(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances",
                        lambda client, owner, pairs, **k: (dict.fromkeys(pairs, 1), set(pairs)))

    class _NoHead(_FakeClient):
        def get_block_number(self):
            raise RuntimeError("rpc down")

    w = ScanWorker(CHAIN, A, _FakeLogSource([[_log(TOKEN, A, SPENDER, 500)]]),
                   _FakeTxSource(), [], _FakeMeta(), client_factory=_NoHead)
    got = _run(w)
    # Discovery emits busy (total 0) with head unknown; verification is
    # count-based so it still shows determinate progress up to 100%.
    assert any(t == 0 for (_s, t) in got["progress"])          # busy during discovery
    assert max(s for (s, t) in got["progress"] if t == 100) == 100
    assert got["done"] == [True]


def test_interruption_stops_before_fetching(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances", lambda *a, **k: ({}, set()))
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
                        lambda client, owner, pairs, **k: (dict.fromkeys(pairs, allowance), set(pairs)))
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


# --- ReconcileWorker: omit a failed read (no false removal) -----------------

def test_reconcile_omits_a_pair_whose_read_failed(monkeypatch):
    from qeth.plugins.approvals import ReconcileWorker
    kept = (TOKEN.lower(), SPENDER.lower())
    gone = (TOKEN.lower(), ("0x" + "22" * 20).lower())
    failed = (TOKEN.lower(), ("0x" + "33" * 20).lower())
    # found = {kept: 9}, read = {kept, gone} (gone read as zero; failed absent).
    monkeypatch.setattr(ap, "fetch_allowances",
                        lambda client, owner, pairs, **k: ({kept: 9}, {kept, gone}))
    w = ReconcileWorker(CHAIN, A, [kept, gone, failed], client_factory=_FakeClient)
    out: list = []
    w.reconciled.connect(lambda c, a, values: out.append(values))
    w.run()
    values = out[0]
    assert values[kept] == 9            # positive → updated
    assert values[gone] == 0            # definitively zero → removed
    assert failed not in values         # read failed → OMITTED (leaf left as-is)
