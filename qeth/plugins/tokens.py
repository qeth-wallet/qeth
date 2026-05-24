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
from decimal import Decimal
from typing import Optional

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QInputDialog,
    QMenu, QMessageBox, QPushButton, QSizePolicy, QStyle, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..chain import EthClient, wei_to_ether
from ..formatting import format_balance as _format_balance
from ..formatting import format_usd as _format_usd
from ..icons import IconCache, bundled_native_icon
from ..plugin import Plugin
from ..prices import DefiLlamaPrices, Price, PriceSource
from ..risk import GoPlusRisk, RiskCache
from ..token_metadata import TokenMetadataCache
from ..tokenlists import TokenListEntry, TokenLists
from ..tokens import BlockscoutSource, TokenBalance
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


class TokenListWorker(QThread):
    """Fetch native + ERC-20 balances for (chain, address) and apply the
    visibility rules: hidden tokens are dropped; only known-or-force-shown
    ERC-20s are kept; the native asset is always returned as the first
    element so the UI can render it on top.

    Emits ``fetched(native_wei: int, tokens: list[TokenBalance])``."""

    # native_wei must travel as ``object``; declaring ``int`` makes PySide6
    # marshal through qint32 and overflows for any balance above ~2.1e9 wei
    # (well below a millionth of an ETH).
    fetched = Signal(object, list)
    failed = Signal(str)

    def __init__(self, chain, address: str, source: BlockscoutSource,
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

    fetched = Signal(int, object)
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

    fetched = Signal(int, object)
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
    refreshed = Signal(int, object, object)
    failed = Signal(str)

    def __init__(self, chain, address: str, token_contracts: list[str], parent=None):
        super().__init__(parent)
        self.chain = chain
        self.address = address
        self.contracts = list(token_contracts)

    def run(self) -> None:
        try:
            client = EthClient(self.chain)
            native = client.get_balance(self.address)
            balances = client.multicall_erc20_balances(self.contracts, self.address) if self.contracts else {}
            self.refreshed.emit(self.chain.chain_id, native, balances)
        except Exception as e:
            self.failed.emit(str(e))


class PricesWorker(QThread):
    """Fetch USD prices for the currently-displayed assets."""

    prices_ready = Signal(int, object)

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

    def __init__(self, store):
        super().__init__()
        self._store = store
        # Sources (constructed once, reused across refreshes).
        self._token_source = BlockscoutSource()
        self._token_lists = TokenLists()
        self._icon_cache = IconCache()
        self._price_source: PriceSource = DefiLlamaPrices()
        self._wallet_cache = WalletCache()
        self._token_metadata = TokenMetadataCache()
        self._risk_source = GoPlusRisk()
        self._risk_cache = RiskCache()
        # Display state.
        self._show_all = False
        self._displayed_view: Optional[tuple[int, str]] = None
        self._discovery_in_flight: set[tuple[int, str]] = set()
        # Lifecycle objects (built lazily / in attach).
        self._panel = None
        self._refresh_timer: Optional[QTimer] = None
        self._lists_loader: Optional[TokenListsLoader] = None

    # --- Plugin contract ----------------------------------------------------

    @property
    def token_panel(self):
        """Alias used by tests + transitional MainWindow code."""
        return self.widget()

    def widget(self) -> QWidget:
        if self._panel is None:
            self._panel = TokenListPanel(self._icon_cache, self._store)
            self._panel.hide_requested.connect(self._on_hide_token)
            self._panel.pin_requested.connect(self._on_pin_token)
            self._panel.add_custom_requested.connect(self._on_add_custom_token)
            self._panel.show_all_toggled.connect(self._on_show_all_toggled)
        return self._panel

    def action_widgets(self):
        if self._panel is None:
            return []
        return self._panel.action_widgets()

    def attach(self, host) -> None:
        super().attach(host)
        # 60-second background refresh against whatever view is on screen.
        # _on_refresh_tick self-dedupes via _discovery_in_flight, so an
        # unfinished run blocks the next tick rather than stacking.
        self._refresh_timer = QTimer()
        self._refresh_timer.setInterval(self.REFRESH_INTERVAL_MS)
        self._refresh_timer.timeout.connect(self._on_refresh_tick)
        self._refresh_timer.start()
        # Kick the curated token-lists loader. Host tracks the worker so
        # it isn't GC'd while running.
        self._lists_loader = TokenListsLoader(self._token_lists)
        self._lists_loader.loaded.connect(self._on_lists_loaded)
        self._lists_loader.failed.connect(self._on_lists_load_failed)
        host.start_worker(self._lists_loader)

    # --- lifecycle hooks ----------------------------------------------------

    def on_account_changed(self, address: Optional[str]) -> None:
        if address is None:
            if self._panel is not None:
                self._panel.clear()
            self._displayed_view = None
            return
        self._refresh(address)

    def on_chain_changed(self) -> None:
        if self.host is None:
            return
        addr = self.host.selected_address
        if addr is not None:
            self._refresh(addr)

    # --- core refresh pipeline ---------------------------------------------

    def _refresh(self, address: str) -> None:
        if self.host is None or self._panel is None:
            return
        chain = self.host.current_chain()
        view_key = (chain.chain_id, address.lower())
        is_new_view = self._displayed_view != view_key

        cached = self._wallet_cache.load(chain.chain_id, address)
        if cached is not None and is_new_view:
            # Immediate render from cache; no flicker while we refresh.
            self._panel.show_cached(chain, cached)
            self._displayed_view = view_key
            # Only kick off a multicall balance refresh once per view;
            # repeated _refresh calls for the same view don't need
            # another round-trip.
            bw = BalanceWorker(
                chain, address, [t.contract for t in cached.tokens],
            )
            bw.refreshed.connect(self._on_balance_refresh)
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
            # Always include force-shown contracts in the multicall set,
            # even if Blockscout didn't return them.
            forced = {a for (cid, a) in self._store.shown_tokens
                      if cid == chain.chain_id}
            seen = set()
            contracts: list[str] = []
            for c in [b.contract for b in blockscout_tokens] + sorted(forced):
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
                self.host.start_worker(pw)

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
                self.host.start_worker(rw)

            def on_balances(cid: int, mc_native, mc_balances: dict) -> None:
                pv["native_wei"] = int(mc_native)
                pv["balances_raw"] = {k.lower(): int(v) for k, v in mc_balances.items()}
                pv["metadata"] = build_metadata()
                kick_risk_then_prices()

            def on_balances_fail(msg: str) -> None:
                log.warning("post-discovery multicall failed: %s", msg)
                pv["native_wei"] = int(blockscout_native_wei)
                pv["balances_raw"] = {
                    b.contract.lower(): int(b.balance_raw) for b in blockscout_tokens
                }
                pv["metadata"] = build_metadata()
                kick_risk_then_prices()

            def kick_balance_multicall() -> None:
                bw = BalanceWorker(chain, address, contracts)
                bw.refreshed.connect(on_balances)
                bw.failed.connect(on_balances_fail)
                self.host.start_worker(bw)

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
            self.host.start_worker(mw)

        def on_failed(msg: str) -> None:
            self._discovery_in_flight.discard(view_key)
            if self._panel._chain_id is None and self._displayed_view == view_key:
                self._panel.show_error(msg)

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

        tokens: list[TokenBalance] = []
        for addr, raw in balances_raw.items():
            # Drop zero balances unless force-shown or in spotlight mode.
            if raw == 0:
                if not (self._show_all
                        or self._store.is_force_shown(chain.chain_id, addr)):
                    continue
            meta = metadata.get(addr)
            if meta is None:
                continue
            sym, name, decimals = meta
            tokens.append(TokenBalance(
                contract=addr, symbol=sym, name=name,
                decimals=decimals, balance_raw=raw,
            ))

        entries = {
            (chain.chain_id, b.contract.lower()): e
            for b in tokens
            if (e := self._token_lists.get(chain.chain_id, b.contract)) is not None
        }

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

        # Cache only the normal-mode visible set (post-dust + force-show);
        # never persist the spotlight superset.
        cache_visible = (
            self._compute_visible_tokens(chain, tokens, prices, show_all=False)
            if self._show_all else visible
        )
        self._save_wallet_cache(chain, address, native_wei, cache_visible, prices, entries)

    def _compute_visible_tokens(self, chain, tokens: list, prices,
                                show_all: bool | None = None) -> list:
        """Apply dust + force-show filter and sort by USD value desc."""
        if show_all is None:
            show_all = self._show_all
        dust = TokenListPanel.DUST_USD_THRESHOLD
        out = []
        for b in tokens:
            addr = b.contract.lower()
            if show_all:
                out.append(b)
                continue
            if self._store.is_force_shown(chain.chain_id, addr):
                out.append(b)
                continue
            price = prices.get(addr)
            if price is None:
                continue
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

    def _on_balance_refresh(self, chain_id: int, native_wei, balances_raw: dict) -> None:
        """Fast in-place balance refresh for the cached set, ahead of
        the slower discovery+prices chain."""
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
        addr, ok = QInputDialog.getText(
            self._panel, "Add custom token",
            f"Contract address on {chain.name} (0x… 40 hex chars):",
        )
        if not ok:
            return
        addr = (addr or "").strip()
        if not (addr.startswith("0x") and len(addr) == 42):
            QMessageBox.warning(
                self._panel, "Invalid address",
                "Expected a 0x-prefixed 40-character hex address.",
            )
            return
        try:
            int(addr[2:], 16)
        except ValueError:
            QMessageBox.warning(
                self._panel, "Invalid address",
                "Address must be hexadecimal.",
            )
            return

        try:
            meta = EthClient(chain).multicall_erc20_metadata([addr])
        except Exception as e:
            QMessageBox.warning(
                self._panel, "Read failed",
                f"Couldn't read ERC-20 metadata: {e}",
            )
            return
        if not meta:
            QMessageBox.warning(
                self._panel, "Not a token",
                "Contract didn't respond to ERC-20 metadata calls "
                "(name/symbol/decimals). It might not be an ERC-20.",
            )
            return
        self._token_metadata.put_many(chain.chain_id, meta)
        self._store.force_show_token(chain.chain_id, addr)

        m = next(iter(meta.values()))
        scam = self._token_lists.is_likely_scam(
            chain.chain_id, addr, m.get("symbol", ""), m.get("name", "")
        )
        if scam:
            QMessageBox.warning(
                self._panel, "Heuristic warning",
                f"Added {m['symbol']!r} ({m['name']}). Heads up: it "
                "matches our scam heuristic (URL or impersonating a "
                "major symbol) and will be marked with an alarm icon. "
                "Pinned anyway since you added it explicitly.",
            )
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
        if self.host is None:
            return
        addr = self.host.selected_address
        if addr is not None:
            self._refresh(addr)
        elif self._panel is not None:
            self._panel.clear()
            self._displayed_view = None

    def _on_lists_load_failed(self, msg: str) -> None:
        if self._panel is not None:
            self._panel.show_message(f"Token lists failed: {msg}")


from decimal import Decimal


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
    hide_requested = Signal(int, str)
    # User wants to add a custom token by contract address.
    add_custom_requested = Signal()
    # User wants to pin (force-show) the currently-selected token.
    pin_requested = Signal(int, str)
    # User toggled the "show all" view (no dust, no hide-list — only scams
    # still hidden).
    show_all_toggled = Signal(bool)

    NATIVE_CONTRACT = ""  # sentinel for the native row

    DUST_USD_THRESHOLD = Decimal("0.01")

    def __init__(self, icon_cache: IconCache, store, parent=None):
        super().__init__(parent)
        self._icons = icon_cache
        self._icons.icon_ready.connect(self._on_icon_ready)
        self._store = store

        v = QVBoxLayout(self)
        # No top margin so the table header aligns with the tree header on
        # the left side (which sits directly in the splitter, no wrapping).
        v.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Symbol", "Balance", "Value (USD)", "Name"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        # Disable focus rectangle on the table — many themes draw a 1px
        # focus border on the current cell which shifts contents on
        # hover/click. Selection still works without it.
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setShowGrid(False)
        # Pin padding + border explicitly for every state so no theme can
        # add an on-hover/on-selected border that shifts the text by 1px.
        # Selection still highlights the whole row (SelectRows + the rule
        # below). Hover style is set to match default so it produces no
        # visible change.
        self.table.setStyleSheet(
            "QTableView::item {"
            "  padding: 3px 6px;"
            "  border: 0;"
            "}"
            "QTableView::item:hover { background: transparent; }"
            "QTableView::item:selected,"
            "QTableView::item:selected:hover {"
            "  background: palette(highlight);"
            "  color: palette(highlighted-text);"
            "}"
        )
        self.table.setIconSize(QSize(20, 20))
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.setSortingEnabled(True)
        # Default: by Value (USD) descending. setSortIndicator only sets the
        # arrow; the actual sort kicks in each time we toggle sortingEnabled
        # off-then-on around a populate/update cycle.
        h = self.table.horizontalHeader()
        h.setSortIndicator(2, Qt.DescendingOrder)
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        v.addWidget(self.table, 1)

        # The +/-/★/👁 buttons are owned by this panel (so they can hook
        # into table selection and the panel's signals) but NOT added to
        # its layout — MainWindow places them on the shared bottom-right
        # action row alongside the chain selector, so we don't waste two
        # rows on what fits in one. ``action_widgets()`` exposes them.
        style = self.style()
        self.btn_add = QPushButton()
        self.btn_add.setIcon(QIcon.fromTheme("list-add",
                                             style.standardIcon(QStyle.SP_FileDialogNewFolder)))
        self.btn_add.setToolTip("Add a custom token by contract address")

        self.btn_hide = QPushButton()
        self.btn_hide.setIcon(QIcon.fromTheme("list-remove",
                                              style.standardIcon(QStyle.SP_TrashIcon)))
        self.btn_hide.setToolTip("Hide selected token from this wallet")
        self.btn_hide.setEnabled(False)

        self.btn_pin = QPushButton()
        _pin_icon = QIcon.fromTheme("emblem-favorite",
                                    QIcon.fromTheme("starred"))
        if _pin_icon.isNull() or not _pin_icon.availableSizes():
            self.btn_pin.setText("★")    # Unicode star, reliable on any system
        else:
            self.btn_pin.setIcon(_pin_icon)
        self.btn_pin.setToolTip(
            "Pin selected token: always show it, even at zero balance "
            "or below the dust threshold"
        )
        self.btn_pin.setEnabled(False)

        self.btn_show_all = QPushButton()
        _eye_icon = QIcon.fromTheme("view-visible",
                                    QIcon.fromTheme("eye-symbolic"))
        if _eye_icon.isNull() or not _eye_icon.availableSizes():
            self.btn_show_all.setText("👁")  # Unicode eye fallback
        else:
            self.btn_show_all.setIcon(_eye_icon)
        self.btn_show_all.setToolTip(
            "Show all tokens (including dust and hidden); suspected "
            "scams stay hidden"
        )
        self.btn_show_all.setCheckable(True)

        for b in (self.btn_add, self.btn_hide, self.btn_pin, self.btn_show_all):
            b.setFlat(True)
            b.setMaximumSize(28, 28)
            b.setIconSize(QSize(16, 16))
            b.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self.btn_add.clicked.connect(self.add_custom_requested.emit)
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
        self._token_lists: "TokenLists | None" = None
        # Same — MainWindow injects so the alarm icon also reflects GoPlus
        # high-risk verdicts (honeypot / hidden owner / >50% sell tax).
        self._risk_cache: "RiskCache | None" = None

    def action_widgets(self) -> list[QWidget]:
        """The +/-/★/👁 buttons, in display order. MainWindow mounts them
        on the shared bottom-right row beside the chain selector."""
        return [self.btn_add, self.btn_hide, self.btn_pin, self.btn_show_all]

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

        # --- native row ---------------------------------------------------
        native_balance = wei_to_ether(native_wei)
        self._balances[(chain.chain_id, self.NATIVE_CONTRACT)] = native_balance
        sym = QTableWidgetItem(chain.symbol)
        sym.setData(Qt.UserRole, (chain.chain_id, self.NATIVE_CONTRACT))
        sym.setToolTip(f"Native {chain.symbol} on {chain.name}")
        bf = sym.font(); bf.setBold(True); sym.setFont(bf)
        native_pix = bundled_native_icon(chain.symbol)
        if native_pix is not None:
            sym.setIcon(QIcon(native_pix))
        bal = _NumericItem(_format_balance(native_balance), native_balance)
        bal.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        bal.setFont(bf)
        val = _NumericItem("", Decimal(0))
        val.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        val.setFont(bf)
        name = QTableWidgetItem(chain.name)
        name.setFont(bf)
        self.table.setItem(0, 0, sym)
        self.table.setItem(0, 1, bal)
        self.table.setItem(0, 2, val)
        self.table.setItem(0, 3, name)

        # --- ERC-20 rows --------------------------------------------------
        alarm_icon = self.style().standardIcon(QStyle.SP_MessageBoxWarning)
        for row, b in enumerate(tokens, start=1):
            key = (chain.chain_id, b.contract.lower())
            self._balances[key] = b.balance
            entry = list_entries.get(key)
            sym = QTableWidgetItem(b.symbol)
            sym.setData(Qt.UserRole, key)
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
                sym.setToolTip(b.contract + "\n⚠ Looks like a scam token")
            else:
                pix = self._icons.get(chain.chain_id, b.contract)
                if pix is not None:
                    sym.setIcon(QIcon(pix))
                elif entry and entry.logo_uri:
                    self._icons.request(chain.chain_id, b.contract, entry.logo_uri)
            bal = _NumericItem(_format_balance(b.balance), b.balance)
            bal.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val = _NumericItem("", Decimal(0))
            val.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            name = QTableWidgetItem(b.name)
            self.table.setItem(row, 0, sym)
            self.table.setItem(row, 1, bal)
            self.table.setItem(row, 2, val)
            self.table.setItem(row, 3, name)

        self.table.setSortingEnabled(True)

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
            key = sym.data(Qt.UserRole)
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
            self._prices_state: dict = {}
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
            self.set_prices(self._chain_id, cached_prices, apply_dust_filter=False)

    def set_prices(self, chain_id: int, prices: dict, apply_dust_filter: bool = True) -> None:
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
        if stored and set(stored.keys()) == set(prices.keys()) and all(
            stored[k].price_usd == prices[k].price_usd for k in prices
        ):
            return
        self._remember_prices(prices)
        self.table.setSortingEnabled(False)
        for row in range(self.table.rowCount()):
            sym = self.table.item(row, 0)
            if sym is None:
                continue
            key = sym.data(Qt.UserRole)
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
                # Dust hiding (display-time; doesn't touch the worker data).
                # Show only: native, user-force-shown, or priced above dust.
                if (is_native
                        or self._store.is_force_shown(cid, addr)
                        or (value is not None and value >= self.DUST_USD_THRESHOLD)):
                    self.table.setRowHidden(row, False)
                else:
                    self.table.setRowHidden(row, True)
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
            cid, addr = item.data(Qt.UserRole) or (None, None)
            if cid == chain_id and addr == contract.lower():
                item.setIcon(QIcon(pix))
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
        key = sym.data(Qt.UserRole)
        if not key or key[1] == self.NATIVE_CONTRACT:
            return None
        return key

    def _emit_for_selected(self, sig) -> None:
        sel = self._selected_token()
        if sel:
            sig.emit(sel[0], sel[1])

    def _update_action_buttons(self) -> None:
        enabled = self._selected_token() is not None
        self.btn_hide.setEnabled(enabled)
        self.btn_pin.setEnabled(enabled)

    # ---- context menu ---------------------------------------------------

    def _on_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        sym_item = self.table.item(item.row(), 0)
        if sym_item is None:
            return
        meta = sym_item.data(Qt.UserRole)
        if not meta:
            return
        cid, addr = meta
        if addr == self.NATIVE_CONTRACT:
            return  # native asset can't be hidden
        menu = QMenu(self)
        act_hide = menu.addAction(f"Hide {sym_item.text()}")
        act_copy = menu.addAction("Copy contract address")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is act_hide:
            self.hide_requested.emit(cid, addr)
        elif chosen is act_copy:
            QApplication.clipboard().setText(addr)


# --- Transaction history panel + worker -------------------------------------

# Light decoding for the most common selectors. Anything not here renders
# as the raw 10-char selector — better than guessing wrong on a name.
