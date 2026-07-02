"""Plugin-side wiring for the ws live watcher (qeth.plugins.transactions).

Offline: the pending snapshot builder is pure, and the attach tests run with
an empty cache so the watcher starts but dials nothing (no chains to watch).
"""

from types import SimpleNamespace
from unittest.mock import Mock


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

    plugin._on_balance_dirty(gnosis, "0xabc", "0xToken", 123, 10**18, 5)
    tokens.on_balance_dirty.assert_called_once_with(
        gnosis, "0xabc", "0xToken", 123, 10**18, 5)

    plugin._on_native_balance(gnosis, "0xabc", 5 * 10**18, 42)
    tokens.on_native_balance.assert_called_once_with(gnosis, "0xabc", 5 * 10**18, 42)

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

    # unknown token: no decimals → no quantity (Mock store makes everything
    # "recognised", so the scam filter is bypassed here — see the dedicated
    # filtering test below for the real-store behaviour)
    tp.host.notify.reset_mock()
    tp.on_transfer_seen(_chain_ns(), "0xme", "0xunk", "0xto", True, 999)
    assert tp.host.notify.call_args.args[0] == "Sent a token"


def test_on_transfer_seen_filters_scam_and_zero(qtbot, tmp_qeth, monkeypatch):
    """No notification for the spam that dominates these logs: unrecognised
    tokens (address poisoning) and zero-value transfers (a transferFrom-0
    emits a Transfer event but moves nothing). A recognised token with real
    value still notifies."""
    from qeth.plugins.tokens import TokensPlugin
    from qeth.store import Store
    tp = TokensPlugin(Store())            # real store → is_custom_token works
    tp.host = Mock()
    monkeypatch.setattr(tp._token_metadata, "get",
                        lambda cid, c: {"symbol": "TOK", "decimals": 18})
    ch = _chain_ns()

    # unrecognised token (in no list, not added by the user) → skipped
    tp.on_transfer_seen(ch, "0xme", "0xscam", "0xpoison", False, 10**18)
    assert tp.host.notify.call_count == 0

    # recognise it (custom-add) but zero value → still skipped
    tp._store.add_custom_token(1, "0xtok")
    tp.on_transfer_seen(ch, "0xme", "0xtok", "0xfrom", False, 0)
    assert tp.host.notify.call_count == 0

    # recognised + non-zero → notifies
    tp.on_transfer_seen(ch, "0xme", "0xtok", "0xfrom", False, 5 * 10**18)
    assert tp.host.notify.call_count == 1


def test_on_native_balance_notifies_received_on_increase(qtbot, monkeypatch):
    from qeth.plugins.tokens import TokensPlugin
    tp = TokensPlugin(Mock())
    tp.host = Mock()
    tp._displayed_view = (1, "0xme")
    monkeypatch.setattr(tp, "_on_balance_refresh", lambda *a: None)
    monkeypatch.setattr(tp._ledger, "apply_native", lambda *a, **k: False)
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
    """on_native_balance applies a lightweight native-only refresh + ordered
    cache write, but only when it's the on-screen view (the inbound-ETH path)."""
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
        tp._ledger, "apply_native",
        lambda chain, acct, wei, block:
            touched.append((chain.chain_id, acct, wei, block)) or False)

    # off-view: ignored
    tp.on_native_balance(SimpleNamespace(chain_id=137), "0xABC", 7, 5)
    assert applied == [] and touched == []

    # on-view: native-only apply (empty token map) + ordered cache write
    tp.on_native_balance(SimpleNamespace(chain_id=100), "0xABC", 9 * 10**18, 5)
    assert applied == [(100, 9 * 10**18, {})]
    assert touched == [(100, "0xABC", 9 * 10**18, 5)]


def test_on_balance_dirty_targeted_runs_off_view(qtbot):
    """A ws Transfer queues a targeted balanceOf re-read for exactly the named
    token even when it's NOT the on-screen view — so switching to the Tokens
    tab after a (browser) tx confirms is instant rather than waiting on the
    slow sweep. Firing fans out one cheap BalanceWorker over the dirtied set."""
    from types import SimpleNamespace
    from qeth.plugins.tokens import BalanceWorker, TokensPlugin
    tp = TokensPlugin(Mock())
    tp.host = Mock()
    tp._displayed_view = (1, "0xother")        # not the dirtied account

    ch = SimpleNamespace(chain_id=100)
    tp.on_balance_dirty(ch, "0xABC", "0xToK")
    # queued under the lowercased key, token lowercased; account case preserved
    assert tp._dirty_balances[(100, "0xabc")][1] == "0xABC"
    assert tp._dirty_balances[(100, "0xabc")][2] == {"0xtok"}

    # a second leg folds into the same slot (one round-trip per burst)
    tp.on_balance_dirty(ch, "0xABC", "0xToK2")
    assert tp._dirty_balances[(100, "0xabc")][2] == {"0xtok", "0xtok2"}

    tp._on_targeted_balance()
    assert tp._dirty_balances == {}
    worker = tp.host.start_worker.call_args.args[0]
    assert isinstance(worker, BalanceWorker)
    assert worker.address == "0xABC"
    assert sorted(worker.contracts) == ["0xtok", "0xtok2"]


