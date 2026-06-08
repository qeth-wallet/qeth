"""Plugin-side wiring for the ws live watcher (qeth.plugins.transactions).

Offline: the pending snapshot builder is pure, and the attach tests run with
an empty cache so the watcher starts but dials nothing (no chains to watch).
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qeth.live_watcher import PendingTx
from qeth.plugins.transactions import (TransactionsPlugin,
                                        _build_pending_snapshot)


def _tx(h, pending, nonce=0):
    return SimpleNamespace(hash=h, from_addr="0xabc", nonce=nonce,
                           raw_signed=None, pending=pending)


def test_build_pending_snapshot_groups_pending_only():
    cache = {
        (100, "0xa"): [_tx("0x1", True), _tx("0x2", False)],   # one pending
        (100, "0xb"): [_tx("0x3", True)],                       # diff account
        (1, "0xa"): [_tx("0x4", False)],                        # all confirmed
    }
    chains = {100: SimpleNamespace(chain_id=100, name="Gnosis"),
              1: SimpleNamespace(chain_id=1, name="Ethereum")}

    snap = _build_pending_snapshot(cache, lambda cid: chains.get(cid))

    assert set(snap) == {100}                  # chain 1 had no pending tx
    chain, pend = snap[100]
    assert chain.name == "Gnosis"
    assert sorted(p.hash for p in pend) == ["0x1", "0x3"]   # merged accounts
    assert all(isinstance(p, PendingTx) for p in pend)


def test_build_pending_snapshot_skips_unknown_chain():
    cache = {(999, "0xa"): [_tx("0x1", True)]}
    snap = _build_pending_snapshot(cache, lambda cid: None)   # host can't resolve
    assert snap == {}


def test_attach_creates_live_watcher_when_flagged(qtbot, monkeypatch):
    monkeypatch.setenv("QETH_LIVE_WS", "1")
    plugin = TransactionsPlugin(disk_cache=Mock())
    plugin.attach(Mock())
    try:
        assert plugin._live_watcher is not None
        qtbot.waitUntil(lambda: plugin._live_watcher.isRunning(), timeout=2000)
        # empty cache -> nothing to watch -> stays a clean idle thread
        assert plugin._live_pending_provider(100) == []
    finally:
        if plugin._live_watcher is not None:
            plugin._live_watcher.stop()
    assert plugin._live_watcher.isFinished()


def test_attach_skips_live_watcher_without_flag(qtbot, monkeypatch):
    monkeypatch.delenv("QETH_LIVE_WS", raising=False)
    plugin = TransactionsPlugin(disk_cache=Mock())
    plugin.attach(Mock())
    assert plugin._live_watcher is None
    # the rebuild is a cheap no-op with the watcher off
    plugin._rebuild_live_snapshot()
    assert plugin._live_snapshot == {}
