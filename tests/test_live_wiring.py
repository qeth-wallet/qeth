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

    plugin._on_transfer_seen(gnosis, "0xabc", "0xtok", "0xcp", False, 7)
    tokens.on_transfer_seen.assert_called_once_with(
        gnosis, "0xabc", "0xtok", "0xcp", False, 7)


def _chain_ns(chain_id=1, symbol="ETH", name="Ethereum"):
    return SimpleNamespace(chain_id=chain_id, symbol=symbol, name=name)


def test_on_transfer_seen_notifies_with_symbol_and_amount(qtbot, monkeypatch):
    """A known token formats amount via its decimals; an unknown token
    degrades to an amount-less 'a token' rather than a wrong number."""
    from qeth.plugins.tokens import TokensPlugin
    tp = TokensPlugin(Mock())
    tp.host = Mock()
    monkeypatch.setattr(
        tp._token_metadata, "get",
        lambda cid, c: {"symbol": "USDC", "decimals": 6}
        if c == "0xtok" else None)

    # known token: 2_500_000 / 10**6 = 2.5 USDC, received
    tp.on_transfer_seen(_chain_ns(), "0xme", "0xtok", "0xfrom", False, 2_500_000)
    title, body, icon = tp.host.notify.call_args.args
    assert title == "Received 2.5 USDC"           # glyph-free; direction is the icon
    assert "0xfrom" in body
    from PySide6.QtGui import QIcon
    assert isinstance(icon, QIcon) and not icon.isNull()

    # unknown token: no decimals → no quantity
    tp.host.notify.reset_mock()
    tp.on_transfer_seen(_chain_ns(), "0xme", "0xunk", "0xto", True, 999)
    assert tp.host.notify.call_args.args[0] == "Sent a token"


def test_on_native_balance_notifies_received_on_increase(qtbot, monkeypatch):
    from qeth.plugins.tokens import TokensPlugin
    tp = TokensPlugin(Mock())
    tp.host = Mock()
    tp._displayed_view = (1, "0xme")
    monkeypatch.setattr(tp, "_on_balance_refresh", lambda *a: None)
    monkeypatch.setattr(tp, "_touch_cached_native", lambda *a: None)
    ch = _chain_ns()

    # first sight seeds baseline — no notification
    tp.on_native_balance(ch, "0xme", 10 * 10**18)
    assert tp.host.notify.call_count == 0

    # increase by 2 ETH → notify received
    tp.on_native_balance(ch, "0xme", 12 * 10**18)
    assert tp.host.notify.call_args.args[0] == "Received 2 ETH"

    # decrease (our own send/gas) → no received notification
    tp.host.notify.reset_mock()
    tp.on_native_balance(ch, "0xme", 11 * 10**18)
    assert tp.host.notify.call_count == 0


def test_maybe_notify_native_sent(qtbot):
    """A confirmed native send of ours notifies; zero-value calls and reverts
    don't."""
    plugin = TransactionsPlugin(disk_cache=Mock())
    plugin.host = Mock()
    ch = _chain_ns(symbol="xDAI", name="Gnosis")

    sent = SimpleNamespace(value_wei=3 * 10**18, success=True, to_addr="0xdest")
    plugin._maybe_notify_native_sent(ch, sent)
    title, body, icon = plugin.host.notify.call_args.args
    assert title == "Sent 3 xDAI"
    assert "Gnosis" in body
    from PySide6.QtGui import QIcon
    assert isinstance(icon, QIcon) and not icon.isNull()

    plugin.host.notify.reset_mock()
    plugin._maybe_notify_native_sent(
        ch, SimpleNamespace(value_wei=0, success=True, to_addr="0xc"))
    plugin._maybe_notify_native_sent(
        ch, SimpleNamespace(value_wei=5, success=False, to_addr="0xc"))
    assert plugin.host.notify.call_count == 0


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