def test_apply_targeted_balances_persists_then_renders_on_view(qtbot, monkeypatch):
    """The authoritative result is persisted for any account (so an off-view tx
    is ready on tab switch), and the on-screen view is re-rendered from that
    cache. Off-view instead flags the view for the next tab activation."""
    from types import SimpleNamespace
    from qeth.plugins.tokens import TokensPlugin
    tp = TokensPlugin(Mock())
    rerendered: list = []
    persisted: list = []
    monkeypatch.setattr(
        tp, "_rerender_view_from_cache",
        lambda ch, acct: rerendered.append(acct))
    monkeypatch.setattr(
        tp, "_persist_targeted_balances",
        lambda ch, acct, wei, bals, block=None: persisted.append((acct, bals)))
    ch = SimpleNamespace(chain_id=100)

    # on/off-view is decided from the HOST's selection, not _displayed_view.
    tp.host = SimpleNamespace(
        selected_address="0xOTHER", current_chain=lambda: ch)   # off-view
    tp._apply_targeted_balances(ch, "0xABC", 9, {"0xToK": 7})
    assert persisted == [("0xABC", {"0xtok": 7})]
    assert rerendered == []
    assert (100, "0xabc") in tp._pending_rerender

    tp.host = SimpleNamespace(
        selected_address="0xABC", current_chain=lambda: ch)     # on-view
    tp._pending_rerender.clear()
    tp._apply_targeted_balances(ch, "0xABC", 9, {"0xToK": 7})
    assert rerendered == ["0xABC"]
    assert tp._pending_rerender == set()


def test_on_activated_rerenders_flag_and_reconciles_balances(qtbot, monkeypatch,
                                                             tmp_path):
    """Switching to the Tokens tab (1) re-renders a flagged background update
    from cache, and (2) ALWAYS reconciles the displayed balances against the
    chain via one multicall — the safety net for a confirmation we never got a
    ws event for (the real bug: a fully-sent token lingered on tab switch)."""
    from types import SimpleNamespace
    from qeth.plugins.tokens import BalanceWorker, TokensPlugin
    from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache
    tp = TokensPlugin(Mock())
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp._wallet_cache.save(CachedWallet(
        chain_id=100, address="0xabc", native_balance_wei=1,
        tokens=[CachedToken(contract="0xtok", symbol="T", name="Tok",
                            decimals=18, balance_raw=5)]))
    workers: list = []
    tp.host = SimpleNamespace(
        selected_address="0xABC",
        current_chain=lambda: SimpleNamespace(chain_id=100),
        start_worker=workers.append)
    tp._panel = object()
    rerendered: list = []
    monkeypatch.setattr(
        tp, "_rerender_view_from_cache",
        lambda ch, acct: rerendered.append(acct))

    # not flagged → no cache re-render, but STILL reconciles (kicks a worker)
    tp.on_activated()
    assert rerendered == []
    assert len(workers) == 1
    assert isinstance(workers[0], BalanceWorker)
    assert workers[0].contracts == ["0xtok"]            # over the displayed set

    # flagged (an off-view tx persisted to cache) → also re-render from cache
    workers.clear()
    tp._pending_rerender.add((100, "0xabc"))
    tp.on_activated()
    assert rerendered == ["0xABC"]
    assert tp._pending_rerender == set()                 # consumed
    assert len(workers) == 1                             # and reconciles too


def test_rerender_view_from_cache_handles_hidden_and_usd(qtbot, tmp_path):
    """End-to-end: a real panel + cache. The in-place path updates a held
    token's balance AND recomputes its USD even with a user-hidden token in the
    cache (the case that used to silently no-op), and a re-render keeps USD."""
    from types import SimpleNamespace
    from PySide6.QtCore import Qt
    from qeth.plugins.tokens import TokenListPanel, TokensPlugin
    from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache
    from qeth.icons import IconCache
    from qeth.store import Store
    eth = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
    acc = "0xabc0000000000000000000000000000000000001"
    tok = "0x" + "11" * 20
    hid = "0x" + "22" * 20
    store = Store.load()
    store.hide_token(1, hid)
    panel = TokenListPanel(IconCache(), store)
    qtbot.addWidget(panel)
    tp = TokensPlugin(store)
    tp._panel = panel
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp.host = SimpleNamespace(selected_address=acc, current_chain=lambda: eth)

    cached = CachedWallet(
        chain_id=1, address=acc, native_balance_wei=2 * 10**18,
        native_price_usd="1000", native_price_updated=1,
        tokens=[
            CachedToken(contract=tok, symbol="TKN", name="T", decimals=18,
                        balance_raw=100 * 10**18, price_usd="1.0", price_updated=1),
            CachedToken(contract=hid, symbol="HID", name="H", decimals=18,
                        balance_raw=9 * 10**18, price_usd="1.0", price_updated=1),
        ])
    tp._wallet_cache.save(cached)
    panel.show_cached(eth, tp._filter_hidden_from_cache(eth, cached))
    tp._displayed_view = (1, acc)

    # Token sent: balance 100 -> 50, persisted; re-render the on-screen view.
    tp._persist_targeted_balances(eth, acc, 2 * 10**18, {tok: 50 * 10**18})
    tp._rerender_view_from_cache(eth, acc)

    def cell(addr, col):
        for r in range(panel.table.rowCount()):
            it = panel.table.item(r, 0)
            if it and it.data(Qt.ItemDataRole.UserRole) and \
                    it.data(Qt.ItemDataRole.UserRole)[1] == addr:
                c = panel.table.item(r, col)
                return c.text() if c else None
        return None

    assert cell(tok, 1) == "50"            # balance updated despite hidden token
    assert cell(tok, 2) == "$50.00"        # USD recomputed, not stale/blank


