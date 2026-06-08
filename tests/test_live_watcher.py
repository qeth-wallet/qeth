"""Tests for the LiveWatcher QThread orchestration + the pending-tx probe.

Offline: the live-I/O seam ``_serve_connection`` is overridden (or
``ws_urls_for`` patched) to exercise the real QThread + asyncio loop + queued
Qt signals + shutdown without a network; the probe logic is tested directly
via ``_probe_one`` against a fake w3. The live ws path is covered by the
async_chain live checks / a manual smoke.
"""

import asyncio

import pytest

import qeth.live_watcher as lw
from qeth.chains import DEFAULT_CHAINS
from qeth.live_watcher import LiveWatcher, PendingTx


def _chain(cid: int):
    return next(c for c in DEFAULT_CHAINS if c.chain_id == cid)


def test_construction_quiets_web3_ws_logging():
    import logging
    lg = logging.getLogger("web3.providers.WebSocketProvider")
    lg.setLevel(logging.INFO)            # pretend something turned it up
    LiveWatcher(lambda: [])              # constructing quiets it
    assert lg.level == logging.WARNING


def test_to_int_normalises_hex_and_int():
    """newHeads block numbers arrive as a hex str from DRPC but an int from
    publicnode (web3's subscription formatting is provider-inconsistent);
    int(hex_str) raised and flapped the connection every block."""
    from qeth.live_watcher import _to_int
    assert _to_int("0x1819da1") == 0x1819DA1
    assert _to_int("0x0") == 0
    assert _to_int(46588696) == 46588696


# --- orchestration (real thread, synthetic connection) --------------------

class _FakeStreamWatcher(LiveWatcher):
    """Replaces the ws connection with a synthetic head stream — one block
    every 20 ms, numbered from a per-chain base so a test can tell chains
    apart. Loops until cancelled/stopped, like a live connection."""

    async def _serve_connection(self, chain, account=None):  # type: ignore[override]
        self.link_state.emit(chain, True)
        n = chain.chain_id * 1000
        while not self._stopping.is_set():
            await asyncio.sleep(0.02)
            self.head.emit(chain, n)
            n += 1


@pytest.fixture
def track(qtbot):
    """Track watchers and guarantee they're stopped on teardown, so a failed
    assertion can't leak a running QThread (which aborts the process)."""
    created: list[LiveWatcher] = []

    def _track(w: LiveWatcher) -> LiveWatcher:
        created.append(w)
        return w

    yield _track
    for w in created:
        w.stop()


def test_streams_heads_and_shuts_down_clean(track, qtbot):
    chain = _chain(100)
    heads: list = []
    links: list = []
    w = track(_FakeStreamWatcher(lambda: [chain]))
    w.head.connect(lambda c, n: heads.append((c.chain_id, n)))
    w.link_state.connect(lambda c, up: links.append(up))
    w.start()

    qtbot.waitUntil(lambda: len(heads) >= 3, timeout=4000)
    assert heads[0] == (100, 100_000)                      # base = chain_id*1000
    assert [n for _, n in heads[:3]] == [100_000, 100_001, 100_002]
    assert links and links[0] is True

    w.stop()
    assert w.isFinished()
    assert not w.isRunning()


def test_supervisor_reconciles_chain_set(track, qtbot):
    """Changing what the provider returns starts/stops subscriptions to
    match — the loop's reconcile step."""
    desired = {"chains": [_chain(100)]}
    up: set = set()
    w = track(_FakeStreamWatcher(lambda: list(desired["chains"])))
    w.link_state.connect(lambda c, on: up.add(c.chain_id) if on else None)
    w.start()

    qtbot.waitUntil(lambda: 100 in up, timeout=4000)       # Gnosis watched
    desired["chains"] = [_chain(1)]                         # switch to Ethereum
    qtbot.waitUntil(lambda: 1 in up, timeout=4000)          # picked up the change

    w.stop()
    assert w.isFinished()


def test_no_ws_urls_emits_down_and_idles(track, qtbot, monkeypatch):
    """A chain with no ws endpoint reports link down (the legacy timer
    floors it) and the connection raises -> backoff idle, with the real
    _serve_connection (no network — there are no URLs to dial)."""
    monkeypatch.setattr(lw, "ws_urls_for", lambda chain: [])
    downs: list = []
    w = track(LiveWatcher(lambda: [_chain(100)]))
    w.link_state.connect(lambda c, on: downs.append(on))
    w.start()

    qtbot.waitUntil(lambda: bool(downs) and downs[0] is False, timeout=4000)
    w.stop()
    assert w.isFinished()


