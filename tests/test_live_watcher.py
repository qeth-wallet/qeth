"""Tests for the LiveWatcher QThread orchestration.

Offline: the single I/O seam ``_stream_heads`` is overridden (or
``ws_urls_for`` patched) so we exercise the real QThread + asyncio loop +
queued Qt signals + shutdown without a network or a ws server. The live ws
path is covered by the async_chain live checks / a manual smoke.
"""

import asyncio

import pytest

import qeth.live_watcher as lw
from qeth.chains import DEFAULT_CHAINS
from qeth.live_watcher import LiveWatcher


def _chain(cid: int):
    return next(c for c in DEFAULT_CHAINS if c.chain_id == cid)


class _FakeStreamWatcher(LiveWatcher):
    """Replaces ws I/O with a synthetic head stream — one block every 20 ms,
    numbered from a per-chain base so a test can tell chains apart."""

    async def _stream_heads(self, chain):  # type: ignore[override]
        self.link_state.emit(chain, True)
        n = chain.chain_id * 1000
        while True:
            await asyncio.sleep(0.02)
            yield n
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
    floors it) and the task idles on backoff rather than spinning, with the
    real _stream_heads (no network — there are no URLs to dial)."""
    monkeypatch.setattr(lw, "ws_urls_for", lambda chain: [])
    downs: list = []
    w = track(LiveWatcher(lambda: [_chain(100)]))
    w.link_state.connect(lambda c, on: downs.append(on))
    w.start()

    qtbot.waitUntil(lambda: bool(downs) and downs[0] is False, timeout=4000)
    w.stop()
    assert w.isFinished()