def test_on_live_refresh_reconciles_displayed_balances(qtbot, monkeypatch, tmp_path):
    """On the on-screen view, the live-refresh (≈1.5 s after a ws event) must
    kick a fresh balance reconcile over the displayed set — the later read that
    catches a token the eager 400 ms targeted read missed (RPC a block behind).
    Without it 'be on the Tokens tab when the tx confirms' left a sent token
    listed until the slow sweep, while 'switch tabs after' worked."""
    from types import SimpleNamespace
    from qeth.plugins.tokens import BalanceWorker, TokensPlugin
    from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache
    tp = TokensPlugin(Mock())
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp._wallet_cache.save(CachedWallet(
        chain_id=100, address="0xabc", native_balance_wei=1,
        tokens=[CachedToken(contract="0xtok", symbol="T", name="Tok",
                            decimals=18, balance_raw=5)]))
    workers: list = []
    tp.host = SimpleNamespace(
        selected_address="0xABC",
        current_chain=lambda: SimpleNamespace(chain_id=100),
        start_worker=workers.append)
    monkeypatch.setattr(tp, "_refresh", lambda a: None)   # isolate the reconcile
    tp._displayed_view = (100, "0xabc")
    tp._live_refresh_addr = "0xABC"

    tp._on_live_refresh()
    assert len(workers) == 1
    assert isinstance(workers[0], BalanceWorker)
    assert workers[0].contracts == ["0xtok"]              # over the displayed set


def test_targeted_drop_repaints_via_host_view_not_stale_displayed_view(qtbot, tmp_path):
    """The qeth-send confirm path (_invalidate_view_and_refresh) transiently
    resets _displayed_view to None. A targeted drop landing in that window must
    STILL repaint the on-screen panel — the decision uses the host's selection,
    not the stale _displayed_view. Otherwise the cache empties but the row stays
    until a tab switch (the reported 'sent WBTC, didn't disappear' bug)."""
    from types import SimpleNamespace
    from PySide6.QtCore import Qt
    from qeth.plugins.tokens import TokenListPanel, TokensPlugin
    from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache
    from qeth.icons import IconCache
    from qeth.store import Store
    eth = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
    acc = "0xabc0000000000000000000000000000000000001"
    wbtc = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
    store = Store.load()
    panel = TokenListPanel(IconCache(), store)
    qtbot.addWidget(panel)
    tp = TokensPlugin(store)
    tp._panel = panel
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp.host = SimpleNamespace(selected_address=acc, current_chain=lambda: eth)
    cached = CachedWallet(
        chain_id=1, address=acc, native_balance_wei=10**18,
        native_price_usd="2000", native_price_updated=1, tokens=[
            CachedToken(contract=wbtc, symbol="WBTC", name="W", decimals=8,
                        balance_raw=15450, price_usd="60000", price_updated=1)])
    tp._wallet_cache.save(cached)
    panel.show_cached(eth, cached)
    tp._displayed_view = (1, acc)

    def visible():
        return {panel.table.item(r, 0).data(Qt.ItemDataRole.UserRole)[1]
                for r in range(panel.table.rowCount())
                if panel.table.item(r, 0)
                and panel.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
                and panel.table.item(r, 0).data(Qt.ItemDataRole.UserRole)[1]
                and not panel.table.isRowHidden(r)}
    assert wbtc in visible()

    tp._displayed_view = None              # the _invalidate_view_and_refresh window
    tp._apply_targeted_balances(eth, acc, 10**18, {wbtc: 0}, 100)

    assert wbtc not in visible()           # repainted from the host view
    assert tp._pending_rerender == set()   # treated as on-view, no deferral
    assert not tp._wallet_cache.load(1, acc).tokens


