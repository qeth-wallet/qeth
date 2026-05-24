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

import datetime

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QMenu,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..chain import wei_to_ether
from ..formatting import format_relative_time as _format_relative_time
from ..formatting import short_addr as _short_addr
from ..plugin import Plugin
from ..transactions import (
    BlockscoutTransactionSource, Transaction, TransactionSource, TxDirection,
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


# --- panel + selector map (moved from qeth.ui) -----------------------------

KNOWN_SELECTORS: dict[str, str] = {
    "0xa9059cbb": "transfer",
    "0x23b872dd": "transferFrom",
    "0x095ea7b3": "approve",
    "0xd0e30db0": "deposit",
    "0x2e1a7d4d": "withdraw",
    "0x7ff36ab5": "swapExactETHForTokens",
    "0x18cbafe5": "swapExactTokensForETH",
    "0x38ed1739": "swapExactTokensForTokens",
    "0x5ae401dc": "multicall",
    "0xac9650d8": "multicall",
}




class TransactionListPanel(QWidget):
    """Right pane / Transactions tab: top-level txs for the selected
    account, newest first. Double-click opens the tx in the block
    explorer; right-click offers copy-hash / copy-counterparty."""

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["When", "Counterparty", "Value", "Method", "Status"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setShowGrid(False)
        # Same selection/hover normalization as TokenListPanel.
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
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.cellDoubleClicked.connect(self._open_in_explorer)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        v.addWidget(self.table, 1)

        # The empty-state / loading / error label sits stacked under the
        # table; we toggle visibility based on state.
        self.status_lbl = QLabel("")
        self.status_lbl.setAlignment(Qt.AlignCenter)
        self.status_lbl.setVisible(False)
        v.addWidget(self.status_lbl)

        # Set by MainWindow before render so we can build explorer URLs
        # and compute SENT/RECEIVED direction labels.
        self._chain = None
        self._viewer: str | None = None

    def set_context(self, chain, viewer_address: str) -> None:
        self._chain = chain
        self._viewer = viewer_address

    def show_loading(self) -> None:
        self.table.setRowCount(0)
        self.status_lbl.setText("Loading transactions…")
        self.status_lbl.setVisible(True)

    def show_error(self, msg: str) -> None:
        self.table.setRowCount(0)
        self.status_lbl.setText(f"Couldn't load transactions: {msg}")
        self.status_lbl.setVisible(True)

    def show_empty(self) -> None:
        self.table.setRowCount(0)
        self.status_lbl.setText("No transactions yet for this account.")
        self.status_lbl.setVisible(True)

    def clear(self) -> None:
        self.table.setRowCount(0)
        self.status_lbl.setVisible(False)
        self._chain = None
        self._viewer = None

    def show_transactions(self, txs: list[Transaction]) -> None:
        if not txs:
            self.show_empty()
            return
        self.status_lbl.setVisible(False)
        self.table.setRowCount(len(txs))
        viewer = (self._viewer or "").lower()
        symbol = self._chain.symbol if self._chain else "ETH"
        now = int(datetime.datetime.now().timestamp())
        for row, tx in enumerate(txs):
            direction = tx.direction(viewer) if viewer else TxDirection.UNRELATED

            when = QTableWidgetItem(_format_relative_time(tx.timestamp, now))
            when.setToolTip(datetime.datetime.fromtimestamp(tx.timestamp)
                            .strftime("%Y-%m-%d %H:%M:%S"))
            when.setData(Qt.UserRole, tx.hash)

            if direction == TxDirection.SENT:
                arrow, counterparty = "→", tx.to_addr
            elif direction == TxDirection.RECEIVED:
                arrow, counterparty = "←", tx.from_addr
            elif direction == TxDirection.SELF:
                arrow, counterparty = "↻", tx.to_addr
            else:
                arrow, counterparty = " ", tx.to_addr or tx.from_addr
            cp = QTableWidgetItem(f"{arrow} {_short_addr(counterparty)}")
            cp.setFont(QFont("monospace"))
            cp.setToolTip(counterparty or "")
            cp.setData(Qt.UserRole, counterparty)

            if tx.value_wei:
                # Native amounts are wei → ether through Decimal (never
                # float — see CLAUDE.md on-chain math rule).
                ether = wei_to_ether(tx.value_wei)
                value_text = f"{ether:.6f} {symbol}"
            else:
                value_text = "—"
            val = QTableWidgetItem(value_text)
            val.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            method_label = KNOWN_SELECTORS.get(tx.method_id, tx.method_id or "—")
            method = QTableWidgetItem(method_label)
            if tx.method_id and method_label != tx.method_id:
                method.setToolTip(tx.method_id)

            status = QTableWidgetItem("✓" if tx.success else "✗")
            status.setTextAlignment(Qt.AlignCenter)
            status.setToolTip("Success" if tx.success else "Reverted")

            self.table.setItem(row, 0, when)
            self.table.setItem(row, 1, cp)
            self.table.setItem(row, 2, val)
            self.table.setItem(row, 3, method)
            self.table.setItem(row, 4, status)

    def _selected_hash(self) -> str | None:
        items = self.table.selectedItems()
        if not items:
            return None
        return self.table.item(items[0].row(), 0).data(Qt.UserRole)

    def _open_in_explorer(self, row: int, col: int) -> None:
        if self._chain is None or not self._chain.explorer:
            return
        h = self.table.item(row, 0).data(Qt.UserRole)
        if not h:
            return
        url = f"{self._chain.explorer.rstrip('/')}/tx/{h}"
        QDesktopServices.openUrl(QUrl(url))

    def _on_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        h = self.table.item(row, 0).data(Qt.UserRole)
        cp = self.table.item(row, 1).data(Qt.UserRole)
        menu = QMenu(self)
        act_open = menu.addAction("Open in block explorer")
        act_open.setEnabled(bool(self._chain and self._chain.explorer and h))
        act_copy_hash = menu.addAction("Copy tx hash")
        act_copy_cp = menu.addAction("Copy counterparty address")
        act_copy_cp.setEnabled(bool(cp))
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is act_open:
            self._open_in_explorer(row, 0)
        elif chosen is act_copy_hash and h:
            QApplication.clipboard().setText(h)
        elif chosen is act_copy_cp and cp:
            QApplication.clipboard().setText(cp)


# --- Main window -------------------------------------------------------------