# --- the pending-tx probe (fake w3, no thread) ----------------------------

class _FakeProvider:
    def __init__(self, results: dict):
        self._results = results          # method -> result value
        self.sent: list = []             # raw txs passed to sendRawTransaction

    async def make_request(self, method, params):
        if method == "eth_sendRawTransaction":
            self.sent.append(params[0])
            return {"result": "0xbroadcast"}
        return {"result": self._results.get(method)}


class _FakeW3:
    def __init__(self, provider):
        self.provider = provider


def _watcher():
    return LiveWatcher(lambda: [])       # no provider needed for direct probe


def test_probe_one_confirms_on_receipt(qapp):
    w = _watcher()
    got: list = []
    w.confirmed.connect(lambda c, h, r: got.append((h, r)))
    receipt = {"transactionHash": "0xabc", "status": "0x1"}
    w3 = _FakeW3(_FakeProvider({"eth_getTransactionReceipt": receipt}))
    tx = PendingTx("0xabc", "0xfrom", 5, None)

    asyncio.run(w._probe_one(_chain(100), tx, w3))
    assert got == [("0xabc", receipt)]


def test_probe_one_drops_when_nonce_consumed(qapp):
    w = _watcher()
    drops: list = []
    w.dropped.connect(lambda c, h: drops.append(h))
    w3 = _FakeW3(_FakeProvider({
        "eth_getTransactionReceipt": None,    # not mined
        "eth_getTransactionCount": "0x6",     # latest 6 > tx nonce 5 -> replaced
    }))
    tx = PendingTx("0xabc", "0xfrom", 5, "0xraw")

    asyncio.run(w._probe_one(_chain(100), tx, w3))
    assert drops == ["0xabc"]
    assert w3.provider.sent == []           # dropped, so no re-broadcast


def test_probe_one_rebroadcasts_when_still_open(qapp):
    w = _watcher()
    prov = _FakeProvider({
        "eth_getTransactionReceipt": None,
        "eth_getTransactionCount": "0x5",     # latest == tx nonce -> still open
    })
    tx = PendingTx("0xabc", "0xfrom", 5, "0xraw")

    asyncio.run(w._probe_one(_chain(100), tx, _FakeW3(prov)))
    assert prov.sent == ["0xraw"]


def test_probe_one_rebroadcast_is_capped(qapp):
    w = _watcher()
    prov = _FakeProvider({
        "eth_getTransactionReceipt": None,
        "eth_getTransactionCount": "0x5",
    })
    w3 = _FakeW3(prov)
    tx = PendingTx("0xabc", "0xfrom", 5, "0xraw")

    for _ in range(LiveWatcher.REBROADCAST_MAX_ATTEMPTS + 3):
        asyncio.run(w._probe_one(_chain(100), tx, w3))
    assert len(prov.sent) == LiveWatcher.REBROADCAST_MAX_ATTEMPTS


# --- Transfer-log subscription (Phase 2) ----------------------------------

def test_transfer_filters_topics():
    from qeth.live_watcher import TRANSFER_TOPIC0
    acct = "0x" + "ab" * 20
    padded = "0x" + "00" * 12 + "ab" * 20
    incoming, outgoing = LiveWatcher._transfer_filters(acct)
    assert incoming == [TRANSFER_TOPIC0, None, padded]   # to = account
    assert outgoing == [TRANSFER_TOPIC0, padded, None]   # from = account


def test_handle_log_emits_balance_dirty(qapp):
    w = _watcher()
    got: list = []
    w.balance_dirty.connect(lambda c, acct, tok: got.append((acct, tok)))
    chain = _chain(100)
    w._handle_log(chain, "0xacc", {"address": "0xTok", "removed": False})
    # reorg-removed log re-reads too — we never trust the log's value
    w._handle_log(chain, "0xacc", {"address": "0xTok", "removed": True})
    assert got == [("0xacc", "0xTok"), ("0xacc", "0xTok")]