def test_ws_reconnect_clears_freshness_floors(qtbot):
    """A ws (re)connect clears the chain's ledger floors: while the socket was
    down we were blind and a reorg may have rewound the chain, so a stamp from
    before the gap must not order out the fresh reads that follow."""
    from types import SimpleNamespace
    from qeth.plugins.tokens import TokensPlugin
    tp = TokensPlugin(Mock())
    ch = SimpleNamespace(chain_id=1)
    tp.on_ws_link_state(ch, True)                       # initial connect
    tp._ledger.stamp_token(1, "0xabc", "0xtok", 100)
    assert tp._ledger.is_token_stale(1, "0xabc", "0xtok", 50)   # 50 < 100
    tp.on_ws_link_state(ch, False)                      # ws drops
    tp.on_ws_link_state(ch, True)                       # reconnect → cleared
    assert not tp._ledger.is_token_stale(1, "0xabc", "0xtok", 50)


def test_discovery_keeps_hidden_held_tokens_in_cache(qtbot, tmp_path):
    """A held token the user HID must stay in the disk cache after a discovery
    (so unhiding brings it back) even though the display filters it — discovery's
    replace-save dropped it, contradicting _filter_hidden_from_cache."""
    from types import SimpleNamespace
    from decimal import Decimal
    from qeth.plugins.tokens import TokenListPanel, TokensPlugin
    from qeth.wallet_cache import CachedWallet, WalletCache
    from qeth.icons import IconCache
    from qeth.prices import Price
    from qeth.store import Store
    eth = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
    acc = "0xabc0000000000000000000000000000000000001"
    tok = "0x1111111111111111111111111111111111111111"
    store = Store.load()
    store.hide_token(1, tok)                       # the user hid it
    panel = TokenListPanel(IconCache(), store)
    qtbot.addWidget(panel)
    tp = TokensPlugin(store)
    tp._panel = panel
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp._token_metadata.put_many(
        1, {tok: {"symbol": "HID", "name": "Hidden", "decimals": 18}})
    tp.host = SimpleNamespace(selected_address=acc, current_chain=lambda: eth,
                              start_worker=lambda w: None)
    tp._wallet_cache.save(CachedWallet(chain_id=1, address=acc,
                                       native_balance_wei=10**18, tokens=[]))
    panel.show_cached(eth, tp._wallet_cache.load(1, acc))
    tp._displayed_view = (1, acc)

    pv = {"chain": eth, "address": acc, "view_key": (1, acc),
          "native_wei": 10**18, "block": 100, "read_failed": False,
          "balances_raw": {tok: 5000},
          "metadata": {tok: ("HID", "Hidden", 18)}}
    tp._on_combined_ready(pv, 1, {"": Price(Decimal("2000"), 1, "x"),
                                  tok: Price(Decimal("1"), 1, "x")})

    # filtered from the display …
    assert (1, tok) not in panel._balances
    # … but kept in the disk cache, so unhiding brings it back
    held = {t.contract.lower() for t in tp._wallet_cache.load(1, acc).tokens}
    assert tok in held


def test_discovery_merges_and_is_block_ordered(qtbot, tmp_path):
    """The systemic bug: a token claimed on-view was DROPPED when a concurrent
    discovery — whose balance snapshot predated the claim, or whose multicall
    failed — completed and rebuilt the view from its own stale/absent read.
    Discovery now MERGES + is per-token block-ordered: a read older than the
    token's recorded block is ignored, a FAILED read applies nothing, and only
    an authoritative zero at a fresh block drops the token."""
    from types import SimpleNamespace
    from decimal import Decimal
    from qeth.plugins.tokens import TokenListPanel, TokensPlugin
    from qeth.wallet_cache import CachedWallet, WalletCache
    from qeth.icons import IconCache
    from qeth.prices import Price
    from qeth.store import Store
    eth = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
    acc = "0xabc0000000000000000000000000000000000001"
    tok = "0x1111111111111111111111111111111111111111"
    store = Store.load()
    store.add_custom_token(1, tok)                 # custom → shows if non-zero
    panel = TokenListPanel(IconCache(), store)
    qtbot.addWidget(panel)
    tp = TokensPlugin(store)
    tp._panel = panel
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp._token_metadata.put_many(
        1, {tok: {"symbol": "CUS", "name": "Custom", "decimals": 18}})
    tp.host = SimpleNamespace(selected_address=acc, current_chain=lambda: eth,
                              start_worker=lambda w: None)
    tp._wallet_cache.save(CachedWallet(chain_id=1, address=acc,
                                       native_balance_wei=10**18, tokens=[]))
    panel.show_cached(eth, tp._wallet_cache.load(1, acc))
    tp._displayed_view = (1, acc)

    def held():
        return {t.contract.lower() for t in tp._wallet_cache.load(1, acc).tokens}

    def discover(balances, block=None, read_failed=False):
        pv = {"chain": eth, "address": acc, "view_key": (1, acc),
              "native_wei": 10**18, "block": block, "read_failed": read_failed,
              "balances_raw": balances,
              "metadata": {tok: ("CUS", "Custom", 18)}}
        tp._on_combined_ready(pv, 1, {"": Price(Decimal("2000"), 1, "x")})

    # claim the custom token at block 100
    tp._apply_targeted_balances(eth, acc, 10**18, {tok: 5000}, 100)
    assert tok in held()

    # a stale discovery read it 0 at block 99 (before the claim) → NOT dropped
    discover({tok: 0}, block=99)
    assert tok in held()

    # a failed discovery (Blockscout fallback, no fresh read) → NOT dropped
    discover({}, read_failed=True)
    assert tok in held()

    # a FRESH discovery reads it 0 at block 101 → authoritative drop
    discover({tok: 0}, block=101)
    assert tok not in held()


