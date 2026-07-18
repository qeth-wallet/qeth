"""TokensPlugin — self-contained Tokens topic UI module.

Owns everything the Tokens view needs:
- Sources: BlockscoutSource (discovery), TokenLists (curated whitelist),
  IconCache, DefiLlamaPrices, WalletCache (per-(chain,addr) snapshot),
  TokenMetadataCache (immutable name/symbol/decimals), GoPlusRisk,
  RiskCache.
- All six QThread workers (TokenListsLoader, TokenListWorker,
  RiskWorker, MetadataWorker, BalanceWorker, PricesWorker).
- The TokenListPanel widget + its four action buttons.
- A 60-second periodic refresh timer for the displayed view.

Lifecycle:
  attach              → start TokenListsLoader, start refresh timer.
  on_account_changed  → refresh for the new address (cached preview
                        first; full discovery if lists are loaded).
  on_chain_changed    → refresh for the current address on the new
                        chain.
"""

from __future__ import annotations

import logging
from collections import deque
from decimal import Decimal
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction, QDesktopServices, QIcon, QKeySequence,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHeaderView,
    QMenu, QPushButton, QSizePolicy, QStyle, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import QULONGLONG
from ..dialog import prompt_text
from ..alerts import warn
from ..chain import EthClient, wei_to_ether
from ..formatting import format_balance as _format_balance
from ..formatting import format_usd as _format_usd
from ..formatting import transfer_notice
from ..icons import (
    IconCache, bundled_native_icon, notification_icon, smooth_icon,
)
from ..plugin import Plugin
from ..pricing import DefiLlamaPrices, Price, PriceSource
from ..risk import GoPlusRisk, RiskCache
from ..token_metadata import TokenMetadataCache
from ..tokenlists import TokenListEntry, TokenLists
from ..tokens import (
    BlockscoutSource, EtherscanV2Source, RoutedTokenSource, TokenBalance,
    TokenSource,
)
from ..toptokens import COINGECKO_PLATFORMS, TopTokens
from ..balance_ledger import BalanceLedger
from ..wallet_cache import CachedToken, CachedWallet, WalletCache


log = logging.getLogger("qeth.plugin.tokens")


# --- Workers ----------------------------------------------------------------

class TokenListsLoader(QThread):
    """Background loader for the curated TokenLists (network + cache)."""

    loaded = Signal()
    failed = Signal(str)

    def __init__(self, lists: TokenLists, parent=None):
        super().__init__(parent)
        self.lists = lists

    def run(self) -> None:
        try:
            self.lists.load()
            self.loaded.emit()
        except Exception as e:
            self.failed.emit(str(e))


class TopTokensLoader(QThread):
    """Background TTL refresh of the top-tokens-by-market-cap lists from
    CoinGecko into ``~/.qeth/toptokens``. A no-op (returns instantly)
    when the cache is still fresh; ``refresh`` is best-effort and
    swallows its own failures, so this never breaks discovery — the
    shipped seed keeps serving the head while a refresh is pending."""

    def __init__(self, top: TopTokens, chain_ids: list[int], parent=None):
        super().__init__(parent)
        self.top = top
        self.chain_ids = chain_ids

    def run(self) -> None:
        if self.top.is_stale():
            self.top.refresh(self.chain_ids)


class TokenListWorker(QThread):
    """Fetch native + ERC-20 balances for (chain, address) and apply the
    visibility rules: hidden tokens are dropped; only known-or-force-shown
    ERC-20s are kept; the native asset is always returned as the first
    element so the UI can render it on top.

    Emits ``fetched(native_wei: int, tokens: list[TokenBalance])``."""

    # native_wei must travel as ``object``; declaring ``int`` makes PySide6
    # marshal through qint32 and overflows for any balance above ~2.1e9 wei
    # (well below a millionth of an ETH). The token list is ``object`` too —
    # PySide6 marshals a declared ``list``'s contents.
    fetched = Signal(object, object)
    failed = Signal(str)

    def __init__(self, chain, address: str, source: TokenSource,
                 lists: TokenLists, store, show_all: bool = False, parent=None):
        super().__init__(parent)
        self.chain = chain
        self.address = address
        self.source = source
        self.lists = lists
        self.store = store
        self.show_all = show_all

    def run(self) -> None:
        try:
            try:
                native_wei = EthClient(self.chain).get_balance(self.address)
            except Exception:
                native_wei = 0

            tokens: list[TokenBalance] = []
            if self.source.supports(self.chain):
                cid = self.chain.chain_id
                for b in self.source.list_balances(self.chain, self.address):
                    if b.balance_raw <= 0:
                        continue
                    known_or_pinned = (
                        self.lists.is_known(cid, b.contract)
                        or self.store.is_force_shown(cid, b.contract)
                    )
                    if not known_or_pinned:
                        continue
                    if not self.show_all and self.store.is_hidden(cid, b.contract):
                        continue
                    tokens.append(b)
                tokens.sort(key=lambda x: x.balance, reverse=True)
            self.fetched.emit(native_wei, tokens)
        except Exception as e:
            self.failed.emit(str(e))


class RiskWorker(QThread):
    """Fetch GoPlus per-contract risk reports for any uncached contracts."""

    fetched = Signal(QULONGLONG, object)   # chain_id can exceed qint32
    failed = Signal(str)

    def __init__(self, source: GoPlusRisk, chain_id: int,
                 contracts: list[str], parent=None):
        super().__init__(parent)
        self.source = source
        self.chain_id = chain_id
        self.contracts = list(contracts)

    def run(self) -> None:
        try:
            reports = self.source.fetch(self.chain_id, self.contracts)
            self.fetched.emit(self.chain_id, reports)
        except Exception as e:
            self.failed.emit(str(e))


class MetadataWorker(QThread):
    """Fetch (name, symbol, decimals) on-chain via multicall for any
    contracts not already in the metadata cache."""

    fetched = Signal(QULONGLONG, object)   # chain_id can exceed qint32
    failed = Signal(str)

    def __init__(self, chain, contracts: list[str], parent=None):
        super().__init__(parent)
        self.chain = chain
        self.contracts = list(contracts)

    def run(self) -> None:
        try:
            client = EthClient(self.chain)
            meta = client.multicall_erc20_metadata(self.contracts)
            self.fetched.emit(self.chain.chain_id, meta)
        except Exception as e:
            self.failed.emit(str(e))


class BalanceWorker(QThread):
    """Refresh balances on-chain via Multicall3 + eth_getBalance."""

    # `dict` would make PySide6 marshal its values through qint64; some
    # ERC-20 raw balances (e.g. ~3.2e19 for ASF with 18 decimals) overflow.
    # Trailing ``object`` is the block the read was pinned to, so a consumer can
    # discard a result older than one it already applied — a slow/stale worker
    # must never overwrite a fresher read (see _apply_targeted_balances). Block
    # numbers exceed qint32 → object.
    refreshed = Signal(QULONGLONG, object, object, object, object)  # +per-token blocks
    failed = Signal(str)

    def __init__(self, chain, address: str, token_contracts: list[str],
                 parent=None):
        super().__init__(parent)
        self.chain = chain
        self.address = address
        self.contracts = list(token_contracts)

    def run(self) -> None:
        try:
            client = EthClient(self.chain)
            # Co-read native + token balances + block in ONE aggregate at
            # "latest" (never "block in the future" on a lagging backend), so
            # the native value shares the tokens' backend/height per chunk
            # rather than coming from a separate eth_getBalance a load balancer
            # could serve stale. The block lets consumers order this result
            # against concurrent reads (per-token block-ordering); a lagging
            # read carries a lower block and is superseded by a fresher one.
            block, native, balances, blocks = client.head_balances(
                self.contracts, self.address)
            if native is None:
                # getEthBalance didn't come back (its chunk failed) — fall
                # back to a plain read so we still emit a native value. This
                # read is NOT co-read with `block` (later chunks may have
                # succeeded and set it), so the consumer's native stamp can be
                # off by the skew between the two — transient and self-healing
                # (the next ordered read supersedes it), and confined to this
                # rare failure path; pre-change EVERY refresh read native
                # separately.
                native = client.get_balance(self.address, "latest")
            self.refreshed.emit(self.chain.chain_id, native, balances, block, blocks)
        except Exception as e:
            self.failed.emit(str(e))


class PricesWorker(QThread):
    """Fetch USD prices for the currently-displayed assets."""

    prices_ready = Signal(QULONGLONG, object)

    def __init__(self, source: PriceSource, chain, contracts: list[str],
                 include_native: bool, parent=None):
        super().__init__(parent)
        self.source = source
        self.chain = chain
        self.contracts = contracts
        self.include_native = include_native

    def run(self) -> None:
        try:
            prices = self.source.fetch(
                self.chain, self.contracts, include_native=self.include_native,
            )
        except Exception:
            prices = {}
        self.prices_ready.emit(self.chain.chain_id, prices)


# --- Plugin -----------------------------------------------------------------

