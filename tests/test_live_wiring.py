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


def test_attach_enables_live_watcher_by_default(qtbot, monkeypatch):
    monkeypatch.delenv("QETH_LIVE_WS", raising=False)   # no env -> on by default
    plugin = TransactionsPlugin(disk_cache=Mock())
    host = Mock()
    host.current_chain = lambda: None       # nothing on screen -> watches nothing
    host.selected_address = None
    plugin.attach(host)
    try:
        assert plugin._live_watcher is not None
        qtbot.waitUntil(lambda: plugin._live_watcher.isRunning(), timeout=2000)
    finally:
        if plugin._live_watcher is not None:
            plugin._live_watcher.stop()


def test_attach_skips_live_watcher_without_flag(qtbot, monkeypatch):
    monkeypatch.setenv("QETH_LIVE_WS", "0")   # explicit opt-out (on by default)
    plugin = TransactionsPlugin(disk_cache=Mock())
    plugin.attach(Mock())
    assert plugin._live_watcher is None
    # the rebuild is a cheap no-op with the watcher off
    plugin._rebuild_live_snapshot()
    assert plugin._live_snapshot == {}


# --- Phase 2 consumer wiring ----------------------------------------------

def test_update_live_account_and_balance_dirty_relay(qtbot, monkeypatch):
    """The on-screen (chain, account) snapshot tracks the view, and
    balance_dirty relays to TokensPlugin.on_balance_dirty (no real watcher —
    we stand a Mock in so no ws connection opens)."""
    monkeypatch.setenv("QETH_LIVE_WS", "0")   # explicit opt-out (on by default)
    gnosis = SimpleNamespace(chain_id=100)
    plugin = TransactionsPlugin(disk_cache=Mock())
    tokens = Mock()
    host = Mock()
    host.current_chain = lambda: gnosis
    host.selected_address = "0xABC"
    host.tokens_plugin = tokens
    plugin.attach(host)
    plugin._live_watcher = Mock()        # pretend it's on, but never connects

    plugin._update_live_account()
    assert plugin._live_account == (gnosis, "0xabc")
    assert plugin._live_account_provider() == (gnosis, "0xabc")

    plugin._on_balance_dirty(gnosis, "0xabc", "0xToken")
    tokens.on_balance_dirty.assert_called_once_with(gnosis, "0xabc", "0xToken")

    plugin._on_native_balance(gnosis, "0xabc", 5 * 10**18)
    tokens.on_native_balance.assert_called_once_with(gnosis, "0xabc", 5 * 10**18)


def test_tokens_on_native_balance_applies_only_for_current_view(qtbot, monkeypatch):
    """on_native_balance applies a lightweight native-only refresh + cache
    touch, but only when it's the on-screen view (the inbound-ETH path)."""
    from types import SimpleNamespace
    from qeth.plugins.tokens import TokensPlugin
    tp = TokensPlugin(Mock())
    tp._displayed_view = (100, "0xabc")
    applied: list = []
    touched: list = []
    monkeypatch.setattr(
        tp, "_on_balance_refresh",
        lambda cid, wei, bals: applied.append((cid, wei, bals)))
    monkeypatch.setattr(
        tp, "_touch_cached_native",
        lambda cid, addr, wei: touched.append((cid, addr, wei)))

    # off-view: ignored
    tp.on_native_balance(SimpleNamespace(chain_id=137), "0xABC", 7)
    assert applied == [] and touched == []

    # on-view: native-only apply (empty token map) + cache touch
    tp.on_native_balance(SimpleNamespace(chain_id=100), "0xABC", 9 * 10**18)
    assert applied == [(100, 9 * 10**18, {})]
    assert touched == [(100, "0xABC", 9 * 10**18)]


def test_touch_cached_native_updates_only_native(qtbot, tmp_path):
    """_touch_cached_native persists the new native balance while leaving the
    cached tokens intact (so the slow sweep + cross-session reopen stay sane)."""
    from qeth.plugins.tokens import TokensPlugin
    from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache
    tp = TokensPlugin(Mock())
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp._wallet_cache.save(CachedWallet(
        chain_id=100, address="0xabc", native_balance_wei=1,
        tokens=[CachedToken(contract="0xtok", symbol="T", name="Tok",
                            decimals=18, balance_raw=42)]))

    tp._touch_cached_native(100, "0xABC", 5 * 10**18)

    reloaded = tp._wallet_cache.load(100, "0xabc")
    assert reloaded is not None
    assert reloaded.native_balance_wei == 5 * 10**18
    assert [(t.contract, t.balance_raw) for t in reloaded.tokens] == [("0xtok", 42)]

    # no-op when unchanged (load returns same value; nothing re-saved is fine)
    tp._touch_cached_native(100, "0xabc", 5 * 10**18)
    assert tp._wallet_cache.load(100, "0xabc").native_balance_wei == 5 * 10**18

    # missing cache → silently ignored (no crash, nothing written)
    tp._touch_cached_native(100, "0xdef", 3)
    assert tp._wallet_cache.load(100, "0xdef") is None


def test_tokens_on_balance_dirty_throttles_for_current_view(qtbot, monkeypatch):
    from types import SimpleNamespace
    from qeth.plugins.tokens import TokensPlugin
    tp = TokensPlugin(Mock())
    tp._displayed_view = (100, "0xabc")
    calls: list = []
    monkeypatch.setattr(tp, "_refresh", lambda a: calls.append(a))

    # off-view: ignored, no timer armed
    tp.on_balance_dirty(SimpleNamespace(chain_id=137), "0xABC", "0xtok")
    assert tp._live_refresh_timer is None

    # on-view: throttle armed; a second dirty doesn't restart it
    tp.on_balance_dirty(SimpleNamespace(chain_id=100), "0xABC", "0xtok")
    assert tp._live_refresh_timer is not None and tp._live_refresh_timer.isActive()
    tp.on_balance_dirty(SimpleNamespace(chain_id=100), "0xABC", "0xtok2")
    assert tp._live_refresh_timer.isActive()

    # firing re-reads the authoritative balances for the view
    tp._on_live_refresh()
    assert calls == ["0xABC"]


def test_tokens_sweep_slows_when_current_chain_ws_live(qtbot):
    from PySide6.QtCore import QTimer
    from qeth.plugins.tokens import TokensPlugin
    tp = TokensPlugin(Mock())
    tp._refresh_timer = QTimer()
    tp._refresh_timer.setInterval(TokensPlugin.REFRESH_INTERVAL_MS)
    tp._displayed_view = (100, "0xabc")

    tp.on_ws_link_state(SimpleNamespace(chain_id=100), True)    # current chain live
    assert tp._refresh_timer.interval() == TokensPlugin.SLOW_REFRESH_INTERVAL_MS
    tp.on_ws_link_state(SimpleNamespace(chain_id=100), False)   # dropped -> floor
    assert tp._refresh_timer.interval() == TokensPlugin.REFRESH_INTERVAL_MS
    tp.on_ws_link_state(SimpleNamespace(chain_id=137), True)    # other chain -> no change
    assert tp._refresh_timer.interval() == TokensPlugin.REFRESH_INTERVAL_MS