def test_stale_discovery_native_does_not_regress(qtbot, tmp_path):
    """2c: discovery's native is block-ordered like every other native write —
    a stale discovery read (older block, behind an LB) must not regress it, and
    a fresh read applies + stamps the block."""
    from types import SimpleNamespace
    from decimal import Decimal
    from qeth.plugins.tokens import TokenListPanel, TokensPlugin
    from qeth.wallet_cache import CachedWallet, WalletCache
    from qeth.icons import IconCache
    from qeth.prices import Price
    from qeth.store import Store
    eth = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
    acc = "0xabc0000000000000000000000000000000000001"
    store = Store.load()
    panel = TokenListPanel(IconCache(), store)
    qtbot.addWidget(panel)
    tp = TokensPlugin(store)
    tp._panel = panel
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp.host = SimpleNamespace(selected_address=acc, current_chain=lambda: eth,
                              start_worker=lambda w: None)
    tp._wallet_cache.save(CachedWallet(chain_id=1, address=acc,
                                       native_balance_wei=10**18, tokens=[]))
    tp._displayed_view = (1, acc)

    def native():
        return tp._wallet_cache.load(1, acc).native_balance_wei

    def discover(native_wei, block):
        pv = {"chain": eth, "address": acc, "view_key": (1, acc),
              "native_wei": native_wei, "block": block, "read_failed": False,
              "balances_raw": {}, "metadata": {}}
        tp._on_combined_ready(pv, 1, {"": Price(Decimal("2000"), 1, "x")})

    tp._apply_targeted_balances(eth, acc, 5 * 10**18, {}, 100)   # native=5 @100
    assert native() == 5 * 10**18
    discover(1 * 10**18, block=99)          # stale → no regress
    assert native() == 5 * 10**18
    discover(6 * 10**18, block=101)         # fresh → applies
    assert native() == 6 * 10**18


def test_reconcile_waits_for_rpc_to_reach_the_event_block(qtbot, monkeypatch):
    """A read that comes back at a block BEFORE the event's block (a lagging
    http backend behind the ws that pushed the log/receipt) must NOT be applied
    — it would settle on the pre-event balance. It reschedules (non-blocking)
    until the RPC reaches the block, then applies. With no min_block, or once
    the read is at/after it, or when retries run out, it applies."""
    from types import SimpleNamespace
    from qeth.plugins.tokens import TokensPlugin
    tp = TokensPlugin(Mock())
    ch = SimpleNamespace(chain_id=1)
    applied: list = []
    retried: list = []
    monkeypatch.setattr(tp, "_apply_targeted_balances",
                        lambda *a: applied.append(a))
    monkeypatch.setattr(tp, "_reconcile_up_to_block",
                        lambda *a: retried.append(a))

    # read at 805 < event block 810 → do NOT apply; schedule a retry
    tp._on_reconcile_read(ch, "0xa", 1, {"0xt": 5}, 805, ["0xt"], 810, 5)
    assert applied == []
    qtbot.waitUntil(lambda: bool(retried), timeout=3000)   # QTimer retry fired
    assert retried[0][3] == 810 and retried[0][4] == 4     # min_block, attempts-1

    # read at/after the event block → apply now
    applied.clear()
    tp._on_reconcile_read(ch, "0xa", 1, {"0xt": 5}, 811, ["0xt"], 810, 5)
    assert len(applied) == 1

    # no min_block → apply immediately (browser tx / event carried no block)
    applied.clear()
    tp._on_reconcile_read(ch, "0xa", 1, {"0xt": 5}, 800, ["0xt"], None, 5)
    assert len(applied) == 1

    # retries exhausted → apply whatever we have rather than loop forever
    applied.clear()
    tp._on_reconcile_read(ch, "0xa", 1, {"0xt": 5}, 805, ["0xt"], 810, 1)
    assert len(applied) == 1


