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

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHeaderView, QLabel, QMenu, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..formatting import format_datetime as _format_datetime
from ..plugin import Plugin
from ..transactions import (
    BlockscoutTransactionSource, Transaction, TransactionSource,
)
from ..transactions_cache import TransactionCache, merge_txs


log = logging.getLogger("qeth.plugin.transactions")


class TransactionsWorker(QThread):
    """Walk every page of a wallet's tx history via the configured
    TransactionSource, emitting each page as it arrives so the UI can
    render incrementally (newest first, growing as we paginate).

    ``sent_only=True`` filters each page to outgoing transactions
    (``tx.from_addr == address``). This is the default because the
    panel sorts by nonce: received-tx nonces are the *sender's* and
    interleave non-monotonically with the wallet's own nonces. The
    filter is applied per page so the empty-page-after-filter case
    (a whole page of received-only txs) just continues walking
    without misinterpreting as end-of-history.

    Early-exits when a (filtered) page contains a hash already in the
    caller's ``known_hashes`` set — that's how repeated runs stay
    fast: the first page typically overlaps prior history, the merge
    handles dedup, and we stop without walking the entire history.

    A small inter-page sleep is polite to Blockscout's rate limit;
    ``max_pages`` is a runaway guard for accounts with deep history."""

    # Object signal carries Python objects (avoids qint64 marshalling).
    page_fetched = Signal(int, str, object)   # chain_id, addr_lower, page
    completed = Signal(int, str)              # chain_id, addr_lower
    failed = Signal(str)

    def __init__(self, source: TransactionSource, chain, address: str,
                 known_hashes=None, page_size: int = 50,
                 max_pages: int = 1000, page_pause_s: float = 0.2,
                 sent_only: bool = True, parent=None):
        super().__init__(parent)
        self.source = source
        self.chain = chain
        self.address = address
        self.known_hashes = set(known_hashes or ())
        self.page_size = page_size
        self.max_pages = max_pages
        self.page_pause_s = page_pause_s
        self.sent_only = sent_only

    def run(self) -> None:
        viewer = self.address.lower()
        try:
            for page_idx in range(1, self.max_pages + 1):
                raw_page = self.source.list_transactions(
                    self.chain, self.address,
                    page=page_idx, limit=self.page_size,
                )
                if not raw_page:
                    # No more rows on the wire — done.
                    break
                page = raw_page
                if self.sent_only:
                    page = [t for t in raw_page
                            if t.from_addr.lower() == viewer]

                if page:
                    self.page_fetched.emit(
                        self.chain.chain_id, viewer, page,
                    )
                    # Caught up: this page already contains entries we
                    # have cached, so anything older is also cached.
                    if any(t.hash in self.known_hashes for t in page):
                        break
                # Note: an empty page *after filter* doesn't mean the
                # end of history — it just means this raw page was all
                # received txs. Keep walking.

                if page_idx < self.max_pages and self.page_pause_s > 0:
                    self.msleep(int(self.page_pause_s * 1000))
            self.completed.emit(self.chain.chain_id, viewer)
        except Exception as e:
            self.failed.emit(str(e))


class TransactionsPlugin(Plugin):
    name = "Transactions"

    def __init__(
        self,
        source: Optional[TransactionSource] = None,
        disk_cache: Optional[TransactionCache] = None,
    ):
        super().__init__()
        # Source / cache injection both let tests pass fakes.
        self._source: TransactionSource = source or BlockscoutTransactionSource()
        self._disk_cache = disk_cache if disk_cache is not None else TransactionCache()
        # In-memory cache, keyed by (chain_id, address_lower). Hydrated
        # lazily from the disk cache on first ``on_account_changed`` for
        # a (chain, addr) — that's what prevents the empty → populated
        # flicker on startup.
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

    # --- persistence shim ---------------------------------------------------

    def header_state(self) -> str:
        if self._panel is None:
            return ""
        return self._panel.header_state()

    def restore_header_state(self, state_hex: str) -> None:
        if self._panel is not None:
            self._panel.restore_header_state(state_hex)

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
        if cached is None:
            # First time this (chain, addr) is seen this session — try
            # the disk cache. Confirmed txs don't change, so cached
            # bytes from a prior run are always safe to render. Also
            # drop any received txs that an earlier (pre-filter) build
            # may have written to disk — keeping them would break the
            # nonce-monotonic sort.
            disk = self._disk_cache.load(chain.chain_id, address)
            if disk:
                addr_l = address.lower()
                disk = [t for t in disk if t.from_addr.lower() == addr_l]
                self._cache[key] = disk
                cached = disk
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

        # Pass the currently-known hashes so the worker can early-exit
        # once it walks back into territory we've already cached. On
        # the very first run this set is empty and the worker paginates
        # to the wallet's full history (or max_pages, whichever is
        # smaller).
        known = {t.hash for t in (cached or [])}
        worker = TransactionsWorker(
            self._source, chain, address, known_hashes=known,
        )
        worker.page_fetched.connect(self._on_page_fetched)
        worker.completed.connect(
            lambda _cid, _addr, k=key: self._in_flight.discard(k)
        )
        worker.failed.connect(
            lambda msg, k=key: self._on_failed(k, msg)
        )
        self.host.start_worker(worker)

    def _on_page_fetched(self, chain_id: int, address_lower: str,
                         page: list) -> None:
        """One page of newest-first transactions arrived. Merge it into
        the cache, save, and re-render — so the list grows as pages
        come in instead of waiting for the whole backfill to finish."""
        key = (chain_id, address_lower)
        existing = self._cache.get(key) or []
        merged = merge_txs(page, existing)
        self._cache[key] = merged
        # Persist after every page so an interrupted backfill leaves
        # the disk cache holding everything we did fetch.
        self._disk_cache.save(chain_id, address_lower, merged)
        # Only repaint if the user still has this view selected.
        if self.host is None:
            return
        addr = self.host.selected_address
        if addr is None or addr.lower() != address_lower:
            return
        if self.host.current_chain().chain_id != chain_id:
            return
        self._panel.show_transactions(merged)

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