class TokensPlugin(Plugin):
    name = "Tokens"

    REFRESH_INTERVAL_MS = 60_000
    # Coalesce a burst of ws Transfer logs (a swap's many legs) into one
    # balance refresh, fired this long after the first dirty event.
    LIVE_REFRESH_DEBOUNCE_MS = 1500
    # Same idea for the targeted balanceOf re-read, but snappier — it's the
    # cheap authoritative path (one tiny multicall), so it shouldn't lag the
    # user's perception of "the tx landed". Still long enough to fold a swap's
    # several same-block legs into one round-trip.
    TARGETED_BALANCE_DEBOUNCE_MS = 400
    # A recognised (curated-list) token we hold but CAN'T value — the price
    # source doesn't cover it (DefiLlama has no quote for e.g. sUSD / EURT) —
    # shows only for this grace window after it first appears unpriced, then
    # hides (unless pinned/custom). The window lets a just-received token's price
    # load (a few discovery/price cycles) before we give up on valuing it; a
    # token whose price never arrives stops cluttering the list. Pin it to keep
    # it visible regardless.
    KNOWN_UNPRICED_GRACE_S = 150.0
    # When the on-screen chain has a live ws, Transfer logs carry token
    # balances and the ws also reads native ~once a minute (on_native_balance),
    # so the HTTP sweep slows to this deep backstop instead of polling every
    # minute. Drops back to REFRESH_INTERVAL_MS the moment ws goes down.
    SLOW_REFRESH_INTERVAL_MS = 300_000  # 5 min
    # Cadence of the cheap balanceOf reconcile of the displayed tokens
    # (_on_reconcile_tick), run only while ws is live. It's the safety net for a
    # Transfer-log subscription that dies SILENTLY: a provider/LB can drop the
    # logs filter while newHeads keep flowing, so link_state stays True (socket
    # warm, native poll fine) yet no balance_dirty ever fires and an ERC-20
    # balance sits stale until the demoted 5-min sweep. This bounds that
    # staleness independent of the discovery sweep.
    RECONCILE_INTERVAL_MS = 60_000

    _ARRIVAL_CAP = 2048   # bound the dedup memory; arrivals are infrequent

    def __init__(self, store):
        super().__init__()
        self._store = store
        # First-source-wins dedup for arrival notifications. The ws Transfer-log
        # watcher and our tx-confirmation receipt scan can BOTH surface the same
        # arrival (either can be the faster — a slow ws sub, or a slow confirm);
        # whichever reaches on_transfer_seen first notifies, keyed by the
        # Transfer event's (chain, tx, log index). Bounded FIFO so it can't grow
        # without limit.
        self._notified_arrivals: set[tuple[int, str, int | None]] = set()
        self._arrival_order: deque[tuple[int, str, int | None]] = deque()
        # Sources (constructed once, reused across refreshes).
        # Etherscan v2 is preferred when a key is configured (more
        # reliable + covers chains Blockscout doesn't, e.g. BSC).
        # Blockscout is the always-available fallback for the
        # chains it serves. Both lookups consult the store at
        # call time so changes to the key take effect on the very
        # next refresh without re-instantiating either source.
        self._token_source = RoutedTokenSource(
            EtherscanV2Source(lambda: self._store.etherscan_api_key),
            BlockscoutSource(),
        )
        self._token_lists = TokenLists()
        # Top-tokens-by-market-cap head: a bounded set we always multicall
        # balanceOf over, so a held major shows even when the indexer's
        # per-holder list omits it (Blockscout has been observed to drop a
        # real USDC). Seeded from the shipped snapshot, TTL-refreshed from
        # CoinGecko in the background. The indexer still covers the long tail.
        self._top_tokens = TopTokens()
        self._icon_cache = IconCache()
        self._price_source: PriceSource = DefiLlamaPrices()
        self._wallet_cache = WalletCache()
        self._token_metadata = TokenMetadataCache()
        self._risk_source = GoPlusRisk()
        self._risk_cache = RiskCache()
        # Display state.
        self._show_all = False
        self._displayed_view: tuple[int, str] | None = None
        # Throttled live-balance refresh, driven by ws Transfer logs via
        # on_balance_dirty (relayed from the TransactionsPlugin's LiveWatcher).
        self._live_refresh_timer: QTimer | None = None
        self._live_refresh_addr: str | None = None
        # Targeted balance re-read driven by ws Transfer logs: the dirtied
        # tokens per (chain_id, addr_lower) awaiting one coalesced multicall,
        # and its short debounce timer. Runs regardless of the on-screen view
        # (persists to cache) so a tx's effect is ready on the next tab switch.
        # value = [chain, account, {token_lower}, min_block|None]
        self._dirty_balances: dict[tuple[int, str], list[Any]] = {}
        self._targeted_timer: QTimer | None = None
        # Views whose wallet cache was updated in the background (a targeted
        # balance re-read) while the Tokens tab was NOT active, so the on-screen
        # panel is stale relative to disk. Consumed by on_activated to re-render
        # the moment the user switches to the tab. Keyed (chain_id, addr_lower).
        self._pending_rerender: set[tuple[int, str]] = set()
        # Views with a live price fetch in flight (for displayed-but-unpriced
        # tokens) — coalesces a burst of live updates into one fetch.
        self._prices_in_flight: set[tuple[int, str]] = set()
        # (chain_id, addr) → monotonic time a recognised token was FIRST seen
        # unpriced. Drives KNOWN_UNPRICED_GRACE_S: show while its price might
        # still load, then hide. Reset when it gets a price or is re-received.
        self._unpriced_since: dict[tuple[int, str], float] = {}
        # Block-ordered balance writer: the single owner of the per-token /
        # per-account-native freshness stamps and the cache mutation that
        # honours them (was two dicts + duplicated ordering logic here). Shares
        # the wallet cache, token sources and the unpriced-grace map so a
        # (re-)received token restarts its grace window on add.
        self._ledger = BalanceLedger(
            lambda: self._wallet_cache, lambda: self._token_lists,
            lambda: self._token_metadata, self._unpriced_since)
        # Set on app.aboutToQuit (wired in attach). The reconcile retry chain is
        # a QTimer.singleShot loop (up to 20 × 700 ms); a retry firing in the
        # teardown window would spawn a BalanceWorker whose ref then dies while
        # it runs — and Qt's QThread destructor aborts the process. The guard
        # stops spawning once we're quitting (satellite 3).
        self._shutting_down = False
        # Chains with a live ws connection (LiveWatcher link_state, relayed) —
        # drives the sweep-interval throttle.
        self._ws_live_chains: set[int] = set()
        # Last native balance seen per (chain_id, addr_lower) from the ws poll,
        # to detect an *increase* (= ETH received) for a desktop notification.
        # First sighting only seeds the baseline (no notification).
        self._last_native_seen: dict[tuple[int, str], int] = {}
        self._discovery_in_flight: set[tuple[int, str]] = set()
        # Per (chain_id, addr_lower): contracts pulled from a
        # confirmed tx receipt's Transfer logs since the last
        # successful discovery for this wallet. Drained when that
        # discovery runs (on_discovered unions them into the
        # multicall set, then pop). See note_receipt_logs.
        self._receipt_contracts: dict[tuple[int, str], set[str]] = {}
        # Lifecycle objects (built lazily / in attach).
        self._panel = None
        self._refresh_timer: QTimer | None = None
        self._reconcile_timer: QTimer | None = None
        self._lists_loader: TokenListsLoader | None = None
        self._top_tokens_loader: TopTokensLoader | None = None

    # --- Plugin contract ----------------------------------------------------

    @property
    def token_panel(self):
        """Alias used by tests + transitional MainWindow code."""
        return self.widget()

    # Read-only accessors so sibling plugins (notably TransactionsPlugin's
    # details dialog) can look up curated token metadata + share the icon
    # cache through the Host, without binding to this plugin's class.
    @property
    def token_lists(self) -> TokenLists:
        return self._token_lists

    @property
    def icon_cache(self) -> IconCache:
        return self._icon_cache

    def last_balance_block(self, chain_id: int, address: str,
                           token: str) -> int | None:
        """The block at which we last applied a confirmed ``balanceOf`` read
        for ``(chain, wallet, token)`` — the BalanceLedger's freshness stamp.
        A verified preview uses this as a fork floor so sending a token that
        just arrived doesn't fork BEFORE the inbound transfer (which would make
        it falsely revert on a zero balance). ERC-20 contract only; ``None``
        when we've never stamped this token (then the floor is unaffected).
        Idle tokens are only stamped by the periodic sweeps (which lag the
        head), so this reads near the head ONLY right after a live transfer —
        exactly when the floor should bite."""
        return self._ledger.balance_block.get(
            (chain_id, address.lower(), token.lower()))

    def _native_chain_icon(self, chain_id: int):
        """Chain logo for the native-asset row, via the host's chain-icon
        cache (kicks a fetch on miss). None until attached / fetched."""
        getter = getattr(self.host, "chain_icon", None) if self.host else None
        return getter(chain_id) if getter else None

    def on_chain_icon_ready(self, chain_id: int, pix) -> None:
        """Host calls this when a chain logo finishes fetching, so the
        native-asset row can fill its (async) icon."""
        if self._panel is not None:
            self._panel.update_native_icon(chain_id, pix)

    def widget(self) -> QWidget:
        if self._panel is None:
            self._panel = TokenListPanel(
                self._icon_cache, self._store,
                chain_icon_getter=self._native_chain_icon,
            )
            self._panel.hide_requested.connect(self._on_hide_token)
            self._panel.pin_requested.connect(self._on_pin_token)
            self._panel.add_custom_requested.connect(self._on_add_custom_token)
            self._panel.show_all_toggled.connect(self._on_show_all_toggled)
            self._panel.transfers_requested.connect(self._on_transfers_requested)
            self._panel.send_requested.connect(self._on_send_requested)
        return self._panel

    def _on_send_requested(self, chain_id: int, contract: str) -> None:
        """Send button click. Look up the user's balance + decimals
        for the selected asset (ERC-20 from the wallet cache, or
        the native row), then ask the host to open the unified
        send-transaction dialog. The dialog handles the rest of
        the flow (sign + broadcast) through the same path the RPC
        signing requests use."""
        if self.host is None:
            return
        chain = self.host.current_chain()
        addr = self.host.selected_address
        if addr is None or chain.chain_id != chain_id:
            return
        cached = self._wallet_cache.load(chain_id, addr)
        is_native = (contract == TokenListPanel.NATIVE_CONTRACT)
        if is_native:
            asset = {
                "is_native": True,
                "contract": None,
                "symbol": chain.symbol,
                "decimals": 18,
                "balance_raw": cached.native_balance_wei if cached else 0,
                "logo_uri": None,
            }
        else:
            entry = None
            if cached is not None:
                for b in cached.tokens:
                    if b.contract.lower() == contract.lower():
                        entry = b
                        break
            if entry is None:
                # Nothing cached — shouldn't happen for a selected
                # row, but bail rather than ship a 0-balance Send.
                return
            tl = self._token_lists.get(chain.chain_id, contract)
            asset = {
                "is_native": False,
                "contract": entry.contract,
                "symbol": entry.symbol,
                "decimals": entry.decimals,
                "balance_raw": entry.balance_raw,
                "logo_uri": tl.logo_uri if tl is not None else None,
                # USD price from the wallet cache (str or None) so the send
                # dialog can show the live value of the typed amount.
                "price_usd": entry.price_usd,
            }
        opener = getattr(self.host, "open_send_dialog", None)
        if callable(opener):
            opener(asset, chain, addr)

    def _on_transfers_requested(self, chain_id: int, contract: str) -> None:
        """Double-click on a token row opens the explorer's
        token-transfers-for-this-holder page —
        ``/token/<contract>?a=<user>`` — so the user lands on their
        own movement history of that token rather than the bare
        contract page."""
        if self.host is None:
            return
        chain = self.host.current_chain()
        addr = self.host.selected_address
        if not chain.explorer or not addr:
            return
        base = chain.explorer.rstrip("/")
        url = f"{base}/token/{contract}?a={addr}"
        QDesktopServices.openUrl(QUrl(url))

    def action_widgets(self):
        if self._panel is None:
            return []
        return self._panel.action_widgets()

    # --- persistence shim ---------------------------------------------------

    def header_state(self) -> str:
        if self._panel is None:
            return ""
        return self._panel.header_state()

    def restore_header_state(self, state_hex: str) -> None:
        if self._panel is not None:
            self._panel.restore_header_state(state_hex)

    def attach(self, host) -> None:
        super().attach(host)
        # Stop spawning reconcile workers once the app starts quitting (see
        # _shutting_down / satellite 3). aboutToQuit fires before app.exec()
        # returns, while the event loop can still deliver a pending retry.
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._on_about_to_quit)
        # 60-second background refresh against whatever view is on screen.
        # _on_refresh_tick self-dedupes via _discovery_in_flight, so an
        # unfinished run blocks the next tick rather than stacking.
        self._refresh_timer = QTimer()
        self._refresh_timer.setInterval(self.REFRESH_INTERVAL_MS)
        self._refresh_timer.timeout.connect(self._on_refresh_tick)
        self._refresh_timer.start()
        # Cheap balanceOf reconcile at a fixed cadence. While ws is live the
        # discovery sweep above is demoted to SLOW_REFRESH_INTERVAL_MS (5 min);
        # this bounds ERC-20 balance staleness to ~RECONCILE_INTERVAL_MS if the
        # Transfer-log subscription dies silently (see _on_reconcile_tick).
        self._reconcile_timer = QTimer()
        self._reconcile_timer.setInterval(self.RECONCILE_INTERVAL_MS)
        self._reconcile_timer.timeout.connect(self._on_reconcile_tick)
        self._reconcile_timer.start()
        # Kick the curated token-lists loader. Host tracks the worker so
        # it isn't GC'd while running.
        self._lists_loader = TokenListsLoader(self._token_lists)
        self._lists_loader.loaded.connect(self._on_lists_loaded)
        self._lists_loader.failed.connect(self._on_lists_load_failed)
        host.start_worker(self._lists_loader)
        # Refresh the top-tokens head if the cache has gone stale (no-op
        # otherwise). The shipped seed already serves the head meanwhile.
        self._top_tokens_loader = TopTokensLoader(
            self._top_tokens, list(COINGECKO_PLATFORMS))
        host.start_worker(self._top_tokens_loader)

    # --- lifecycle hooks ----------------------------------------------------

    def on_account_changed(self, address: str | None) -> None:
        # New viewing session → re-seed the native-receive baseline, so
        # returning to an account doesn't fire a misleading "received" for the
        # delta accumulated while it was off-screen (and unpolled).
        self._last_native_seen.clear()
        if address is None:
            if self._panel is not None:
                self._panel.clear()
            self._displayed_view = None
            return
        self._refresh(address)

    def on_chain_changed(self) -> None:
        self._last_native_seen.clear()   # re-seed baseline; see on_account_changed
        if self.host is None:
            return
        addr = self.host.selected_address
        if addr is not None:
            self._refresh(addr)

    def on_activated(self) -> None:
        """The Tokens tab became active. Two things happen:

        1. If a background ws update was flagged (persisted to cache but the
           panel was inactive so it couldn't repaint), render it now from cache.
        2. **Always** reconcile the displayed balances against the chain with
           one cheap multicall. This is the safety net the whole feature hinges
           on: a confirmation we never saw a ws Transfer for (a dropped socket,
           a tx whose logs we don't subscribe to) otherwise leaves a stale row
           — e.g. a fully-sent token still listed — until the slow sweep. The
           result routes through the same persist+rerender as a live update, so
           a now-zero token drops off the moment you look at the tab."""
        if self.host is None or self._panel is None:
            return
        addr = self.host.selected_address
        if addr is None:
            return
        chain = self.host.current_chain()
        key = (chain.chain_id, addr.lower())
        if key in self._pending_rerender:
            self._pending_rerender.discard(key)
            self._rerender_view_from_cache(chain, addr)
        self._reconcile_displayed_balances(chain, addr)

    def _reconcile_displayed_balances(self, chain, addr: str) -> None:
        """Re-read the displayed token set's balances against the chain (one
        multicall) and route the result through the same persist+rerender as a
        live update, so a now-zero token drops. This is the FRESH, later read
        that the live event's eager 400 ms targeted read can miss when the http
        RPC is momentarily a block behind the ws that delivered the
        confirmation — the asymmetry where 'switch tab after confirm' worked
        (on_activated reconciles) but 'be on the tab when it confirms' didn't
        (only the eager read fired). Called from on_activated AND the on-view
        live-refresh so both views get it."""
        if self.host is None:
            return
        cached = self._wallet_cache.load(chain.chain_id, addr)
        if cached is None or not cached.tokens:
            return
        bw = BalanceWorker(chain, addr, [t.contract for t in cached.tokens])
        bw.refreshed.connect(
            lambda cid, nat, bals, blk, blks, ch=chain, acct=addr:
            self._apply_targeted_balances(ch, acct, nat, bals, blk, blks))
        bw.failed.connect(
            lambda msg: log.warning("reconcile BalanceWorker failed: %s", msg))
        self.host.start_worker(bw)

    # --- core refresh pipeline ---------------------------------------------

    # keccak256("Transfer(address,address,uint256)") — the ERC-20
    # transfer event signature. ERC-721 uses the SAME selector but
    # with the tokenId as a third indexed topic (4 topics total),
    # so a ``len(topics) == 3`` filter cleanly excludes 721s.
    _TRANSFER_TOPIC0 = (
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )

    @staticmethod
    def _topic_hex(topic) -> str:
        """Normalise a log topic to a lowercase 0x-prefixed hex
        string. web3.py 7 hands topics back as ``HexBytes``;
        ``str(HexBytes(b'\\xdd...'))`` returns the BYTES literal
        ``"b'\\xdd...'"``, not the hex form — comparing against a
        ``"0xdd…"`` string never matches. Use ``.hex()`` (which
        ``HexBytes`` defines as the raw hex without prefix) when
        available, otherwise treat the value as already-a-string."""
        if hasattr(topic, "hex"):
            h = topic.hex()
            return h if h.startswith("0x") else "0x" + h.lower()
        return str(topic).lower()

    def on_balance_dirty(self, chain, account: str, token: str,
                         block=None, native=None, balance=None) -> None:
        """A ws ERC-20 Transfer touched ``account`` on ``chain`` (the
        LiveWatcher, relayed by TransactionsPlugin). We never trust the log's
        value — instead it names the token whose ``balanceOf`` changed.
        ``native``/``balance`` are the LiveWatcher's authoritative re-read OVER
        THE WS CONNECTION at the log's ``block`` (the backend that streamed the
        log has that block, so no http skew). When present they're applied
        directly, block-ordered; when None (ws read failed) we fall back to an
        http re-read that waits for the RPC to reach ``block``.

        Two responses, deliberately split:

        - **Always** (even off the on-screen view) queue a *targeted* balance
          re-read of just that token (+ native), apply it to the wallet cache,
          and repaint in place if it's the current view. This is the cheap,
          authoritative path that makes a (browser-originated) tx's effect show
          the moment it confirms — and persists it so switching to the Tokens
          tab afterwards is instant rather than waiting on the slow sweep.
        - **On the on-screen view only**, also schedule the full discovery
          refresh (prices, and surfacing a brand-new token not yet cached)."""
        if token:
            if balance is not None and native is not None:
                # authoritative ws read at the event's block → apply now, no
                # http round-trip and no waiting (block-ordering still guards
                # against an out-of-order apply).
                self._apply_targeted_balances(
                    chain, account, native, {token.lower(): int(balance)}, block)
            else:
                # ws read unavailable → http re-read that waits for the block.
                self._queue_targeted_balance(chain, account, token, block)
        if self._displayed_view != (chain.chain_id, account.lower()):
            return
        # The discovery multicall set deliberately omits the full curated list
        # (~5k contracts once aborted the balance thread), so a freshly-received
        # token that's neither a top-N major nor yet indexed by Blockscout
        # wouldn't surface from the refresh alone — it'd wait minutes for the
        # indexer (or a receipt scan, which only fires for txs WE signed). We
        # have the token's address from the Transfer log, so force a RECOGNISED
        # one straight into the next discovery — same spam filter as the
        # notification, so address-poisoning stays out and unknown tokens still
        # wait for the indexer exactly as before. This is what makes a token
        # received via a browser tx (or an airdrop) appear without a refresh.
        if token and self._worth_notifying_token(chain.chain_id, token):
            self._receipt_contracts.setdefault(
                (chain.chain_id, account.lower()), set()).add(token.lower())
        self._schedule_live_refresh(account)

    def _queue_targeted_balance(self, chain, account: str, token: str,
                                block=None) -> None:
        """Accumulate a dirty token for a coalesced targeted balance read.
        A swap fires several Transfer logs touching the account in one block;
        folding them into one short-debounced multicall keeps it to a single
        cheap round-trip per burst instead of one per leg. ``block`` (the log's
        block) is tracked as the minimum height the re-read must reach."""
        key = (chain.chain_id, account.lower())
        slot = self._dirty_balances.get(key)
        if slot is None:
            slot = [chain, account, set(), None]
            self._dirty_balances[key] = slot
        slot[2].add(token.lower())
        if block is not None:
            slot[3] = max(slot[3] or 0, int(block))
        if self._targeted_timer is None:
            self._targeted_timer = QTimer(self)
            self._targeted_timer.setSingleShot(True)
            self._targeted_timer.timeout.connect(self._on_targeted_balance)
        if not self._targeted_timer.isActive():
            self._targeted_timer.start(self.TARGETED_BALANCE_DEBOUNCE_MS)

    def _on_targeted_balance(self) -> None:
        """Fire one tiny ``balanceOf`` multicall per dirtied (chain, account)
        for exactly the tokens the ws Transfer logs named, then apply the
        authoritative result to the cache (+ panel if on screen). Waits (non-
        blocking) for the RPC to reach the log's block first."""
        if self.host is None:
            self._dirty_balances.clear()
            return
        pending = self._dirty_balances
        self._dirty_balances = {}
        for chain, account, contracts, min_block in pending.values():
            if not contracts:
                continue
            self._reconcile_up_to_block(
                chain, account, sorted(contracts), min_block)

    def _on_about_to_quit(self) -> None:
        """App is quitting — stop spawning reconcile workers (satellite 3)."""
        self._shutting_down = True

    def _reconcile_up_to_block(self, chain, account: str, tokens,
                               min_block, attempts: int = 20) -> None:
        """Re-read ``tokens`` at latest and apply — but only once the RPC's head
        has reached ``min_block`` (the event's block). If the read comes back at
        an EARLIER block (a lagging http backend behind the ws that pushed the
        log / receipt), reschedule shortly rather than apply a pre-event balance.
        Non-blocking (a QTimer, NOT a sleeping worker thread → no signing-dialog
        stall). Correctness against out-of-order reads still comes from per-token
        block-ordering; this just stops the eager read from settling on a stale
        value and leaving the update to the slow sweep."""
        if self.host is None or not tokens or self._shutting_down:
            return
        toks = list(tokens)
        bw = BalanceWorker(chain, account, toks)
        bw.refreshed.connect(
            lambda cid, nat, bals, blk, blks, ch=chain, acct=account, tk=toks,
            mb=min_block, at=attempts:
            self._on_reconcile_read(ch, acct, nat, bals, blk, tk, mb, at, blks))
        bw.failed.connect(
            lambda msg: log.warning("targeted BalanceWorker failed: %s", msg))
        self.host.start_worker(bw)

    def _on_reconcile_read(self, chain, account: str, native_wei, balances_raw,
                           block, tokens, min_block, attempts, blocks=None) -> None:
        # `block` is the per-batch MIN (conservative) — wait on it so every
        # token's chunk has reached the receipt block before we apply.
        if (min_block is not None and block is not None
                and int(block) < int(min_block) and attempts > 1
                and not self._shutting_down):
            QTimer.singleShot(
                700, lambda: self._reconcile_up_to_block(
                    chain, account, tokens, min_block, attempts - 1))
            return
        self._apply_targeted_balances(
            chain, account, native_wei, balances_raw, block, blocks)

    def _apply_targeted_balances(
        self, chain, account: str, native_wei, balances_raw: dict, block=None,
        blocks: dict | None = None,
    ) -> None:
        """Authoritative targeted balances landed. Persist to the wallet cache
        first (that's the source of truth), then render the on-screen view from
        it. Off-view — the common case, a browser tx while the Tokens tab was
        inactive — flag it so switching to the tab renders it (a bare tab switch
        doesn't repaint; see on_activated). Driving the panel off the cache
        (not an in-place diff against a possibly-mismatched displayed set) is
        what makes this reliable when tokens are hidden/dust-filtered.

        ``block`` orders reads PER TOKEN (in _persist_targeted_balances): a read
        older than the block last recorded for a given token is ignored for that
        token. Ordering must NOT be per-account — an unrelated read (native, or
        another token) landing at a higher block would otherwise skip a fresh,
        correct read for THIS token (the 'USDT balance didn't update' bug behind
        a load-balanced node whose backends report different heads)."""
        key = (chain.chain_id, account.lower())
        raw = {k.lower(): int(v) for k, v in balances_raw.items()}
        blks = {k.lower(): v for k, v in blocks.items()} if blocks else None
        self._persist_targeted_balances(
            chain, account, native_wei, raw, block, blks)
        # Decide on-screen from the HOST's actual selection, not the internal
        # _displayed_view — the latter is transiently reset to None by
        # _invalidate_view_and_refresh (the qeth-send confirm path), and if the
        # drop landed in that window the panel was left stale (cache emptied but
        # the row still shown) until a tab switch. The host's selected account +
        # chain is the ground truth for "what the user is looking at".
        if self._is_current_view(chain, account):
            self._rerender_view_from_cache(chain, account)
        else:
            self._pending_rerender.add(key)
        # A token added by the live path (a received/swapped-in token) has no
        # price yet, and discovery doesn't reliably cover it (receipt-extras is
        # one-shot; it may be neither top-N nor Blockscout-indexed yet), so it
        # would show a blank USD value indefinitely. Fetch prices for any
        # displayed-but-unpriced token directly.
        self._ensure_prices_for_unpriced(chain, account)

    def _ensure_prices_for_unpriced(self, chain, account: str) -> None:
        """Fetch USD prices for cached tokens that don't have one yet and apply
        them (cache + panel). Bounded to the wallet's own holdings and guarded
        so a burst of live updates coalesces into one fetch per view."""
        if self.host is None:
            return
        cached = self._wallet_cache.load(chain.chain_id, account)
        if cached is None:
            return
        unpriced = [t.contract for t in cached.tokens if not t.price_usd]
        key = (chain.chain_id, account.lower())
        if not unpriced or key in self._prices_in_flight:
            return
        self._prices_in_flight.add(key)
        pw = PricesWorker(self._price_source, chain, unpriced,
                          include_native=False)
        pw.prices_ready.connect(
            lambda cid, prices, ch=chain, acct=account:
            self._apply_fetched_prices(ch, acct, prices))
        pw.finished.connect(
            lambda k=key: self._prices_in_flight.discard(k))
        self.host.start_worker(pw)

    def _apply_fetched_prices(self, chain, account: str, prices: dict) -> None:
        """Write freshly-fetched token prices into the cache and repaint the USD
        column if this view is on screen."""
        cached = self._wallet_cache.load(chain.chain_id, account)
        if cached is None:
            return
        changed = False
        for t in cached.tokens:
            p = prices.get(t.contract.lower())
            if p is not None and t.price_usd != str(p.price_usd):
                t.price_usd = str(p.price_usd)
                t.price_updated = p.timestamp or 0
                changed = True
        if not changed:
            return
        self._wallet_cache.save(cached)
        if self._is_current_view(chain, account):
            self._rerender_view_from_cache(chain, account)

    def _is_current_view(self, chain, account: str) -> bool:
        """Whether the host is currently showing ``(chain, account)`` — the
        authoritative 'is this on screen' check (see _apply_targeted_balances)."""
        host = self.host
        if host is None:
            return False
        sel = getattr(host, "selected_address", None)
        if not sel:
            return False
        try:
            cur = host.current_chain()
        except Exception:
            return False
        return cur.chain_id == chain.chain_id and sel.lower() == account.lower()

    def _rerender_view_from_cache(self, chain, account: str) -> None:
        """Reflect the wallet cache for ``(chain, account)`` in the panel.
        Fast in-place balance update when the displayed contract set still
        matches (no flicker — the common send-of-a-held-token case); otherwise
        a full ``show_cached`` rebuild (a just-received token added a row, or
        the panel was showing a different view). Always recomputes USD via
        reapply_prices so the Value column tracks the new balance."""
        if self._panel is None:
            return
        cached = self._wallet_cache.load(chain.chain_id, account)
        if cached is None:
            return
        if not self._show_all:
            cached = self._filter_hidden_from_cache(chain, cached)
        tokens = [
            TokenBalance(contract=t.contract, symbol=t.symbol, name=t.name,
                         decimals=t.decimals, balance_raw=t.balance_raw)
            for t in cached.tokens
        ]
        if self._panel.update_balances_if_set_unchanged(
                chain, cached.native_balance_wei, tokens):
            self._panel.reapply_prices()
        else:
            self._panel.show_cached(chain, cached)
        self._displayed_view = (chain.chain_id, account.lower())

    def _persist_targeted_balances(
        self, chain, account: str, native_wei, balances_raw: dict, block=None,
        blocks: dict | None = None,
    ) -> None:
        """Write authoritative native + per-token balances into the wallet
        cache, block-ordered. Thin wrapper over the ledger (kept so existing
        callers / tests read naturally). ``blocks`` carries the per-token read
        heights (each token ordered by its own chunk's block, not the batch min)."""
        self._ledger.apply_read(
            chain, account, native_wei, balances_raw, block, blocks)

    def on_native_balance(self, chain, account: str, native_wei,
                          block=None) -> None:
        """The on-screen account's native balance, read over the live ws every
        ~minute (LiveWatcher.native_balance, relayed by TransactionsPlugin).
        Inbound ETH fires no Transfer log, so on_balance_dirty never sees it —
        this is the native counterpart. Apply in place (no discovery) and
        freshen the cached native. ``block`` orders it: a stale poll (an LB that
        jumped backwards) is dropped so it can't regress the shown balance or
        re-fire a 'received' notification for money that arrived earlier."""
        if self._displayed_view != (chain.chain_id, account.lower()):
            return
        if self._ledger.native_stale(chain.chain_id, account, block):
            return
        # Panel first (it detects the change against the still-old cache), then
        # the ordered cache write (stamps the native block).
        self._on_balance_refresh(chain.chain_id, native_wei, {})
        self._ledger.apply_native(chain, account, native_wei, block)
        self._notify_native_delta(chain, account, int(native_wei))

    def _notify_native_delta(self, chain, account: str, native_wei: int) -> None:
        """Desktop-notify an ETH receive, inferred from a rise in the native
        balance between ws polls (a plain ETH receive fires no log, so this is
        the only live signal for it). First sighting seeds the baseline only;
        a *decrease* is our own send/gas (notified from the confirmed tx, not
        here). The amount is exact unless we also spent gas in the same window."""
        key = (chain.chain_id, account.lower())
        prev = self._last_native_seen.get(key)
        self._last_native_seen[key] = native_wei
        if prev is None or native_wei <= prev:
            return
        amount = _format_balance(wei_to_ether(native_wei - prev))
        title, body = transfer_notice(
            False, amount, chain.symbol, chain_name=chain.name)
        icon = notification_icon(bundled_native_icon(chain.symbol), False)
        self._notify(title, body, icon)

    def _worth_notifying_token(self, chain_id: int, token: str) -> bool:
        """Whether a token transfer deserves a desktop notification: only if we
        recognise the token (it's in the curated lists or the user added it).
        Filters out address-poisoning spam — scammers blast every active
        address with in/out transfers of tokens that are in no list."""
        addr = token.lower()
        return (self._token_lists.get(chain_id, addr) is not None
                or self._store.is_custom_token(chain_id, addr))

    def on_transfer_seen(
        self, chain, account: str, token: str, counterparty: str,
        outgoing: bool, raw_value,
        tx_hash: str | None = None, log_index: int | None = None,
    ) -> None:
        """An ERC-20 Transfer touching the on-screen account. Raise a
        sent/received desktop notification — but skip the scam/spam that
        dominates these logs:

          - **zero value** moves nothing: a ``transferFrom(me, x, 0)`` still
            emits a Transfer event (often on a stale/zero allowance) but isn't
            a real transfer;
          - **unrecognised tokens** are overwhelmingly address-poisoning spam —
            only notify for tokens in the curated lists or that the user added.

        Fed from two sources for the same arrival — the ws Transfer-log watcher
        (LiveWatcher, relayed) and, for a tx of ours, the confirmation receipt
        scan — so an arrival still notifies when the ws sub missed it. ``tx_hash``
        + ``log_index`` key the dedup so only the FIRST source notifies.

        (The balance still re-reads for every transfer via on_balance_dirty;
        this only gates the *notification*.)"""
        if int(raw_value) == 0:
            return
        if not self._worth_notifying_token(chain.chain_id, token):
            return
        key = self._arrival_key(chain.chain_id, tx_hash, log_index)
        if key is not None and key in self._notified_arrivals:
            return                    # a faster source already notified this one
        meta = self._token_metadata.get(chain.chain_id, token.lower())
        if meta and meta.get("symbol"):
            symbol = str(meta["symbol"])
            decimals = int(meta.get("decimals") or 18)
            amount = _format_balance(Decimal(int(raw_value)) / (Decimal(10) ** decimals))
        else:
            symbol = "a token"
            amount = ""          # no decimals → no trustworthy quantity
        title, body = transfer_notice(
            outgoing, amount, symbol,
            counterparty=counterparty, chain_name=chain.name)
        # The token logo if it's cached (held tokens usually are); otherwise
        # the badge stands alone. We don't block the notification on a fetch.
        base = self._icon_cache.get(chain.chain_id, token.lower())
        self._notify(title, body, notification_icon(base, outgoing))
        if key is not None:
            self._record_arrival(key)

    @staticmethod
    def _arrival_key(
        chain_id: int, tx_hash: str | None, log_index: int | None,
    ) -> tuple[int, str, int | None] | None:
        """Cross-source dedup key for one Transfer event, or None when the
        source didn't carry a tx hash (then we can't dedup, so notify)."""
        if not tx_hash:
            return None
        return (chain_id, str(tx_hash).lower().removeprefix("0x"), log_index)

    def _record_arrival(self, key: tuple[int, str, int | None]) -> None:
        self._notified_arrivals.add(key)
        self._arrival_order.append(key)
        while len(self._arrival_order) > self._ARRIVAL_CAP:
            self._notified_arrivals.discard(self._arrival_order.popleft())

    def _notify(self, title: str, body: str, icon=None) -> None:
        host = self.host
        notify = getattr(host, "notify", None) if host is not None else None
        if callable(notify):
            notify(title, body, icon)

    def _schedule_live_refresh(self, address: str) -> None:
        """Throttle: the first dirty event arms a one-shot timer; events
        arriving while it's pending fold in (no restart → no starvation under
        a steady transfer stream)."""
        self._live_refresh_addr = address
        if self._live_refresh_timer is None:
            self._live_refresh_timer = QTimer(self)
            self._live_refresh_timer.setSingleShot(True)
            self._live_refresh_timer.timeout.connect(self._on_live_refresh)
        if not self._live_refresh_timer.isActive():
            self._live_refresh_timer.start(self.LIVE_REFRESH_DEBOUNCE_MS)

    def _on_live_refresh(self) -> None:
        addr = self._live_refresh_addr
        if (addr and self._displayed_view is not None
                and self._displayed_view[1] == addr.lower()):
            # A fresh balance reconcile (≈1.5 s after the event, by which point
            # the http RPC has caught up to the ws head) drops a now-zero token
            # the eager 400 ms targeted read may have missed. Without it the
            # on-screen view had no later read and a sent token lingered until
            # the slow sweep — whereas an off-screen view got it via on_activated.
            if self.host is not None:
                self._reconcile_displayed_balances(
                    self.host.current_chain(), addr)
            self._refresh(addr)

    def on_ws_link_state(self, chain, connected: bool) -> None:
        """ws link up/down for a chain (LiveWatcher, relayed). When the
        on-screen chain's ws is live, Transfer logs carry token balances and
        the ws polls native ~once a minute, so the HTTP sweep slows to a deep
        backstop (and snaps back to the fast floor if ws drops)."""
        if connected:
            if chain.chain_id not in self._ws_live_chains:
                # A (re)connect: while the ws was down we were blind to new
                # heads / logs and a reorg may have rewound the chain. Clear
                # this chain's freshness floors so the fresh reads that follow
                # re-establish truth instead of being ordered out by a stamp
                # from before the gap.
                self._ledger.reset_chain(chain.chain_id)
            self._ws_live_chains.add(chain.chain_id)
        else:
            self._ws_live_chains.discard(chain.chain_id)
        self._apply_sweep_interval()

    def _apply_sweep_interval(self) -> None:
        """Set the sweep timer to the slow interval when the on-screen chain
        has a live ws, else the normal one. Re-evaluated on every link_state
        — and a chain switch re-emits link_state as the connection retargets,
        so the view change is covered too."""
        if self._refresh_timer is None:
            return
        cur = self._displayed_view[0] if self._displayed_view else None
        live = cur is not None and cur in self._ws_live_chains
        self._refresh_timer.setInterval(
            self.SLOW_REFRESH_INTERVAL_MS if live else self.REFRESH_INTERVAL_MS)

    def note_receipt_logs(self, chain, receipt) -> None:
        """Called by TransactionsPlugin when a tx receipt comes in.
        Parse the receipt's logs for ERC-20 Transfer events that
        touch any of our wallets and stash the (token, wallet)
        pairs for the next discovery refresh.

        Without this, a swap (USDT → USDC, ETH → DAI, …) leaves
        the receiving wallet showing only the old token until
        Blockscout indexes the inbound — minutes. With it, the
        moment the tx confirms we know which token contracts
        touched our wallets and force them into the next multicall
        set. Refresh fires immediately if the affected wallet is
        the one currently being viewed."""
        # web3.py 7 returns receipts as AttributeDict, which is a
        # Mapping but NOT a dict subclass — so isinstance(receipt,
        # dict) is False and skipping that way silently no-ops
        # every call. Use duck-typing on .get() instead. A plain
        # dict also matches (used in tests + the raw RPC path).
        if receipt is None or not hasattr(receipt, "get"):
            return
        our_addrs = {a["address"].lower() for a in self._store.accounts}
        chain_id = chain.chain_id
        affected_wallets: set[str] = set()
        # token contracts that moved for each of our wallets in THIS tx — read
        # authoritatively below, at the receipt's own block.
        touched: dict[str, set[str]] = {}
        # (wallet_lower, token_lower) -> summed received amount in THIS receipt.
        # Summed across logs so the same token received twice in one tx credits
        # once with the total (the ledger stamps the token after the credit, so
        # a per-log call would drop the second — see BalanceLedger.apply_floor).
        credits: dict[tuple[str, str], int] = {}
        receipt_block = self._parse_block(receipt.get("blockNumber"))
        # The tx's own endpoints are affected regardless of its events:
        # the sender's NATIVE balance changed by construction (gas +
        # value), a plain value transfer has no logs at all, and a tx
        # whose events are custom (bridge calls, TAC system contracts)
        # still moved value. Without this, a chain with no working ws
        # (chainlist-added, e.g. TAC) showed a stale native balance
        # until the next 60 s sweep even though we held the receipt.
        for party in (receipt.get("from"), receipt.get("to")):
            if isinstance(party, str) and party.lower() in our_addrs:
                affected_wallets.add(party.lower())
        logs = receipt.get("logs") or []
        for lg in logs:      # NB: not `log` — that's the module logger
            topics = lg.get("topics") or []
            if len(topics) != 3:
                continue
            if self._topic_hex(topics[0]) != self._TRANSFER_TOPIC0:
                continue
            token = lg.get("address")
            if not token:
                continue
            # Topic addresses are 32-byte left-padded; take the
            # right 20 bytes (40 hex chars) of the normalised topic.
            from_lower = "0x" + self._topic_hex(topics[1])[-40:]
            to_lower = "0x" + self._topic_hex(topics[2])[-40:]
            # Parse the value (uint256 in log.data, not in topics).
            try:
                value = int(self._topic_hex(lg.get("data") or "0x0"), 16)
            except ValueError:
                value = 0
            for party, is_recipient in ((from_lower, False),
                                          (to_lower, True)):
                if party in our_addrs:
                    key = (chain_id, party)
                    self._receipt_contracts.setdefault(
                        key, set()
                    ).add(token)
                    touched.setdefault(party, set()).add(token.lower())
                    affected_wallets.add(party)
                    # On the RECEIVING side, sum the credit for a single ledger
                    # apply below — so the new holding is visible the next time
                    # the user views that wallet, without waiting for discovery
                    # on a wallet they may not be viewing. (The sender side
                    # relies on the reconcile / normal-view refresh.)
                    if is_recipient and value > 0:
                        ck = (party, token.lower())
                        credits[ck] = credits.get(ck, 0) + value
        # Apply each received token's total ONCE, block-ordered + idempotent:
        # if an authoritative read at/after the receipt block already landed,
        # the amount is already in the balance, so this no-ops (no double count
        # on top of the ws absolute read, or a duplicate confirm). The stamp it
        # leaves also blocks a later stale zero read from dropping the token.
        for (wallet, token_lower), delta in credits.items():
            self._ledger.apply_floor(
                chain, wallet, token_lower, receipt_block, delta)
        if not affected_wallets or self.host is None:
            return
        # Confirm-driven reconcile: a confirmed tx is the source of truth, so
        # re-read the moved tokens (at the node's current head) and route them
        # through the same drop/persist/repaint path as a live event. This
        # covers the sender side (dropping a sent-to-zero token) that the old
        # code left to discovery. It waits (NON-blocking, via QTimer — not the
        # old sleeping worker that stalled the signing dialog) for the http RPC
        # to reach the receipt's block before trusting the read: the confirm
        # arrives over the ws/tip, but the balanceOf read goes over http, which
        # can still be a moment behind that block and return the PRE-send value.
        for wallet, tokens in touched.items():
            self._reconcile_up_to_block(
                chain, wallet, sorted(tokens), receipt_block)
        current = self.host.selected_address
        if current is None:
            return
        chain_now = self.host.current_chain()
        if (chain_now.chain_id == chain_id
                and current.lower() in affected_wallets):
            self._invalidate_view_and_refresh()

    @staticmethod
    def _parse_block(value) -> int | None:
        """A receipt ``blockNumber`` as an int — hex string or already-int."""
        if value is None:
            return None
        try:
            return int(value, 16) if isinstance(value, str) else int(value)
        except (ValueError, TypeError):
            return None

    def _sibling_held_contracts(self, chain_id: int,
                                 self_address: str) -> set[str]:
        """Every token contract that has appeared in any OTHER
        wallet's cache on this chain, returned as checksum
        addresses suitable for merging into the multicall set.

        Why this exists: a user can send USDC from wallet A to
        wallet B inside qeth; B's Blockscout token-discovery is a
        few blocks behind chain head and may miss the inbound
        transfer for minutes. The receiving wallet then looks
        empty in the UI until the next discovery cycle. By cross-
        checking balanceOf on the union of "every token any of my
        OTHER wallets has ever cached" we catch the new holding at
        chain-head speed without waiting for Blockscout to catch
        up.

        We deliberately DON'T filter on the sibling's current
        balance > 0: if A sent its full USDC stash to B and A's
        cache was refreshed first, A would show 0 USDC and a
        ``balance > 0`` filter would drop USDC from the set — so
        the cross-check on B would never query it. Including
        zero-balance siblings catches that exact case. The cost is
        a few extra multicall slots per refresh; cheap.

        Reads cached snapshots only — no extra RPC. Excludes the
        current address."""
        self_lower = self_address.lower()
        contracts: set[str] = set()
        for acct in self._store.accounts:
            sibling = acct.get("address", "")
            if not sibling or sibling.lower() == self_lower:
                continue
            cached = self._wallet_cache.load(chain_id, sibling)
            if cached is None:
                continue
            for token in cached.tokens:
                contracts.add(token.contract)
        return contracts

    def _refresh(self, address: str) -> None:
        if self.host is None or self._panel is None:
            return
        # Captured non-None aliases for the nested worker closures below;
        # mypy doesn't carry the guard's narrowing into inner scopes.
        host, panel = self.host, self._panel
        chain = self.host.current_chain()
        view_key = (chain.chain_id, address.lower())
        is_new_view = self._displayed_view != view_key

        cached = self._wallet_cache.load(chain.chain_id, address)
        if cached is not None and is_new_view:
            # Drop user-hidden tokens before rendering. The disk
            # cache deliberately keeps them around — unhide should
            # bring them back instantly without re-discovering —
            # but they must not appear on the table.
            if not self._show_all:
                cached = self._filter_hidden_from_cache(chain, cached)
            # Immediate render from cache; no flicker while we refresh.
            self._panel.show_cached(chain, cached)
            self._displayed_view = view_key
            # Only kick off a multicall balance refresh once per view;
            # repeated _refresh calls for the same view don't need
            # another round-trip. We deliberately DON'T inject
            # sibling-held contracts here — _on_balance_refresh
            # only updates tokens that are already in the cached
            # set (no metadata for new contracts at this stage), so
            # the extra calls would be wasted. The full discovery
            # pass below picks up sibling-only contracts properly.
            bw = BalanceWorker(
                chain, address, [t.contract for t in cached.tokens],
            )
            # Route through the block-ordered persist+render path, NOT the raw
            # in-place _on_balance_refresh: this read fires on the confirm path
            # (_invalidate_view_and_refresh) right after a send, so it can land
            # on a lagging backend and read the PRE-send balance. Applied
            # in-place + un-ordered it regressed the panel over the correct
            # value the targeted read had just written (the 'send-out never
            # updates' bug — the cache was right, the panel stale). Block
            # ordering drops the stale read per token.
            bw.refreshed.connect(
                lambda cid, nat, bals, blk, blks, ch=chain, acct=address:
                self._apply_targeted_balances(ch, acct, nat, bals, blk, blks))
            bw.failed.connect(
                lambda msg: log.warning("BalanceWorker failed: %s", msg)
            )
            self.host.start_worker(bw)

        if not self._token_lists.loaded:
            if cached is None and is_new_view:
                self._panel.show_message(
                    "Loading token lists… selection will refresh automatically"
                )
                self._displayed_view = view_key
            return

        # Mark this view as the displayed one BEFORE kicking (or
        # piggy-backing on) the async pipeline. Two reasons:
        #
        #  (1) ``_on_combined_ready`` drops stale results by
        #  comparing against ``_displayed_view``; without setting
        #  it here, fresh wallets (no cache → the early-render
        #  branch above didn't run) would have their completed
        #  discovery silently discarded.
        #
        #  (2) Must happen BEFORE the in_flight guard. Otherwise
        #  the "click fresh wallet → click another → click fresh
        #  again" sequence falls into the early return below
        #  WITHOUT clearing the panel or updating
        #  ``_displayed_view`` — leaving the previous wallet's
        #  rows on screen and discarding the in-flight result
        #  when it lands.
        if is_new_view:
            if cached is None:
                # No cache yet — show a placeholder rather than the
                # previous wallet's contents.
                self._panel.show_message("Discovering tokens…")
            self._displayed_view = view_key

        # Per-view discovery guard — avoid stacking duplicate
        # Blockscout/multicall/prices chains when _refresh fires
        # multiple times for the same view.
        if view_key in self._discovery_in_flight:
            return
        self._discovery_in_flight.add(view_key)

        # Per-call state captured by closure so concurrent jobs for
        # different views never trample each other's data.
        pv = {"chain": chain, "address": address, "view_key": view_key}

        def on_discovered(blockscout_native_wei, blockscout_tokens: list) -> None:
            # Discard Blockscout's balances — they're a few blocks behind
            # chain head. The contract list is the only thing we keep;
            # metadata (name/symbol/decimals) is fetched on-chain via
            # multicall (immutable, cached), with Blockscout's values as
            # a one-shot fallback for contracts whose multicall reverts.
            # Build the multicall set as the union of three sources:
            #   1. Blockscout's per-holder token list — holder-
            #      specific but lags chain head by minutes.
            #   2. Force-shown contracts (user pinned).
            #   3. Sibling wallets' cached holdings — catches
            #      intra-qeth transfers ahead of Blockscout.
            #
            # Curated token lists (the Frame-style "balanceOf
            # every known token") were briefly part of this union
            # but caused Qt to abort the BalanceWorker QThread
            # mid-flight under the ~5k-contract load (observed on
            # mainnet: thread destroyed while running). The
            # functional value the curated path provided — chain-
            # head visibility on inbound transfers — is already
            # covered by the receipt scan for transfers between
            # our own wallets and by Blockscout for everything
            # else (within minutes of indexing). The curated
            # metadata cache prefill we did at startup still helps
            # the receipt-credit path know how to label a fresh
            # inbound USDT without a separate metadata fetch.
            forced = {a for (cid, a) in self._store.shown_tokens
                      if cid == chain.chain_id}
            # Custom-added tokens: always balance-checked (so they surface the
            # moment they have a balance) even though they're not force-shown.
            custom = {a for (cid, a) in self._store.custom_tokens
                      if cid == chain.chain_id}
            siblings = self._sibling_held_contracts(chain.chain_id, address)
            # Drain receipt-derived contracts for THIS view — popped
            # so they don't permanently inflate the multicall set
            # once we've already discovered them.
            receipt_extras = self._receipt_contracts.pop(
                (chain.chain_id, address.lower()), set()
            )
            # Top-tokens-by-market-cap head: always multicalled, so a held
            # major surfaces even when the indexer's per-holder list dropped
            # it. Bounded (~a few hundred/chain) — well under the ~5k curated
            # load that once aborted the balance QThread. The dedupe below
            # keeps it from double-counting contracts the indexer already
            # returned; balanceOf for the (mostly zero) rest is cheap.
            top = self._top_tokens.contracts(chain.chain_id)
            seen = set()
            contracts: list[str] = []
            for c in (
                [b.contract for b in blockscout_tokens]
                + sorted(forced) + sorted(custom) + sorted(siblings)
                + sorted(receipt_extras)
                + top
            ):
                cl = c.lower()
                if cl in seen:
                    continue
                seen.add(cl)
                contracts.append(c)
            blockscout_meta = {
                b.contract.lower(): (b.symbol, b.name, b.decimals)
                for b in blockscout_tokens
            }

            def build_metadata() -> dict:
                out: dict = {}
                for c in contracts:
                    al = c.lower()
                    m = self._token_metadata.get(chain.chain_id, al)
                    if m:
                        out[al] = (m["symbol"], m["name"], m["decimals"])
                    elif al in blockscout_meta:
                        out[al] = blockscout_meta[al]
                return out

            def kick_prices() -> None:
                pw = PricesWorker(
                    self._price_source, chain, contracts, include_native=True,
                )
                pw.prices_ready.connect(
                    lambda c, p: self._on_combined_ready(pv, c, p)
                )
                host.start_worker(pw)

            def kick_risk_then_prices() -> None:
                """GoPlus first for any uncached non-whitelisted contracts,
                then prices. Whitelisted ones can never be scams so we
                skip them; risk fetch is ~300ms batched and cached."""
                needed_risk = self._risk_cache.missing(
                    chain.chain_id,
                    [c for c in contracts
                     if not self._token_lists.is_known(chain.chain_id, c)],
                )
                if not needed_risk:
                    kick_prices()
                    return

                def on_risk(cid: int, reports: dict) -> None:
                    if reports:
                        self._risk_cache.put_many(cid, reports)
                    kick_prices()

                def on_risk_fail(msg: str) -> None:
                    log.warning("risk fetch failed: %s", msg)
                    kick_prices()

                rw = RiskWorker(self._risk_source, chain.chain_id, needed_risk)
                rw.fetched.connect(on_risk)
                rw.failed.connect(on_risk_fail)
                host.start_worker(rw)

            def on_balances(cid: int, mc_native, mc_balances: dict,
                            block=None, blocks=None) -> None:
                pv["native_wei"] = int(mc_native)
                pv["block"] = block
                pv["blocks"] = {k.lower(): v for k, v in (blocks or {}).items()}
                raw = {k.lower(): int(v) for k, v in mc_balances.items()}
                # A queried token ABSENT from the result had its balanceOf
                # read fail (rate-limited upstream / lagging node), not
                # return zero — a real zero comes back explicitly. Keep its
                # last-known value so a failed read can't blink it out.
                self._carry_forward_absent(chain, address, raw, contracts)
                pv["balances_raw"] = raw
                pv["metadata"] = build_metadata()
                kick_risk_then_prices()

            def on_balances_fail(msg: str) -> None:
                log.warning("post-discovery multicall failed: %s", msg)
                pv["native_wei"] = int(blockscout_native_wei)
                # The multicall failed → we have NO fresh on-chain balances.
                # Do not seed the view from the explorer's stale numbers (they
                # can blink out a just-claimed token the indexer hasn't seen).
                # Mark the read as failed so the merge in _on_combined_ready
                # preserves the existing cache and only refreshes prices.
                pv["read_failed"] = True
                pv["block"] = None
                pv["blocks"] = {}
                raw = {
                    b.contract.lower(): int(b.balance_raw) for b in blockscout_tokens
                }
                # The multicall failed entirely; these are the explorer's
                # stale balances. For a token we were already showing, prefer
                # its last-known (a conclusive multicall value) over the
                # explorer's lagging number — the explorer value only seeds
                # tokens we hadn't shown yet.
                self._carry_forward_absent(
                    chain, address, raw, contracts, override_existing=True)
                pv["balances_raw"] = raw
                pv["metadata"] = build_metadata()
                kick_risk_then_prices()

            def kick_balance_multicall() -> None:
                bw = BalanceWorker(chain, address, contracts)
                bw.refreshed.connect(on_balances)
                bw.failed.connect(on_balances_fail)
                host.start_worker(bw)

            missing_meta = self._token_metadata.missing(chain.chain_id, contracts)
            if not missing_meta:
                kick_balance_multicall()
                return

            def on_meta(cid: int, meta: dict) -> None:
                if meta:
                    self._token_metadata.put_many(chain.chain_id, meta)
                kick_balance_multicall()

            def on_meta_fail(msg: str) -> None:
                log.warning("metadata multicall failed: %s", msg)
                kick_balance_multicall()

            mw = MetadataWorker(chain, missing_meta)
            mw.fetched.connect(on_meta)
            mw.failed.connect(on_meta_fail)
            host.start_worker(mw)

        def on_failed(msg: str) -> None:
            # Indexer discovery failed (e.g. Blockscout's tokenlist
            # endpoint timing out for a high-activity address like
            # Vitalik's). Rather than leave the panel blank, fall back to
            # a top-N-only pass — the majors can still be multicalled
            # directly, so a held USDC shows even with the indexer down.
            # on_discovered drives the in-flight guard to completion via
            # _on_combined_ready, so we DON'T discard it here.
            if self._top_tokens.contracts(chain.chain_id):
                log.warning("token discovery source failed (%s); falling "
                            "back to top-N multicall for %s", msg, address)
                on_discovered(0, [])
                return
            self._discovery_in_flight.discard(view_key)
            # Nothing to fall back to (chain has no top-N): surface the
            # error so the empty panel isn't mistaken for "no tokens".
            if self._displayed_view == view_key:
                panel.show_error(msg)
            log.warning("token discovery failed for %s: %s", address, msg)

        worker = TokenListWorker(
            chain, address, self._token_source, self._token_lists, self._store,
            show_all=self._show_all,
        )
        worker.fetched.connect(on_discovered)
        worker.failed.connect(on_failed)
        if cached is None and is_new_view:
            self._panel.show_loading(address)
            self._displayed_view = view_key
        self.host.start_worker(worker)

    def _on_combined_ready(self, pv: dict, chain_id: int, prices) -> None:
        """TokenListWorker + PricesWorker both done. Apply visibility +
        sort once, then update the panel. Single visible update."""
        self._discovery_in_flight.discard(pv["view_key"])
        if self._panel is None:
            return
        chain = pv["chain"]
        if chain.chain_id != chain_id:
            return
        # Drop stale results — user may have switched wallets/chain
        # while this pipeline was running.
        if self._displayed_view != pv["view_key"]:
            return

        address = pv["address"]
        native_wei = pv["native_wei"]
        metadata = pv["metadata"]
        balances_raw = pv["balances_raw"]
        block = pv.get("block")            # per-batch MIN — orders native
        blocks = pv.get("blocks") or {}    # per-token heights — order each token

        # MERGE discovery's read into the cached set (do NOT replace it):
        #  - start from the current cache (carry every held token forward),
        #  - apply each fresh read PER-TOKEN block-ordered — a read older than
        #    the block we last recorded for a token is ignored, so a stale
        #    discovery can't drop a freshly-claimed token,
        #  - only an authoritative zero at a fresh-enough block drops a token,
        #  - a failed multicall (block is None) applies NO balances — it just
        #    refreshes prices over the preserved cache.
        # This is what stops the "claimed token appeared then got hidden /
        # flickered" bug: discovery used to rebuild the whole view from its own
        # (possibly stale or failed) read and drop everything else.
        cached_now = self._wallet_cache.load(chain.chain_id, address)
        # Order discovery's native like every other native write (2c): a stale
        # read (older block, behind an LB) must not regress it — keep the cached
        # value; a fresh read stamps the native block so a later poll orders
        # against it. The prices/risk legs put seconds between the read and here,
        # exactly the window a confirm's native write lands in.
        if self._ledger.native_stale(chain.chain_id, address, block):
            if cached_now is not None:
                native_wei = cached_now.native_balance_wei
        else:
            self._ledger.stamp_native(chain.chain_id, address, block)
        cached_by = {t.contract.lower(): t
                     for t in (cached_now.tokens if cached_now else [])}
        merged: dict[str, TokenBalance] = {
            cl: TokenBalance(contract=t.contract, symbol=t.symbol, name=t.name,
                             decimals=t.decimals, balance_raw=t.balance_raw)
            for cl, t in cached_by.items()
        }
        # A FAILED multicall reports read_failed → we have no fresh balances, so
        # apply none (preserve the cache, just refresh prices). A successful
        # read applies its balances; ``block`` (when present) orders per token.
        if not pv.get("read_failed"):
            for addr, raw in balances_raw.items():
                cl = addr.lower()
                # Order by the height THIS token's chunk ran at, not the batch
                # min — a min dragged down by one lagging chunk otherwise
                # rejects this token's fresh read forever (the stuck-balance
                # bug). A token that was NOT freshly read — carried forward from
                # cache by _carry_forward_absent — is absent from `blocks`, so
                # tblock is None: it's applied (its cached value) but NOT stamped
                # (satellite 2), so a later CORRECT read at a lower block isn't
                # discarded as stale against a floor it never actually earned.
                tblock = blocks.get(cl)
                if self._ledger.is_token_stale(chain.chain_id, address, cl,
                                               tblock):
                    continue   # stale read for this token — keep cached value
                self._ledger.stamp_token(chain.chain_id, address, cl, tblock)
                if raw == 0 and not self._show_all:
                    merged.pop(cl, None)      # authoritative zero → drop
                    continue
                meta = metadata.get(cl)
                if meta is None and cl in cached_by:
                    c = cached_by[cl]
                    meta = (c.symbol, c.name, c.decimals)
                if meta is None:
                    continue
                sym, name, decimals = meta
                merged[cl] = TokenBalance(
                    contract=addr, symbol=sym, name=name,
                    decimals=decimals, balance_raw=raw)
        tokens: list[TokenBalance] = list(merged.values())

        entries = {
            (chain.chain_id, b.contract.lower()): e
            for b in tokens
            if (e := self._token_lists.get(chain.chain_id, b.contract)) is not None
        }

        # Price resilience: for any asset the fresh fetch didn't return, fall back
        # to its last-known price from the cache. Without this a price-source
        # outage (e.g. DefiLlama 404ing every batch) leaves every token unpriced,
        # which the visibility filter then hides — emptying the whole panel.
        # Fresh prices always win; a token never priced stays unpriced (grace).
        if cached_now is not None:
            if "" not in prices and cached_now.native_price_usd:
                prices[""] = Price(Decimal(cached_now.native_price_usd),
                                   cached_now.native_price_updated, "cache")
            for cl, ct in cached_by.items():
                if cl not in prices and ct.price_usd:
                    prices[cl] = Price(Decimal(ct.price_usd),
                                       ct.price_updated, "cache")

        visible = self._compute_visible_tokens(chain, tokens, prices)
        apply_dust = not self._show_all
        if self._panel.contract_set_matches(chain, visible):
            self._panel.update_balances_if_set_unchanged(
                chain, native_wei, visible,
            )
            self._panel.set_prices(
                chain.chain_id, prices, apply_dust_filter=apply_dust,
            )
        else:
            self._panel.render_full(
                chain, native_wei, visible, entries, prices,
                apply_dust_filter=apply_dust,
            )

        # Cache the normal-mode visible set (post-dust + force-show; never the
        # spotlight superset) PLUS any held tokens the user HID. The disk cache
        # must keep hidden holdings — the display filters them at render time
        # (_filter_hidden_from_cache), but dropping them from disk means
        # unhiding can't bring a token back until the next discovery happens to
        # re-read it. Dust/zero stay filtered so the fast in-place rerender
        # (contract-set match) still applies for the common case.
        cache_visible = (
            self._compute_visible_tokens(chain, tokens, prices, show_all=False)
            if self._show_all else visible
        )
        shown = {b.contract.lower() for b in cache_visible}
        hidden_held = [
            b for b in tokens
            if int(b.balance_raw) > 0 and b.contract.lower() not in shown
            and self._store.is_hidden(chain.chain_id, b.contract)
        ]
        self._save_wallet_cache(
            chain, address, native_wei, cache_visible + hidden_held,
            prices, entries)

    def _filter_hidden_from_cache(self, chain, cached):
        """Return a shallow copy of ``cached`` with user-hidden
        tokens removed. Keeps the disk cache itself untouched so
        unhiding still has the entry to bring back."""
        kept = [
            t for t in cached.tokens
            if not self._store.is_hidden(chain.chain_id, t.contract)
        ]
        if len(kept) == len(cached.tokens):
            return cached
        from dataclasses import replace
        return replace(cached, tokens=kept)

    def _within_unpriced_grace(self, chain_id: int, addr: str) -> bool:
        """True while a recognised-but-unpriced token is still inside its grace
        window (``KNOWN_UNPRICED_GRACE_S`` from when first seen unpriced). Starts
        the timer on first call. Shared by the discovery filter and the panel's
        display-time hiding so both agree on when to drop it. Reset elsewhere
        when the token gets a price or is re-received from zero."""
        import time as _t
        key = (chain_id, addr.lower())
        since = self._unpriced_since.setdefault(key, _t.monotonic())
        return _t.monotonic() - since < self.KNOWN_UNPRICED_GRACE_S

    def _compute_visible_tokens(self, chain, tokens: list, prices,
                                show_all: bool | None = None) -> list:
        """Apply hide + dust + force-show filter and sort by USD
        value desc."""
        if show_all is None:
            show_all = self._show_all
        dust = TokenListPanel.DUST_USD_THRESHOLD
        out = []
        for b in tokens:
            addr = b.contract.lower()
            if show_all:
                out.append(b)
                continue
            # User-hidden tokens drop out entirely (unless
            # spotlight/show_all is on, where everything passes).
            # Has to live here as well as in the Blockscout-source
            # worker — the multicall + tokenlist discovery path
            # comes through this function without going through
            # WalletTokensLoader's filter, so SUSHI/ZIK and other
            # priced curated entries would otherwise re-surface on
            # every refresh.
            if self._store.is_hidden(chain.chain_id, addr):
                continue
            if self._store.is_force_shown(chain.chain_id, addr):
                out.append(b)
                continue
            # Custom-added token with a non-zero balance: the user added it
            # explicitly, so show any amount (exempt from the dust filter).
            # It was already dropped at exactly-zero by the raw==0 filter.
            if self._store.is_custom_token(chain.chain_id, addr):
                out.append(b)
                continue
            price = prices.get(addr)
            if price is None:
                # Unrecognised + no price → spam → drop. A recognised token we
                # can't value shows only inside its grace window (so a just-
                # received token isn't hidden while its price loads), then hides
                # if the price never arrives. Pinned/custom already returned.
                if (self._token_lists.is_known(chain.chain_id, addr)
                        and self._within_unpriced_grace(chain.chain_id, addr)):
                    out.append(b)
                continue
            self._unpriced_since.pop((chain.chain_id, addr), None)  # priced
            if b.balance * price.price_usd < dust:
                continue
            out.append(b)

        def _value(b):
            p = prices.get(b.contract.lower())
            return b.balance * p.price_usd if p else Decimal(0)
        out.sort(key=_value, reverse=True)
        return out

    def _on_refresh_tick(self) -> None:
        """Periodic re-fetch for the currently-displayed account."""
        if self.host is None:
            return
        addr = self.host.selected_address
        if addr is not None:
            self._refresh(addr)

    def _on_reconcile_tick(self) -> None:
        """Cheap periodic balanceOf reconcile of the displayed tokens — the
        safety net for a silently-dead Transfer-log subscription (see
        RECONCILE_INTERVAL_MS). Reuses _reconcile_displayed_balances (one
        multicall, routed through the same persist+rerender as a live update),
        so a stale ERC-20 balance corrects within ~a minute even when the ws
        stops delivering logs without dropping the socket.

        Only runs while the on-screen chain has a live ws: with ws down the
        60-s discovery sweep already re-reads every balance, so a second read
        would be redundant."""
        if self.host is None or self._shutting_down:
            return
        addr = self.host.selected_address
        if addr is None:
            return
        chain = self.host.current_chain()
        if chain.chain_id not in self._ws_live_chains:
            return
        self._reconcile_displayed_balances(chain, addr)

    def _carry_forward_absent(
        self, chain, address: str, balances_raw: dict, contracts: list,
        *, override_existing: bool = False,
    ) -> None:
        """Protect already-shown balances from an inconclusive read.

        A token we queried but that's ABSENT from ``balances_raw`` had its
        balanceOf read fail (a rate-limited upstream, a lagging node) rather
        than return zero — a genuine zero comes back explicitly in a
        multicall. So absence must not drop a token that was showing a
        balance: carry its last-known value forward until a conclusive read
        lands. With ``override_existing`` (the block-explorer fallback path),
        also replace a stale explorer value for an already-shown token with
        its last-known — the explorer's number only seeds tokens we hadn't
        shown yet. Last-known comes from the saved wallet cache (the previous
        good view); mutates ``balances_raw`` in place."""
        cached = self._wallet_cache.load(chain.chain_id, address)
        if cached is None:
            return
        prev = {t.contract.lower(): t.balance_raw
                for t in cached.tokens if t.balance_raw}
        if not prev:
            return
        queried = {c.lower() for c in contracts}
        for cl, bal in prev.items():
            if cl not in queried:
                continue
            if cl not in balances_raw or override_existing:
                balances_raw[cl] = bal

    def _on_balance_refresh(self, chain_id: int, native_wei, balances_raw: dict,
                            block=None) -> None:
        """Fast in-place balance refresh for the cached set, ahead of
        the slower discovery+prices chain. ``block`` is the (ignored here)
        trailing arg BalanceWorker now emits."""
        if self.host is None or self._panel is None:
            return
        chain = self.host.current_chain()
        if chain.chain_id != chain_id:
            return
        addr = self.host.selected_address
        if addr is None:
            return
        cached = self._wallet_cache.load(chain_id, addr)
        if cached is None:
            return
        # Compare/update against the DISPLAYED set, not the raw cache. The panel
        # renders the hidden-filtered subset, so building from the full cache
        # makes contract_set_matches fail whenever any token is user-hidden —
        # and the in-place update then silently no-ops (the live balance never
        # moves). Filtering here matches what's on screen.
        if not self._show_all:
            cached = self._filter_hidden_from_cache(chain, cached)

        nothing_changed = (
            int(native_wei) == cached.native_balance_wei
            and all(
                int(balances_raw.get(t.contract.lower(), t.balance_raw))
                == t.balance_raw
                for t in cached.tokens
            )
        )
        if nothing_changed:
            return

        tokens = [
            TokenBalance(
                contract=t.contract, symbol=t.symbol, name=t.name,
                decimals=t.decimals,
                balance_raw=int(balances_raw.get(t.contract.lower(), t.balance_raw)),
            )
            for t in cached.tokens
        ]
        if self._panel.update_balances_if_set_unchanged(chain, native_wei, tokens):
            self._panel.reapply_prices()

    def _save_wallet_cache(
        self, chain, address: str, native_wei: int,
        tokens: list, prices: dict, entries: dict,
    ) -> None:
        """Persist the multicall-derived view."""
        import time
        now = int(time.time())

        cached = CachedWallet(
            chain_id=chain.chain_id,
            address=address.lower(),
            native_balance_wei=int(native_wei),
            native_balance_updated=now,
        )
        np = prices.get("")
        if np is not None:
            cached.native_price_usd = str(np.price_usd)
            cached.native_price_updated = np.timestamp or now

        for b in tokens:
            addr = b.contract.lower()
            price = prices.get(addr)
            entry = entries.get((chain.chain_id, addr))
            cached.tokens.append(CachedToken(
                contract=addr, symbol=b.symbol, name=b.name,
                decimals=b.decimals,
                logo_uri=entry.logo_uri if entry else None,
                balance_raw=int(b.balance_raw),
                price_usd=str(price.price_usd) if price else None,
                balance_updated=now,
                price_updated=price.timestamp if price else 0,
            ))
        self._wallet_cache.save(cached)

    # --- panel signal handlers ---------------------------------------------

    def _on_hide_token(self, chain_id: int, contract: str) -> None:
        self._store.hide_token(chain_id, contract)
        self._invalidate_view_and_refresh()

    def _on_pin_token(self, chain_id: int, contract: str) -> None:
        self._store.force_show_token(chain_id, contract)
        self._invalidate_view_and_refresh()

    def _on_show_all_toggled(self, on: bool) -> None:
        self._show_all = on
        self._invalidate_view_and_refresh()

    def _on_add_custom_token(self) -> None:
        if self.host is None or self._panel is None:
            return
        chain = self.host.current_chain()
        addr, ok = prompt_text(
            self._panel, "Add custom token",
            f"Contract address on {chain.name} (0x… 40 hex chars):",
            wide=True,
        )
        if not ok:
            return
        addr = (addr or "").strip()
        if not (addr.startswith("0x") and len(addr) == 42):
            warn(
                self._panel, "Invalid address",
                "Expected a 0x-prefixed 40-character hex address.",
            )
            return
        try:
            int(addr[2:], 16)
        except ValueError:
            warn(
                self._panel, "Invalid address",
                "Address must be hexadecimal.",
            )
            return

        try:
            meta = EthClient(chain).multicall_erc20_metadata([addr])
        except Exception as e:
            warn(
                self._panel, "Read failed",
                f"Couldn't read ERC-20 metadata: {e}",
            )
            return
        if not meta:
            warn(
                self._panel, "Not a token",
                "Contract didn't respond to ERC-20 metadata calls "
                "(name/symbol/decimals). It might not be an ERC-20.",
            )
            return
        self._token_metadata.put_many(chain.chain_id, meta)
        # Track it (always balance-checked) but don't force-show: it appears
        # only once it has a non-zero balance, then hides again at exactly 0.
        self._store.add_custom_token(chain.chain_id, addr)

        m = next(iter(meta.values()))
        scam = self._token_lists.is_likely_scam(
            chain.chain_id, addr, m.get("symbol", ""), m.get("name", "")
        )
        if scam:
            warn(
                self._panel, "Heuristic warning",
                f"Added {m['symbol']!r} ({m['name']}). Heads up: it "
                "matches our scam heuristic (URL or impersonating a "
                "major symbol) and will be marked with an alarm icon. "
                "Tracked anyway since you added it explicitly.",
            )
        elif self.host is not None:
            # Feedback: a zero-balance custom token won't appear in the list,
            # so confirm it's being tracked rather than leaving the user
            # wondering why nothing changed.
            self.host.status_message(
                f"Tracking {m.get('symbol') or 'token'} — shows when it has a "
                "balance", 4000)
        self._invalidate_view_and_refresh()

    def _invalidate_view_and_refresh(self) -> None:
        """Force the next _refresh to do a full discovery round rather
        than short-circuiting on _discovery_in_flight / _displayed_view."""
        self._displayed_view = None
        self._discovery_in_flight.clear()
        if self.host is None:
            return
        addr = self.host.selected_address
        if addr is not None:
            self._refresh(addr)

    # --- token lists loader callbacks --------------------------------------

    def _on_lists_loaded(self) -> None:
        n = self._token_lists.count()
        if self.host is not None:
            self.host.status_message(
                f"Token lists loaded ({n} known tokens)", 3000
            )
        if self._panel is not None:
            # Hand the lists + risk cache to the panel so it can run the
            # combined scam check for the alarm-icon decision.
            self._panel._token_lists = self._token_lists
            self._panel._risk_cache = self._risk_cache
            # Share the unpriced-grace clock so the panel's display-time hiding
            # agrees with the discovery filter on when to drop a token we can't
            # value (else it'd linger on screen until the next discovery).
            self._panel._unpriced_grace = self._within_unpriced_grace
        # Prefill the on-chain metadata cache from the curated
        # lists. The curated entries already carry symbol/name/
        # decimals (the JSON we just downloaded HAS this) so
        # MetadataWorker can skip them entirely — without this,
        # adding curated tokens to the discovery set would trigger
        # a ~50-multicall metadata sweep on first refresh of each
        # chain. Permanent cache; one-shot work per session.
        self._prefill_metadata_from_token_lists()
        if self.host is None:
            return
        addr = self.host.selected_address
        if addr is not None:
            self._refresh(addr)
        elif self._panel is not None:
            self._panel.clear()
            self._displayed_view = None

    def _prefill_metadata_from_token_lists(self) -> None:
        """Push every (symbol, name, decimals) record from the
        curated token lists into the on-chain metadata cache so
        MetadataWorker has nothing to fetch for them later. Idempotent
        — TokenMetadataCache.put_many overwrites existing keys but
        the value is identical."""
        from ..chains import DEFAULT_CHAINS
        for chain in DEFAULT_CHAINS:
            meta: dict[str, dict] = {}
            for addr in self._token_lists.addresses_for_chain(chain.chain_id):
                entry = self._token_lists.get(chain.chain_id, addr)
                if entry is None:
                    continue
                meta[addr] = {
                    "symbol": entry.symbol,
                    "name": entry.name,
                    "decimals": entry.decimals,
                }
            if meta:
                self._token_metadata.put_many(chain.chain_id, meta)

    def _on_lists_load_failed(self, msg: str) -> None:
        if self._panel is not None:
            self._panel.show_message(f"Token lists failed: {msg}")