def test_stale_confirm_read_does_not_regress_the_panel(qtbot, tmp_path):
    """The 'qeth send-out never updates' bug: the confirm path's is_new_view
    read fires right after a send and can read the PRE-send balance from a
    lagging backend. Routed through the block-ordered _apply_targeted_balances
    (not the raw in-place path), a stale read is dropped per token, so it can't
    regress the panel over the correct value a fresher read already wrote."""
    from types import SimpleNamespace
    from PySide6.QtCore import Qt
    from qeth.plugins.tokens import TokenListPanel, TokensPlugin
    from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache
    from qeth.icons import IconCache
    from qeth.store import Store
    eth = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
    acc = "0xabc0000000000000000000000000000000000001"
    usdt = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    store = Store.load()
    panel = TokenListPanel(IconCache(), store)
    qtbot.addWidget(panel)
    tp = TokensPlugin(store)
    tp._panel = panel
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp.host = SimpleNamespace(selected_address=acc, current_chain=lambda: eth,
                              start_worker=lambda w: None)
    tp._wallet_cache.save(CachedWallet(
        chain_id=1, address=acc, native_balance_wei=10**18,
        native_price_usd="2000", native_price_updated=1, tokens=[
            CachedToken(contract=usdt, symbol="USDT", name="Tether",
                        decimals=6, balance_raw=644_000000, price_usd="1",
                        price_updated=1)]))
    panel.show_cached(eth, tp._wallet_cache.load(1, acc))
    tp._displayed_view = (1, acc)

    def panel_usdt():
        for r in range(panel.table.rowCount()):
            it = panel.table.item(r, 0)
            key = it.data(Qt.ItemDataRole.UserRole) if it else None
            if key and key[1] == usdt:
                return panel.table.item(r, 1).text()
        return None

    # the correct post-send balance lands at block 810
    tp._apply_targeted_balances(eth, acc, 10**18, {usdt: 423_000000}, 810)
    assert panel_usdt() == "423"
    # the confirm path's fast read (lagging backend) reads PRE-send 644 @805
    tp._apply_targeted_balances(eth, acc, 10**18, {usdt: 644_000000}, 805)
    assert panel_usdt() == "423"        # NOT regressed to 644


def test_balance_ordering_is_per_token_not_per_account(qtbot, tmp_path):
    """A fresh read for one token must not be skipped because an UNRELATED read
    (native only, or another token) landed at a higher block. Ordering is
    per-token — the 'partial USDT send didn't update' bug behind a
    load-balanced node whose backends report different heads. But a genuinely
    stale read for the SAME token (older than its own last block) is ignored."""
    from types import SimpleNamespace
    from qeth.plugins.tokens import TokenListPanel, TokensPlugin
    from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache
    from qeth.icons import IconCache
    from qeth.store import Store
    eth = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
    acc = "0xabc0000000000000000000000000000000000001"
    usdt = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    store = Store.load()
    panel = TokenListPanel(IconCache(), store)
    qtbot.addWidget(panel)
    tp = TokensPlugin(store)
    tp._panel = panel
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp.host = SimpleNamespace(selected_address=acc, current_chain=lambda: eth,
                              start_worker=lambda w: None)
    tp._wallet_cache.save(CachedWallet(
        chain_id=1, address=acc, native_balance_wei=10**18, tokens=[
            CachedToken(contract=usdt, symbol="USDT", name="Tether",
                        decimals=6, balance_raw=644_000000, price_usd="1",
                        price_updated=1)]))
    panel.show_cached(eth, tp._wallet_cache.load(1, acc))
    tp._displayed_view = (1, acc)

    def usdt_bal():
        c = tp._wallet_cache.load(1, acc)
        return next((t.balance_raw for t in c.tokens
                     if t.contract.lower() == usdt), None)

    # an UNRELATED read (native only) lands at a higher block 105
    tp._apply_targeted_balances(eth, acc, 10**18, {}, 105)
    # the partial-send USDT read comes back at block 101 → must still apply
    tp._apply_targeted_balances(eth, acc, 10**18, {usdt: 423_000000}, 101)
    assert usdt_bal() == 423_000000

    # a genuinely stale USDT read (block 100 < its last block 101) is ignored
    tp._apply_targeted_balances(eth, acc, 10**18, {usdt: 999_000000}, 100)
    assert usdt_bal() == 423_000000


def test_stale_read_cannot_overwrite_a_fresher_drop(qtbot, tmp_path):
    """Race guard: a balance worker kicked BEFORE a send (reads the token still
    non-zero) that finishes AFTER the drop must not resurrect it. Reads are
    ordered by block — an older block than one already applied is discarded.
    (The 'cbBTC dropped then reappeared a moment later' bug.)"""
    from types import SimpleNamespace
    from qeth.plugins.tokens import TokenListPanel, TokensPlugin
    from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache
    from qeth.icons import IconCache
    from qeth.store import Store
    eth = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
    acc = "0xabc0000000000000000000000000000000000001"
    cb = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
    store = Store.load()
    panel = TokenListPanel(IconCache(), store)
    qtbot.addWidget(panel)
    tp = TokensPlugin(store)
    tp._panel = panel
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    # re-adding a token with no price kicks a price fetch → needs start_worker
    tp.host = SimpleNamespace(selected_address=acc, current_chain=lambda: eth,
                              start_worker=lambda w: None)
    cached = CachedWallet(
        chain_id=1, address=acc, native_balance_wei=10**18,
        native_price_usd="2000", native_price_updated=1, tokens=[
            CachedToken(contract=cb, symbol="cbBTC", name="cb", decimals=8,
                        balance_raw=100000, price_usd="60000", price_updated=1)])
    tp._wallet_cache.save(cached)
    panel.show_cached(eth, cached)
    tp._displayed_view = (1, acc)

    def held():
        return {t.contract.lower() for t in tp._wallet_cache.load(1, acc).tokens}

    # Fresh read at block 100: cbBTC fully sent → drop.
    tp._apply_targeted_balances(eth, acc, 10**18, {cb: 0}, 100)
    assert cb not in held()

    # STALE read from block 99 (worker kicked before the send) lands late with
    # cbBTC still non-zero → must be ignored, NOT re-added.
    tp._apply_targeted_balances(eth, acc, 10**18, {cb: 100000}, 99)
    assert cb not in held()

    # A genuine re-receive at a NEWER block (101) is applied.
    tp._apply_targeted_balances(eth, acc, 10**18, {cb: 5}, 101)
    assert cb in held()


