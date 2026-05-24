"""TransactionsPlugin — self-contained tx-history UI module.

Migrated out of MainWindow as step 2 of the plugin refactor. Owns its
data source, in-memory cache, in-flight set, and the QThread worker.
Lifecycle:

    on_account_changed  → render cached if any; trigger fetch when
                          this plugin is currently active.
    on_chain_changed    → same — current chain is part of the cache key.
    on_activated        → if no cache yet for the current (chain, addr),
                          fire a background fetch.

Lazy-loading is preserved: Blockscout is only hit when the plugin is
the active one (user opened the Transactions tab) AND we don't have
a cached page yet.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtCore import QThread
from PySide6.QtWidgets import QWidget

from .plugin import Plugin
from .transactions import (
    BlockscoutTransactionSource, Transaction, TransactionSource,
)


log = logging.getLogger("qeth.plugin.transactions")


class TransactionsWorker(QThread):
    """Fetch the recent transactions for (chain, address) via the
    configured TransactionSource."""

    # Object signal carries Python objects (avoids qint64 marshalling).
    fetched = Signal(int, str, object)
    failed = Signal(str)

    def __init__(self, source: TransactionSource, chain, address: str,
                 limit: int = 50, parent=None):
        super().__init__(parent)
        self.source = source
        self.chain = chain
        self.address = address
        self.limit = limit

    def run(self) -> None:
        try:
            txs = self.source.list_transactions(
                self.chain, self.address, limit=self.limit,
            )
            self.fetched.emit(
                self.chain.chain_id, self.address.lower(), txs,
            )
        except Exception as e:
            self.failed.emit(str(e))


class TransactionsPlugin(Plugin):
    name = "Transactions"

    def __init__(self, source: Optional[TransactionSource] = None):
        super().__init__()
        # Source injection lets tests pass a fake; default is Blockscout.
        self._source: TransactionSource = source or BlockscoutTransactionSource()
        # In-memory cache, keyed by (chain_id, address_lower). Survives
        # within a session; disk-backed cache is a future iteration.
        self._cache: dict[tuple[int, str], list[Transaction]] = {}
        # Active fetches — prevents duplicate Blockscout calls when
        # on_activated / on_account_changed / on_chain_changed all fire
        # close together (e.g. user clicks a new account while the tab
        # is open).
        self._in_flight: set[tuple[int, str]] = set()
        # The widget is built lazily so the plugin can be instantiated
        # outside a Qt event loop (useful in pure-Python imports).
        self._panel = None

    # --- Plugin contract ----------------------------------------------------

    def widget(self) -> QWidget:
        if self._panel is None:
            # Imported here to avoid a module-load-time Qt import in
            # consumers that just want the plugin metadata.
            from .ui import TransactionListPanel
            self._panel = TransactionListPanel()
        return self._panel

    # No bottom-row actions yet. Future: a Refresh button, a "load
    # more" cursor, etc. would go here.
    def action_widgets(self):
        return []

    # --- lifecycle hooks ----------------------------------------------------

    def on_account_changed(self, address: Optional[str]) -> None:
        if address is None:
            if self._panel is not None:
                self._panel.clear()
            return
        self._refresh(address)

    def on_chain_changed(self) -> None:
        addr = self.host.selected_address if self.host else None
        if addr is not None:
            self._refresh(addr)

    def on_activated(self) -> None:
        addr = self.host.selected_address if self.host else None
        if addr is not None:
            # Force the fetch path even if the cache is empty — this
            # is the user explicitly opening the tab.
            self._refresh(addr, force_fetch=True)

    # --- core --------------------------------------------------------------

    def _is_active(self) -> bool:
        """True when this plugin is currently the active tab in its
        slot. Used to decide whether to actually hit Blockscout —
        switching accounts while the user is on the Tokens tab should
        just invalidate our cache view, not trigger network calls."""
        panel = self._panel
        if panel is None:
            return False
        # When the slot is single-plugin, the widget is always shown.
        # When multi-plugin, only the active one is visible.
        return panel.isVisible()

    def _refresh(self, address: str, force_fetch: bool = False) -> None:
        """Render cached transactions immediately (if any) and kick a
        background fetch when the plugin is currently visible and
        we're not already refreshing this (chain, address) view.
        ``force_fetch=True`` skips the visibility gate — used by
        ``on_activated`` so an explicit tab open always refreshes."""
        if self.host is None or self._panel is None:
            # Plugin not yet attached, or widget not built — nothing
            # to do, the next activation will pick up state.
            return
        chain = self.host.current_chain()
        key = (chain.chain_id, address.lower())

        self._panel.set_context(chain, address)
        cached = self._cache.get(key)
        if cached is not None:
            self._panel.show_transactions(cached)
        elif force_fetch or self._is_active():
            self._panel.show_loading()

        if not (force_fetch or self._is_active()):
            return
        if not self._source.supports(chain):
            self._panel.show_error(
                f"Transactions aren't available for {chain.name}."
            )
            return
        if key in self._in_flight:
            return
        self._in_flight.add(key)

        worker = TransactionsWorker(self._source, chain, address)
        worker.fetched.connect(self._on_fetched)
        worker.failed.connect(
            lambda msg, k=key: self._on_failed(k, msg)
        )
        self.host.start_worker(worker)

    def _on_fetched(self, chain_id: int, address_lower: str,
                    txs: list) -> None:
        key = (chain_id, address_lower)
        self._in_flight.discard(key)
        self._cache[key] = txs
        # Only repaint if the user still has this view selected — they
        # may have clicked another account/chain while we waited.
        if self.host is None:
            return
        addr = self.host.selected_address
        if addr is None or addr.lower() != address_lower:
            return
        if self.host.current_chain().chain_id != chain_id:
            return
        self._panel.show_transactions(txs)

    def _on_failed(self, key: tuple[int, str], msg: str) -> None:
        self._in_flight.discard(key)
        log.warning("transactions fetch failed for %s/%s: %s",
                    key[0], key[1], msg)
        if self.host is None or self._panel is None:
            return
        addr = self.host.selected_address
        if addr is None or addr.lower() != key[1]:
            return
        if self.host.current_chain().chain_id != key[0]:
            return
        if not self._is_active():
            return
        self._panel.show_error(msg)