# --- panel + helpers (moved from qeth.ui) ----------------------------------

def _is_scam_via_lists(lists, chain_id: int, b: TokenBalance,
                       risk_cache=None) -> bool:
    risk = None
    if risk_cache is not None:
        risk = risk_cache.get(chain_id, b.contract)
    return lists.is_likely_scam(chain_id, b.contract, b.symbol, b.name, risk=risk)




class _NumericItem(QTableWidgetItem):
    """QTableWidgetItem that sorts numerically by an associated Decimal,
    regardless of the formatted display text. Falls back to string compare
    against non-numeric peers so heterogeneous columns still sort sanely."""

    def __init__(self, text: str, value: Decimal):
        super().__init__(text)
        self._value = value

    def set_value(self, value: Decimal) -> None:
        self._value = value

    def __lt__(self, other):
        if isinstance(other, _NumericItem):
            return self._value < other._value
        return super().__lt__(other)




class TokenListPanel(QWidget):
    """Right pane: native + held ERC-20s for the currently-selected account.

    Native row pinned at index 0; ERC-20s below, sorted by balance. Each row
    carries (chain_id, contract_or_empty) in the Symbol cell's UserRole so
    the icon cache + context menu can find the right row to act on.
    """

    # User asked to hide a specific (chain_id, contract). Empty contract means
    # the native asset row was clicked (no-op for now — can't hide native).
    hide_requested = Signal(QULONGLONG, str)
    # User wants to add a custom token by contract address.
    add_custom_requested = Signal()
    # User wants to pin (force-show) the currently-selected token.
    pin_requested = Signal(QULONGLONG, str)
    # User toggled the "show all" view (no dust, no hide-list — only scams
    # still hidden).
    show_all_toggled = Signal(bool)
    # User double-clicked a token row; carries (chain_id, contract).
    # Native rows don't emit (no token-transfers page for native).
    transfers_requested = Signal(QULONGLONG, str)
    # User clicked the Send button with a token row selected; carries
    # (chain_id, contract_or_empty_for_native). Plugin pops a Send
    # dialog with the user's recipient + amount + gas controls.
    send_requested = Signal(QULONGLONG, str)

    NATIVE_CONTRACT = ""  # sentinel for the native row

    DUST_USD_THRESHOLD = Decimal("0.01")

    def __init__(self, icon_cache: IconCache, store, parent=None,
                 chain_icon_getter=None):
        super().__init__(parent)
        self._icons = icon_cache
        self._icons.icon_ready.connect(self._on_icon_ready)
        self._store = store
        # Last prices applied to the Value column, for the set_prices
        # short-circuit + reapply_prices. Canonical annotation lives here so
        # show_balances / _remember_prices can reset it with a bare assignment.
        self._prices_state: dict = {}
        # Callable chain_id -> QPixmap|None; the native-row falls back to
        # the chain logo when the native symbol has no bundled icon.
        self._chain_icon_getter = chain_icon_getter

        v = QVBoxLayout(self)
        # No top margin so the table header aligns with the tree header on
        # the left side (which sits directly in the splitter, no wrapping).
        v.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Symbol", "Balance", "Value (USD)", "Name"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        # Disable focus rectangle on the table — many themes draw a 1px
        # focus border on the current cell which shifts contents on
        # hover/click. Selection still works without it.
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setShowGrid(False)
        # Pin padding + border explicitly for every state so no theme can
        # add an on-hover/on-selected border that shifts the text by 1px.
        # Selection still highlights the whole row (SelectRows + the rule
        # below). Hover style is set to match default so it produces no
        # visible change.
        # Selection paint is swapped between solid (focused) and
        # outlined (unfocused) by the focus-aware filter
        # installed in MainWindow. We seed the stylesheet here
        # with just padding + hover; the filter appends the
        # focused/unfocused selection rules.
        self.table.setStyleSheet(
            "QTableView::item {"
            "  padding: 3px 6px;"
            "  border: 0;"
            "}"
            "QTableView::item:hover { background: transparent; }"
        )
        self.table.setIconSize(QSize(20, 20))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        # Enter / Return on the focused tokens table opens the Send
        # dialog for the highlighted row — same as clicking the
        # Send button on the toolbar. Installed as an event filter
        # rather than a keyPressEvent override to avoid subclassing.
        self.table.installEventFilter(self)
        self.table.setSortingEnabled(True)
        # Default: by Value (USD) descending. setSortIndicator only sets the
        # arrow; the actual sort kicks in each time we toggle sortingEnabled
        # off-then-on around a populate/update cycle.
        h = self.table.horizontalHeader()
        h.setSortIndicator(2, Qt.SortOrder.DescendingOrder)
        # Interactive = user can drag the column edge. The Name column
        # stays Stretch so widening the window fills the gap instead
        # of leaving a void to the right.
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)  # Symbol
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)  # Balance
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)  # Value (USD)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)      # Name
        for col, width in enumerate((90, 120, 110, 0)):
            if width:
                h.resizeSection(col, width)
        v.addWidget(self.table, 1)

        # The +/-/★/👁 buttons are owned by this panel (so they can hook
        # into table selection and the panel's signals) but NOT added to
        # its layout — MainWindow places them on the shared bottom-right
        # action row alongside the chain selector, so we don't waste two
        # rows on what fits in one. ``action_widgets()`` exposes them.
        style = self.style()

        # Send button — labelled (icon + "Send") so it stands out
        # from the icon-only utility buttons to its right. Disabled
        # until a row is selected. Sends either the selected ERC-20
        # or the native asset (when the native row is selected).
        self.btn_send = QPushButton("&Send")
        # Right-arrow ("go-next"), shared with the ENS Transfer button + the
        # send/transfer composer confirm buttons so "move value out" reads the
        # same everywhere.
        _send_icon = QIcon.fromTheme(
            "go-next", style.standardIcon(QStyle.StandardPixmap.SP_ArrowForward))
        if _send_icon.isNull() or not _send_icon.availableSizes():
            # Fall back to a unicode arrow in environments whose icon theme
            # lacks even go-next.
            self.btn_send.setText("➤ &Send")
        else:
            self.btn_send.setIcon(_send_icon)
        self.btn_send.setToolTip("Send")
        self.btn_send.setEnabled(False)

        self.btn_add = QPushButton()
        self.btn_add.setIcon(QIcon.fromTheme("list-add",
                                             style.standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder)))
        self.btn_add.setToolTip("Add custom token")

        self.btn_hide = QPushButton()
        self.btn_hide.setIcon(QIcon.fromTheme("list-remove",
                                              style.standardIcon(QStyle.StandardPixmap.SP_TrashIcon)))
        self.btn_hide.setToolTip("Hide token")
        self.btn_hide.setEnabled(False)

        # Copy-contract button — mirrors the "Copy contract address"
        # item in the context menu so it's reachable from the toolbar
        # too (consistent with the wallet pane: every menu item has a
        # button equivalent and vice versa).
        self.btn_copy = QPushButton()
        self.btn_copy.setIcon(QIcon.fromTheme("edit-copy",
                                              style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)))
        self.btn_copy.setToolTip("Copy address")
        self.btn_copy.setEnabled(False)

        self.btn_pin = QPushButton()
        _pin_icon = QIcon.fromTheme("emblem-favorite",
                                    QIcon.fromTheme("starred"))
        if _pin_icon.isNull() or not _pin_icon.availableSizes():
            self.btn_pin.setText("★")    # Unicode star, reliable on any system
        else:
            self.btn_pin.setIcon(_pin_icon)
        self.btn_pin.setToolTip("Pin — always show")
        self.btn_pin.setEnabled(False)

        self.btn_show_all = QPushButton()
        _eye_icon = QIcon.fromTheme("view-visible",
                                    QIcon.fromTheme("eye-symbolic"))
        if _eye_icon.isNull() or not _eye_icon.availableSizes():
            self.btn_show_all.setText("👁")  # Unicode eye fallback
        else:
            self.btn_show_all.setIcon(_eye_icon)
        self.btn_show_all.setToolTip("Show all (incl. dust)")
        self.btn_show_all.setCheckable(True)

        for b in (self.btn_add, self.btn_copy, self.btn_hide,
                  self.btn_pin, self.btn_show_all):
            b.setFlat(True)
            b.setMaximumSize(28, 28)
            b.setIconSize(QSize(16, 16))
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        # Send is intentionally NOT flat-and-square — it carries a
        # text label and gets default button sizing so it reads as
        # a primary action vs the icon-only utility row.
        self.btn_send.setIconSize(QSize(16, 16))

        self.btn_send.clicked.connect(
            lambda: self._emit_for_selected(self.send_requested)
        )
        self.btn_add.clicked.connect(self.add_custom_requested.emit)
        self.btn_copy.clicked.connect(self._copy_selected_contract)
        # Ctrl+C copies the selected token's contract address, scoped to
        # the table so it only fires when this tab has focus.
        copy_act = QAction(self.table)
        copy_act.setShortcut(QKeySequence.StandardKey.Copy)
        copy_act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy_act.triggered.connect(self._copy_selected_contract)
        self.table.addAction(copy_act)
        self.btn_hide.clicked.connect(
            lambda: self._emit_for_selected(self.hide_requested)
        )
        self.btn_pin.clicked.connect(
            lambda: self._emit_for_selected(self.pin_requested)
        )
        self.btn_show_all.toggled.connect(self.show_all_toggled.emit)
        self.table.itemSelectionChanged.connect(self._update_action_buttons)

        # current chain (set by show_balances) — needed to scope icon lookups
        # and context-menu actions.
        self._chain_id: int | None = None
        # Set externally by MainWindow so we can mark scams with an alarm
        # icon and short-circuit `is_likely_scam` against the curated lists.
        self._token_lists: TokenLists | None = None
        # Injected by TokensPlugin: (chain_id, addr) → is this recognised-but-
        # unpriced token still inside its show-then-hide grace window? None until
        # wired (then known_pending shows unpriced recognised tokens forever).
        self._unpriced_grace: Callable[[int, str], bool] | None = None
        # Same — MainWindow injects so the alarm icon also reflects GoPlus
        # high-risk verdicts (honeypot / hidden owner / >50% sell tax).
        self._risk_cache: RiskCache | None = None

    def action_widgets(self) -> list[QWidget]:
        """The button strip, in display order. MainWindow mounts them
        on the shared bottom-right row beside the chain selector.
        Send is leftmost (labelled, primary action); the icon-only
        utility buttons follow."""
        return [self.btn_send, self.btn_add, self.btn_copy, self.btn_hide,
                self.btn_pin, self.btn_show_all]

    def header_state(self) -> str:
        """Hex-encoded QHeaderView.saveState() — captures column widths,
        order, and the active sort indicator. Persisted by MainWindow."""
        return bytes(
            self.table.horizontalHeader().saveState().toHex().data()
        ).decode()

    def restore_header_state(self, state_hex: str) -> None:
        if not state_hex:
            return
        try:
            from PySide6.QtCore import QByteArray
            self.table.horizontalHeader().restoreState(
                QByteArray.fromHex(state_hex.encode())
            )
        except Exception:
            pass

    # ---- displaying data -------------------------------------------------

    def show_loading(self, address: str) -> None:
        self.table.setRowCount(0)

    def show_balances(
        self,
        chain,
        native_wei: int,
        tokens: list[TokenBalance],
        list_entries: dict,    # (chain_id, addr_lower) -> TokenListEntry
    ) -> None:
        """Populate the table with the native asset on top, then ERC-20s.

        Caller is responsible for wrapping this (and any subsequent
        set_prices) in ``setUpdatesEnabled(False/True)`` when invoked
        during a refresh — otherwise the row-count change can produce a
        brief blank frame between rows being resized and cells being
        re-populated. See ``render_full`` for the safe one-shot helper.
        """
        self._chain_id = chain.chain_id
        # Disable sorting while populating; re-enabling at the end triggers
        # a single sort by the current header indicator.
        self.table.setSortingEnabled(False)
        # Set the new row count directly. If smaller than current, Qt
        # truncates from the bottom; if larger, it appends empty rows. Old
        # cells in surviving rows persist until we overwrite them just
        # below, avoiding the all-blank moment that setRowCount(0) would
        # cause. Callers that need to fully replace contents still get
        # correct results.
        row_count = 1 + len(tokens)
        self.table.setRowCount(row_count)

        # Remember per-row Decimal balances so set_prices can multiply
        # without re-parsing the displayed text.
        self._balances: dict[tuple[int, str], Decimal] = {}
        # We just rebuilt every row with an EMPTY Value cell. set_prices
        # short-circuits when the incoming prices equal _prices_state — so a
        # re-render with unchanged prices (show_cached on a wallet switch, the
        # on_activated re-render) would leave the Value column blank. Reset the
        # baseline so the set_prices that render_full runs next always paints.
        self._prices_state = {}

        # --- native row ---------------------------------------------------
        native_balance = wei_to_ether(native_wei)
        self._balances[(chain.chain_id, self.NATIVE_CONTRACT)] = native_balance
        # Remembered so a chain-icon-ready signal can fill the native row's
        # icon later (the cache fetch is async).
        self._native_chain_id = chain.chain_id
        self._native_symbol = chain.symbol
        sym = QTableWidgetItem(chain.symbol)
        sym.setData(Qt.ItemDataRole.UserRole, (chain.chain_id, self.NATIVE_CONTRACT))
        sym.setToolTip(f"Native {chain.symbol} on {chain.name}")
        bf = sym.font(); bf.setBold(True); sym.setFont(bf)
        native_pix = bundled_native_icon(chain.symbol)
        if native_pix is None and self._chain_icon_getter is not None:
            # No bundled icon for this native (AVAX/BNB/XDAI/…): the native
            # asset's logo is the chain's own logo, which the chain-icon
            # cache fetches from Curve/TrustWallet. If it isn't cached yet
            # the getter kicks the fetch and returns None; update_native_icon
            # fills it in when chain-icon-ready fires.
            native_pix = self._chain_icon_getter(chain.chain_id)
        if native_pix is not None:
            sym.setIcon(smooth_icon(native_pix))
        bal = _NumericItem(_format_balance(native_balance), native_balance)
        bal.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bal.setFont(bf)
        val = _NumericItem("", Decimal(0))
        val.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val.setFont(bf)
        name = QTableWidgetItem(chain.name)
        name.setFont(bf)
        self.table.setItem(0, 0, sym)
        self.table.setItem(0, 1, bal)
        self.table.setItem(0, 2, val)
        self.table.setItem(0, 3, name)

        # --- ERC-20 rows --------------------------------------------------
        alarm_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        for row, b in enumerate(tokens, start=1):
            key = (chain.chain_id, b.contract.lower())
            self._balances[key] = b.balance
            entry = list_entries.get(key)
            sym = QTableWidgetItem(b.symbol)
            sym.setData(Qt.ItemDataRole.UserRole, key)
            sym.setToolTip(b.contract)
            # Mark suspected scams with an alarm. Most of the time these
            # don't reach the panel at all (filtered upstream); the case
            # that survives is "user force-shows a contract that fails
            # the heuristic" — exactly when a warning is most useful.
            scam = (
                self._token_lists is not None
                and _is_scam_via_lists(
                    self._token_lists, chain.chain_id, b,
                    risk_cache=self._risk_cache,
                )
            )
            if scam:
                sym.setIcon(alarm_icon)
                sym.setToolTip(b.contract + "\n⚠ Suspected scam")
            else:
                pix = self._icons.get(chain.chain_id, b.contract)
                if pix is not None:
                    sym.setIcon(smooth_icon(pix))
                elif entry and entry.logo_uri:
                    self._icons.request(chain.chain_id, b.contract, entry.logo_uri)
            bal = _NumericItem(_format_balance(b.balance), b.balance)
            bal.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val = _NumericItem("", Decimal(0))
            val.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            name = QTableWidgetItem(b.name)
            self.table.setItem(row, 0, sym)
            self.table.setItem(row, 1, bal)
            self.table.setItem(row, 2, val)
            self.table.setItem(row, 3, name)

        self.table.setSortingEnabled(True)

    def update_native_icon(self, chain_id: int, pix) -> None:
        """Fill the native row's icon when its chain logo finishes fetching
        (async). No-op if a different chain is shown, the native symbol has
        a bundled icon, or the row isn't present. Found by UserRole, not
        row index — the user may have sorted the native row off the top."""
        if pix is None or pix.isNull():
            return
        if chain_id != getattr(self, "_native_chain_id", None):
            return
        if bundled_native_icon(getattr(self, "_native_symbol", "") or ""):
            return   # bundled icon already set; don't override
        target = (chain_id, self.NATIVE_CONTRACT)
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it is not None and it.data(Qt.ItemDataRole.UserRole) == target:
                it.setIcon(smooth_icon(pix))
                return

    def render_full(self, chain, native_wei: int, tokens: list[TokenBalance],
                    entries: dict, prices: dict,
                    apply_dust_filter: bool = True) -> None:
        """Atomic full re-render: rows + balances + prices in one go,
        with paint events suspended so the user never sees an
        intermediate blank or pre-price-filter state.

        ``apply_dust_filter`` is forwarded to set_prices — spotlight
        mode passes False so zero-value / unpriced rows stay visible.
        """
        self.setUpdatesEnabled(False)
        try:
            self.show_balances(chain, native_wei, tokens, entries)
            self.set_prices(chain.chain_id, prices,
                            apply_dust_filter=apply_dust_filter)
        finally:
            self.setUpdatesEnabled(True)

    def show_cached(self, chain, cached: CachedWallet) -> None:
        """Render immediately from a cached wallet snapshot. Reuses the
        normal show_balances + set_prices code paths so display + sort +
        dust filter behave identically to a fresh fetch."""
        tokens: list[TokenBalance] = [
            TokenBalance(
                contract=t.contract, symbol=t.symbol, name=t.name,
                decimals=t.decimals, balance_raw=t.balance_raw,
            )
            for t in cached.tokens
        ]
        entries: dict = {}
        for t in cached.tokens:
            if t.logo_uri:
                entries[(chain.chain_id, t.contract.lower())] = TokenListEntry(
                    chain_id=chain.chain_id, address=t.contract.lower(),
                    symbol=t.symbol, name=t.name, decimals=t.decimals,
                    source="cache", logo_uri=t.logo_uri,
                )
        prices: dict = {}
        if cached.native_price_usd:
            prices[""] = Price(
                Decimal(cached.native_price_usd),
                cached.native_price_updated, "cache",
            )
        for t in cached.tokens:
            if t.price_usd:
                prices[t.contract.lower()] = Price(
                    Decimal(t.price_usd), t.price_updated, "cache",
                )
        self.render_full(chain, cached.native_balance_wei, tokens, entries, prices)

    def contract_set_matches(self, chain, tokens: list[TokenBalance]) -> bool:
        """True if the panel currently displays exactly the contract set
        described by ``tokens`` (plus the native row). Lets callers decide
        between in-place updates and rebuilds without mutating cells."""
        if self._chain_id != chain.chain_id:
            return False
        expected = {(chain.chain_id, self.NATIVE_CONTRACT)} | {
            (chain.chain_id, b.contract.lower()) for b in tokens
        }
        return set(self._balances.keys()) == expected

    def update_balances_if_set_unchanged(
        self,
        chain,
        native_wei: int,
        tokens: list[TokenBalance],
    ) -> bool:
        """If the displayed contract set matches the new fetch, update
        balance cells in place. Only cells whose value actually differs
        are mutated, and the sort toggle is skipped entirely when nothing
        changed — both to prevent gratuitous repaints/reorders that the
        user perceives as flicker. Returns False to tell the caller
        "fall back to show_balances rebuild" when contracts changed."""
        if not self.contract_set_matches(chain, tokens):
            return False
        new_native = wei_to_ether(native_wei)
        by_addr = {b.contract.lower(): b for b in tokens}

        # First pass: collect what would change without mutating anything.
        changes: list[tuple[int, tuple[int, str], Decimal]] = []
        for row in range(self.table.rowCount()):
            sym = self.table.item(row, 0)
            if sym is None:
                continue
            key = sym.data(Qt.ItemDataRole.UserRole)
            if not key:
                continue
            _, addr = key
            if addr == self.NATIVE_CONTRACT:
                new_value = new_native
            else:
                b = by_addr.get(addr)
                if b is None:
                    continue
                new_value = b.balance
            if self._balances.get(key) != new_value:
                changes.append((row, key, new_value))

        if not changes:
            # Set matches, all balances identical to what's already shown.
            # Touch nothing, don't toggle sort.
            return True

        self.table.setSortingEnabled(False)
        for row, key, value in changes:
            self._balances[key] = value
            bal_cell = self.table.item(row, 1)
            if bal_cell is None:
                continue
            bal_cell.setText(_format_balance(value))
            if isinstance(bal_cell, _NumericItem):
                bal_cell.set_value(value)
        self.table.setSortingEnabled(True)
        return True

    def show_error(self, msg: str) -> None:
        self.table.setRowCount(0)
        self._chain_id = None
        self._balances = {}

    def show_message(self, msg: str) -> None:
        self.table.setRowCount(0)
        self._chain_id = None
        self._balances = {}

    def clear(self) -> None:
        self.table.setRowCount(0)
        self._chain_id = None
        self._balances = {}

    def _remember_prices(self, prices: dict) -> None:
        if not hasattr(self, "_prices_state"):
            self._prices_state = {}
        for k, v in prices.items():
            self._prices_state[k] = v

    def reapply_prices(self) -> None:
        """Recompute the Value column using the most recent prices we have.

        Called after balance-only refreshes so the value cells aren't stale
        relative to the new balances. Crucially: does NOT re-apply the
        dust filter. The dust check is only meaningful with fresh DefiLlama
        prices (which only arrive at combined-update time); re-running it
        here against stale cached prices would just oscillate borderline
        tokens between visible/hidden."""
        if self._chain_id is None:
            return
        cached_prices = getattr(self, "_prices_state", {}) or {}
        if cached_prices:
            # force=True: the prices are unchanged (that's why they're cached),
            # but a BALANCE just changed, so Value (= balance × price) must be
            # recomputed. Without the force the set_prices short-circuit would
            # see identical prices and skip the recompute, leaving a stale USD
            # next to the freshly-updated balance.
            self.set_prices(self._chain_id, cached_prices,
                            apply_dust_filter=False, force=True)

    def set_prices(self, chain_id: int, prices: dict,
                   apply_dust_filter: bool = True, force: bool = False) -> None:
        """Populate the Value (USD) column from a {addr_lower: Price} dict
        and hide rows whose value falls below the dust threshold.

        Visibility rules:
        - Native row: always shown.
        - Force-shown ERC-20 (user override): always shown.
        - Priced ERC-20 with value < DUST_USD_THRESHOLD: hidden.
        - ERC-20 with no price quote: hidden (treated as zero — if it
          mattered, the user can force-show it).

        Sorting is suspended while we mutate cells, then re-enabled so the
        table re-sorts once by the current header indicator (Value desc by
        default; whatever the user clicked otherwise)."""
        if self._chain_id != chain_id:
            return
        # Short-circuit when the price values match the last call —
        # nothing in the table would change, so don't even toggle sort
        # (which causes Qt to repaint visible rows even when no order
        # change is needed).
        stored = getattr(self, "_prices_state", None) or {}
        if not force and stored and set(stored.keys()) == set(prices.keys()) and all(
            stored[k].price_usd == prices[k].price_usd for k in prices
        ):
            return
        self._remember_prices(prices)
        self.table.setSortingEnabled(False)
        for row in range(self.table.rowCount()):
            sym = self.table.item(row, 0)
            if sym is None:
                continue
            key = sym.data(Qt.ItemDataRole.UserRole)
            if not key:
                continue
            cid, addr = key
            is_native = (addr == self.NATIVE_CONTRACT)

            balance = self._balances.get(key)
            price = prices.get(addr)  # native lives under ""
            cell = self.table.item(row, 2)
            value: Decimal | None = None
            if cell is not None and balance is not None and price is not None:
                value = balance * price.price_usd
                cell.setText(_format_usd(value))
                if isinstance(cell, _NumericItem):
                    cell.set_value(value)

            if apply_dust_filter:
                # Display-time hiding (doesn't touch the worker data):
                #   - native always shows;
                #   - an EXACTLY-zero balance never shows — even pinned/custom
                #     ("pin"/"add" mean show-when-held, not show-a-zero);
                #   - otherwise show if pinned, custom-added, above the dust
                #     USD threshold, OR a RECOGNISED token whose price hasn't
                #     loaded yet. The last clause is what makes a just-received
                #     curated token (a swap's output, e.g. WETH) appear at once
                #     instead of staying hidden — the targeted read adds it to
                #     the cache without a price, and "no price → hide" is only
                #     meant for unrecognised spam, not a token in the lists.
                #     Once it IS priced the normal dust rule takes over.
                is_zero = balance is not None and balance == 0
                known_pending = (
                    price is None
                    and self._token_lists is not None
                    and self._token_lists.is_known(cid, addr)
                    # ...but only while inside the grace window — a recognised
                    # token the price source can't value (sUSD/EURT) stops
                    # showing once its window lapses (pin it to keep it).
                    and (self._unpriced_grace is None
                         or self._unpriced_grace(cid, addr)))
                show = is_native or (not is_zero and (
                    self._store.is_force_shown(cid, addr)
                    or self._store.is_custom_token(cid, addr)
                    or known_pending
                    or (value is not None and value >= self.DUST_USD_THRESHOLD)))
                self.table.setRowHidden(row, not show)
        self.table.setSortingEnabled(True)

    # ---- icon refresh ---------------------------------------------------

    def _on_icon_ready(self, chain_id: int, contract: str) -> None:
        if self._chain_id != chain_id:
            return
        pix = self._icons.get(chain_id, contract)
        if pix is None:
            return
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            cid, addr = item.data(Qt.ItemDataRole.UserRole) or (None, None)
            if cid == chain_id and addr == contract.lower():
                item.setIcon(smooth_icon(pix))
                break

    # ---- action buttons --------------------------------------------------

    def _selected_token(self) -> tuple[int, str] | None:
        """``(chain_id, contract_lower)`` of the currently-selected ERC-20
        row, or None when nothing is selected / the native row is."""
        items = self.table.selectedItems()
        if not items:
            return None
        sym = self.table.item(items[0].row(), 0)
        if sym is None:
            return None
        key = sym.data(Qt.ItemDataRole.UserRole)
        if not key or key[1] == self.NATIVE_CONTRACT:
            return None
        return key

    def _selected_any(self) -> tuple[int, str] | None:
        """Like ``_selected_token`` but doesn't filter the native
        row out — used by Send, which is meaningful for both
        ERC-20s and the native asset. Returns
        ``(chain_id, NATIVE_CONTRACT)`` for the native row."""
        items = self.table.selectedItems()
        if not items:
            return None
        sym = self.table.item(items[0].row(), 0)
        if sym is None:
            return None
        key = sym.data(Qt.ItemDataRole.UserRole)
        return key or None

    def _emit_for_selected(self, sig) -> None:
        # ``send_requested`` uses _selected_any (native + ERC-20);
        # every other connected signal sticks to _selected_token
        # (ERC-20 only).
        if sig is self.send_requested:
            sel = self._selected_any()
        else:
            sel = self._selected_token()
        if sel:
            sig.emit(sel[0], sel[1])

    def _update_action_buttons(self) -> None:
        enabled = self._selected_token() is not None
        any_selected = self._selected_any() is not None
        self.btn_copy.setEnabled(enabled)
        self.btn_hide.setEnabled(enabled)
        self.btn_pin.setEnabled(enabled)
        # Send works on both ERC-20 and the native row, so it's
        # enabled whenever ANY row is selected.
        self.btn_send.setEnabled(any_selected)

    def _copy_selected_contract(self) -> None:
        sel = self._selected_token()
        if sel:
            QApplication.clipboard().setText(sel[1])

    # ---- context menu ---------------------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802 — Qt method name
        from PySide6.QtCore import QEvent
        if (obj is self.table
                and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)):
            self._emit_for_selected(self.send_requested)
            return True
        return super().eventFilter(obj, event)

    def _on_cell_double_clicked(self, row: int, _col: int) -> None:
        """Emit ``transfers_requested`` for non-native rows. The
        plugin builds the explorer URL and opens it."""
        sym_item = self.table.item(row, 0)
        if sym_item is None:
            return
        meta = sym_item.data(Qt.ItemDataRole.UserRole)
        if not meta:
            return
        cid, addr = meta
        if addr == self.NATIVE_CONTRACT:
            return
        self.transfers_requested.emit(cid, addr)

    def _on_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        sym_item = self.table.item(item.row(), 0)
        if sym_item is None:
            return
        meta = sym_item.data(Qt.ItemDataRole.UserRole)
        if not meta:
            return
        cid, addr = meta
        is_native = (addr == self.NATIVE_CONTRACT)
        menu = QMenu(self)
        # Convention: the menu mirrors the panel's action buttons for the
        # clicked row. Send is the primary action and works for both the
        # native asset and ERC-20s, so it heads every row's menu (the native
        # row previously got no menu at all — only Send applies to it).
        act_send = menu.addAction(self.btn_send.icon(), f"Send {sym_item.text()}")
        # The remaining items are ERC-20-only: the native asset has no
        # contract address to copy and can't be pinned/hidden.
        act_copy = act_pin = act_hide = None
        if not is_native:
            menu.addSeparator()
            act_copy = menu.addAction(
                self.btn_copy.icon(), "Copy Contract Address")
            # Pin is one-shot (no unpin UI yet); skip it for already-pinned
            # tokens so the menu doesn't suggest a no-op.
            if not self._store.is_force_shown(cid, addr):
                act_pin = menu.addAction(
                    self.btn_pin.icon(), f"Pin {sym_item.text()}")
            act_hide = menu.addAction(
                self.btn_hide.icon(), f"Hide {sym_item.text()}")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_send:
            self.send_requested.emit(cid, addr)
        elif chosen is act_hide:
            self.hide_requested.emit(cid, addr)
        elif chosen is act_copy:
            QApplication.clipboard().setText(addr)
        elif chosen is act_pin and act_pin is not None:
            self.pin_requested.emit(cid, addr)


# --- Transaction history panel + worker -------------------------------------

# Light decoding for the most common selectors. Anything not here renders
# as the raw 10-char selector — better than guessing wrong on a name.