def test_stale_discovery_cannot_resurrect_a_sent_token(qtbot, tmp_path):
    """A full send: a live targeted read drops the now-zero token. A DISCOVERY
    whose balance snapshot predates the send then completes — it must NOT bring
    the token back in the panel or the cache (the bug a wallet-switch 'fixed').
    """
    from decimal import Decimal
    from types import SimpleNamespace
    from PySide6.QtCore import Qt
    from qeth.plugins.tokens import TokenListPanel, TokensPlugin
    from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache
    from qeth.icons import IconCache
    from qeth.prices import Price
    from qeth.store import Store
    eth = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
    acc = "0xabc0000000000000000000000000000000000001"
    wbtc = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
    usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    store = Store.load()
    panel = TokenListPanel(IconCache(), store)
    qtbot.addWidget(panel)
    tp = TokensPlugin(store)
    tp._panel = panel
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp.host = SimpleNamespace(selected_address=acc, current_chain=lambda: eth)

    cached = CachedWallet(
        chain_id=1, address=acc, native_balance_wei=10**18,
        native_price_usd="2000", native_price_updated=1, tokens=[
            CachedToken(contract=wbtc, symbol="WBTC", name="W", decimals=8,
                        balance_raw=100000, price_usd="60000", price_updated=1),
            CachedToken(contract=usdc, symbol="USDC", name="U", decimals=6,
                        balance_raw=50 * 10**6, price_usd="1", price_updated=1)])
    tp._wallet_cache.save(cached)
    panel.show_cached(eth, cached)
    tp._displayed_view = (1, acc)

    def visible():
        out = set()
        for r in range(panel.table.rowCount()):
            it = panel.table.item(r, 0)
            key = it.data(Qt.ItemDataRole.UserRole) if it else None
            if key and key[1] and not panel.table.isRowHidden(r):
                out.add(key[1])
        return out

    # Live read at block 100: WBTC fully sent → drop.
    tp._apply_targeted_balances(eth, acc, 10**18, {wbtc: 0}, 100)
    assert wbtc not in visible()

    # A stale discovery whose balances were read at block 99 (before the send)
    # now completes — its WBTC>0 is OLDER than the drop, so block-ordering must
    # reject it.
    pv = {"chain": eth, "address": acc, "view_key": (1, acc),
          "native_wei": 10**18, "block": 99,
          "balances_raw": {wbtc: 100000, usdc: 50 * 10**6},
          "metadata": {wbtc: ("WBTC", "W", 8), usdc: ("USDC", "U", 6)}}
    prices = {wbtc: Price(Decimal("60000"), 1, "x"),
              usdc: Price(Decimal("1"), 1, "x"),
              "": Price(Decimal("2000"), 1, "x")}
    tp._on_combined_ready(pv, 1, prices)

    assert wbtc not in visible()                          # not resurrected on panel
    reloaded = tp._wallet_cache.load(1, acc)
    assert wbtc not in {t.contract.lower() for t in reloaded.tokens}   # nor cache


def test_persist_targeted_balances_writes_absolute(qtbot, tmp_path):
    """Persist writes ABSOLUTE native + per-token balances (unlike the receipt
    path's delta): a held token is overwritten in place; an unknown new token
    with no metadata is left for discovery, not invented."""
    from types import SimpleNamespace
    from qeth.plugins.tokens import TokensPlugin
    from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache
    tp = TokensPlugin(Mock())
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp._wallet_cache.save(CachedWallet(
        chain_id=100, address="0xabc", native_balance_wei=1,
        tokens=[CachedToken(contract="0xtok", symbol="T", name="Tok",
                            decimals=18, balance_raw=42)]))
    ch = SimpleNamespace(chain_id=100)

    tp._persist_targeted_balances(ch, "0xABC", 5 * 10**18, {"0xtok": 7})
    reloaded = tp._wallet_cache.load(100, "0xabc")
    assert reloaded is not None
    assert reloaded.native_balance_wei == 5 * 10**18
    assert [(t.contract, t.balance_raw) for t in reloaded.tokens] == [("0xtok", 7)]

    # an unrecognised new token (no metadata to render it) is NOT added
    tp._persist_targeted_balances(ch, "0xABC", 5 * 10**18, {"0xnew": 99})
    reloaded = tp._wallet_cache.load(100, "0xabc")
    assert reloaded is not None
    assert [t.contract for t in reloaded.tokens] == ["0xtok"]


