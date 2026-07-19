"""ScanWorker — progressive paging + cursor walk + interruption (commit 1).

Drives the worker's run() synchronously (no thread) so emitted signals land in
lists; fetch_allowances + metadata are stubbed so no network/chain is touched.
"""

from types import SimpleNamespace

import qeth.plugins.approvals as ap
from qeth.plugins.approvals import ScanWorker
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


class _FakeSource:
    def __init__(self, pages):
        self._pages = list(pages)
        self.cursors: list = []

    def list_transactions(self, chain, address, page=1, limit=100, before_block=None):
        self.cursors.append(before_block)
        return self._pages.pop(0) if self._pages else []


class _FakeClient:
    def __init__(self, chain):
        pass

    def get_transaction_count(self, address, block):
        return 5


class _FakeMeta:
    def missing(self, cid, tokens):
        return []

    def get(self, cid, token):
        return {"symbol": "TK", "name": "Tok", "decimals": 18}

    def put_many(self, cid, items):
        pass


def _run(worker):
    got: dict = {"batch": [], "rows": [], "progress": [], "done": []}
    worker.batch_fetched.connect(lambda c, a, t: got["batch"].append(list(t)))
    worker.rows_ready.connect(lambda c, a, r: got["rows"].append(list(r)))
    worker.progress.connect(lambda c, a, s, t: got["progress"].append((s, t)))
    worker.scan_done.connect(lambda c, a, ok: got["done"].append(ok))
    worker.run()
    return got


def _worker(src, snapshot):
    return ScanWorker(CHAIN, A, src, snapshot, _FakeMeta(),
                      client_factory=_FakeClient)


def test_full_history_snapshot_skips_all_fetching(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances",
                        lambda client, owner, pairs, **k: dict.fromkeys(pairs, 999))
    monkeypatch.setattr(ap, "_is_full_history", lambda txs: True)
    src = _FakeSource([])
    snap = [_tx(0, 100, _approve(SPENDER))]
    got = _run(_worker(src, snap))
    assert src.cursors == []                     # never hit the network
    assert got["done"] == [True]
    assert got["rows"] and got["rows"][0][0].spender.lower() == SPENDER.lower()


def test_pages_walk_the_before_block_cursor(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances", lambda *a, **k: {})
    monkeypatch.setattr(ap, "_is_full_history", lambda txs: False)
    page1 = [_tx(i, 200 - i, h="0x" + format(i, "064x")) for i in range(100)]  # full page
    page2 = [_tx(200 + i, 50 - i, h="0x" + format(200 + i, "064x")) for i in range(3)]
    src = _FakeSource([page1, page2])
    got = _run(_worker(src, []))
    assert src.cursors[0] is None                # newest first
    assert src.cursors[1] == 101                 # oldest block of page1 (200-99)
    assert len(got["batch"]) == 2                # both pages had new rows
    assert got["done"] == [True]                 # short 2nd page = history start


def test_batch_carries_only_new_rows(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances", lambda *a, **k: {})
    monkeypatch.setattr(ap, "_is_full_history", lambda txs: False)
    dup = _tx(0, 100, h="0xdup")
    page1 = [dup, _tx(1, 99, h="0xnew")]         # short page (len 2 < 100)
    src = _FakeSource([page1])
    got = _run(_worker(src, [dup]))              # dup already in snapshot
    assert len(got["batch"]) == 1
    assert [t.hash for t in got["batch"][0]] == ["0xnew"]


def test_interruption_stops_before_fetching(monkeypatch):
    monkeypatch.setattr(ap, "fetch_allowances", lambda *a, **k: {})
    monkeypatch.setattr(ap, "_is_full_history", lambda txs: False)
    src = _FakeSource([[_tx(0, 100)]])
    w = _worker(src, [])
    monkeypatch.setattr(w, "isInterruptionRequested", lambda: True)
    got = _run(w)
    assert src.cursors == []                     # loop body never ran
    assert got["done"] == [False]                # reported incomplete


def test_discovered_approve_pair_becomes_a_row(monkeypatch):
    seen = {}

    def _fake_allowances(client, owner, pairs, **k):
        seen["pairs"] = set(pairs)
        return dict.fromkeys(pairs, 42)

    monkeypatch.setattr(ap, "fetch_allowances", _fake_allowances)
    monkeypatch.setattr(ap, "_is_full_history", lambda txs: False)
    page1 = [_tx(0, 100, _approve(SPENDER), h="0xapprove")]   # short page
    src = _FakeSource([page1])
    got = _run(_worker(src, []))
    assert (TOKEN.lower(), SPENDER.lower()) in seen["pairs"]
    rows = [r for batch in got["rows"] for r in batch]
    assert any(r.token == TOKEN.lower() and r.allowance == 42 for r in rows)
