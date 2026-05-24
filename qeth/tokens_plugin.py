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

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import QInputDialog, QMessageBox, QWidget

from .chain import EthClient
from .icons import IconCache
from .plugin import Plugin
from .prices import DefiLlamaPrices, PriceSource
from .risk import GoPlusRisk, RiskCache
from .token_metadata import TokenMetadataCache
from .tokenlists import TokenLists
from .tokens import BlockscoutSource, TokenBalance
from .wallet_cache import CachedToken, CachedWallet, WalletCache


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
            # Local import: avoid pulling Qt UI module at plugin import
            # time so non-UI consumers (CLI tools, tests of pure logic)
            # can import qeth.tokens_plugin without booting Qt.
            from .ui import TokenListPanel
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
        from .ui import TokenListPanel
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