def test_apply_native_updates_only_native_and_is_ordered(qtbot, tmp_path):
    """The ordered native-only write (ledger.apply_native, driven by the ws
    native poll) persists the new native while leaving cached tokens intact,
    and a stale (older-block) poll can't regress it."""
    from types import SimpleNamespace
    from qeth.plugins.tokens import TokensPlugin
    from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache
    tp = TokensPlugin(Mock())
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp._wallet_cache.save(CachedWallet(
        chain_id=100, address="0xabc", native_balance_wei=1,
        tokens=[CachedToken(contract="0xtok", symbol="T", name="Tok",
                            decimals=18, balance_raw=42)]))
    ch = SimpleNamespace(chain_id=100)

    assert tp._ledger.apply_native(ch, "0xABC", 5 * 10**18, block=10) is True
    reloaded = tp._wallet_cache.load(100, "0xabc")
    assert reloaded.native_balance_wei == 5 * 10**18
    assert [(t.contract, t.balance_raw) for t in reloaded.tokens] == [("0xtok", 42)]

    # stale poll (older block) → no regression, no change
    assert tp._ledger.apply_native(ch, "0xABC", 2 * 10**18, block=5) is False
    assert tp._wallet_cache.load(100, "0xabc").native_balance_wei == 5 * 10**18

    # unchanged value at a newer block → nothing re-saved
    assert tp._ledger.apply_native(ch, "0xABC", 5 * 10**18, block=11) is False

    # missing cache → silently ignored (nothing to update in place)
    assert tp._ledger.apply_native(ch, "0xdef", 3, block=1) is False
    assert tp._wallet_cache.load(100, "0xdef") is None


def test_stale_native_poll_does_not_regress_or_renotify(qtbot, monkeypatch,
                                                        tmp_path):
    """2d: an out-of-order ws native poll (LB jumped back) must not regress the
    shown balance nor re-fire a 'received' notification for ETH seen earlier."""
    from qeth.plugins.tokens import TokensPlugin
    from qeth.wallet_cache import CachedWallet, WalletCache
    tp = TokensPlugin(Mock())
    tp.host = Mock()
    tp._wallet_cache = WalletCache(cache_dir=tmp_path)
    tp._wallet_cache.save(CachedWallet(
        chain_id=100, address="0xme", native_balance_wei=10**18))
    tp._displayed_view = (100, "0xme")
    monkeypatch.setattr(tp, "_on_balance_refresh", lambda *a: None)
    ch = _chain_ns(chain_id=100)

    tp.on_native_balance(ch, "0xme", 12 * 10**18, 20)   # fresh: received 2 ETH
    tp.host.notify.reset_mock()                          # drop the first-sight seed

    # stale poll (older block) reporting the OLD balance → dropped entirely
    tp.on_native_balance(ch, "0xme", 10 * 10**18, 15)
    assert tp._wallet_cache.load(100, "0xme").native_balance_wei == 12 * 10**18
    assert tp.host.notify.call_count == 0

    # later fresh poll back to 12 → unchanged, no bogus "received" (the bug was
    # a spurious notify after the stale value lowered the baseline)
    tp.on_native_balance(ch, "0xme", 12 * 10**18, 21)
    assert tp.host.notify.call_count == 0


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


def test_tokens_on_balance_dirty_force_adds_recognised_token(qtbot, monkeypatch):
    # A live Transfer of a RECOGNISED token forces it into the next discovery
    # (the multicall set omits the full curated list), so a token received via
    # a browser tx / airdrop surfaces without waiting minutes for Blockscout.
    # Spam (unrecognised) tokens are NOT forced — they'd be address-poisoning.
    from types import SimpleNamespace
    from qeth.plugins.tokens import TokensPlugin
    tp = TokensPlugin(Mock())
    tp._displayed_view = (1, "0xabc")
    monkeypatch.setattr(tp, "_refresh", lambda a: None)
    monkeypatch.setattr(tp, "_worth_notifying_token",
                        lambda cid, t: t.lower() == "0xcvx")

    tp.on_balance_dirty(SimpleNamespace(chain_id=1), "0xABC", "0xCVX")
    assert "0xcvx" in tp._receipt_contracts.get((1, "0xabc"), set())

    tp.on_balance_dirty(SimpleNamespace(chain_id=1), "0xABC", "0xSPAM")
    assert "0xspam" not in tp._receipt_contracts.get((1, "0xabc"), set())

    # off-view dirty never forces anything in (the guard returns first).
    tp.on_balance_dirty(SimpleNamespace(chain_id=137), "0xABC", "0xCVX")
    assert (137, "0xabc") not in tp._receipt_contracts


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