# --- panel ----------------------------------------------------------------


class TransactionListPanel(QWidget):
    """Right pane / Transactions tab: top-level txs for the selected
    account, newest first. Double-click opens the tx in the block
    explorer; right-click offers copy-hash / copy-counterparty."""

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        # Status / Nonce / Time / Hash. The Status column has an empty
        # label — the ✓/✗ glyph speaks for itself, and dropping the word
        # "Status" lets the column be tight against the left edge.
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "Nonce", "Time", "Hash"])
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
        # ElideMiddle on the view lets the Hash column adapt: the full
        # hash is stored in the cell, and Qt truncates at paint time
        # only as much as needed to fit the column width — so the
        # rendered text grows as the user widens the column.
        # Short-text cells (Status/Nonce/Time, all ResizeToContents)
        # always fit, so this setting only ever takes effect on Hash.
        self.table.setTextElideMode(Qt.ElideMiddle)
        h = self.table.horizontalHeader()
        # Status / Nonce / Time auto-fit content (no user-drag — there's
        # nothing meaningful to widen them to). Hash stretches to fill
        # the remaining space; its rendered text is the short
        # 0x1234…abcd form, so the wider cell looks padded rather than
        # full-bleed. Stretch + ResizeToContents together also mean
        # there's no empty trailing space after Hash.
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)  # Status
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)  # Nonce
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)  # Time
        h.setSectionResizeMode(3, QHeaderView.Stretch)           # Hash
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

    def header_state(self) -> str:
        """Hex-encoded QHeaderView.saveState() — captures column widths,
        order, and any sort indicator. MainWindow persists this on close
        and restores on startup."""
        return bytes(
            self.table.horizontalHeader().saveState().toHex()
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
        for row, tx in enumerate(txs):
            status = QTableWidgetItem("✓" if tx.success else "✗")
            status.setTextAlignment(Qt.AlignCenter)
            status.setToolTip("Success" if tx.success else "Reverted")

            nonce = QTableWidgetItem(str(tx.nonce))
            nonce.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

            time_item = QTableWidgetItem(_format_datetime(tx.timestamp))

            # Full hash stored as the cell text; the view elides it
            # in the middle at paint time based on the column's
            # current width, so widening the column reveals more
            # characters until the whole 0x… string fits.
            hash_item = QTableWidgetItem(tx.hash)
            hash_item.setFont(QFont("monospace"))
            hash_item.setToolTip(tx.hash)
            hash_item.setData(Qt.UserRole, tx.hash)

            self.table.setItem(row, 0, status)
            self.table.setItem(row, 1, nonce)
            self.table.setItem(row, 2, time_item)
            self.table.setItem(row, 3, hash_item)

    # Column 3 (Hash) carries the full tx hash on UserRole. The
    # explorer-open and context-menu handlers read it from there.

    def _selected_hash(self) -> str | None:
        items = self.table.selectedItems()
        if not items:
            return None
        return self.table.item(items[0].row(), 3).data(Qt.UserRole)

    def _open_in_explorer(self, row: int, col: int) -> None:
        if self._chain is None or not self._chain.explorer:
            return
        h = self.table.item(row, 3).data(Qt.UserRole)
        if not h:
            return
        url = f"{self._chain.explorer.rstrip('/')}/tx/{h}"
        QDesktopServices.openUrl(QUrl(url))

    def _on_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        row = item.row()
        h = self.table.item(row, 3).data(Qt.UserRole)
        menu = QMenu(self)
        act_open = menu.addAction("Open in block explorer")
        act_open.setEnabled(bool(self._chain and self._chain.explorer and h))
        act_copy_hash = menu.addAction("Copy tx hash")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is act_open:
            self._open_in_explorer(row, 0)
        elif chosen is act_copy_hash and h:
            QApplication.clipboard().setText(h)
