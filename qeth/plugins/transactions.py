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

import datetime
import logging
from typing import Optional

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QFontDatabase, QTextCharFormat,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFormLayout,
    QHeaderView, QLabel, QMenu, QPushButton, QSizePolicy,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from ..abi import BlockscoutAbiSource, decode_call
from ..abi_cache import AbiCache
from ..chain import wei_to_ether
from ..formatting import format_datetime as _format_datetime
from ..plugin import Plugin
from ..transactions import (
    BlockscoutTransactionSource, Transaction, TransactionSource,
)
from ..transactions_cache import TransactionCache, merge_txs


log = logging.getLogger("qeth.plugin.transactions")


# --- decoded-call renderer ------------------------------------------------
#
# Colour choices balance light and dark themes — moderate saturation
# so both modes stay readable. Function name is bold + default text
# colour (always contrasts against the background); types and values
# get distinct colours so the eye can scan them quickly.
_TYPE_COLOR = "#0066cc"     # cool blue
_VALUE_COLOR = "#22863a"    # green

# Monospace families we prefer for the decoded-call view, in order.
# All ship a Bold style — that's what made the function name fail to
# stand out when Qt resolved generic ``monospace`` to a Regular-only
# family on Linux. The list also covers macOS (Menlo / SF Mono /
# Monaco) and Windows (Consolas / Courier New) so the picker lands
# on a platform-native choice without us having to special-case OS.
_MONO_FAMILY_PREFERENCES = (
    # Linux desktop defaults
    "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono",
    # Cross-platform popular dev fonts (Adobe / GitHub / Mozilla)
    "Source Code Pro", "Hack", "Fira Code", "Cascadia Code",
    # macOS built-ins (Menlo is the default since 10.6; SF Mono and
    # Monaco are also shipped)
    "Menlo", "SF Mono", "Monaco",
    # Windows built-ins
    "Consolas", "Courier New",
)


def _pick_mono_font() -> QFont:
    installed = set(QFontDatabase.families())
    for family in _MONO_FAMILY_PREFERENCES:
        if family not in installed:
            continue
        if any("bold" in s.lower() for s in QFontDatabase.styles(family)):
            f = QFont(family)
            f.setFixedPitch(True)
            return f
    # Last resort — generic alias. Bold may render as faux-bold (or
    # not at all) depending on what the resolver picks, but the
    # rest of the dialog still looks fine.
    f = QFont("monospace")
    f.setStyleHint(QFont.Monospace)
    f.setFixedPitch(True)
    return f


def _render_decoded(text_edit, decoded: dict) -> None:
    """Render a decoded call into ``text_edit`` as Python-style
    annotated text. Top-level args render as

        register(
            registration: tuple = {
                label: string = qeth,
                secret: bytes32 = 0x99…,
                …
            },
        )

    with the function name bold, types in blue and values in green.
    Struct args expand recursively with deepening indentation; leaf
    args go on one line.

    Uses QTextCursor + QTextCharFormat directly rather than HTML —
    Qt's HTML renderer silently drops ``<b>`` / ``font-weight`` when
    the resolved font has no Bold variant. We explicitly walk a
    preference list of monospace families and pick the first one
    that is (a) installed and (b) ships a Bold style. The CSS
    ``monospace`` alias on some systems resolves to families with
    only a Regular style (e.g. Droid Sans Mono Slashed), which
    would render the function name visually identical to the rest
    of the line."""
    text_edit.clear()
    cursor = text_edit.textCursor()

    mono = _pick_mono_font()
    base = QTextCharFormat()
    base.setFont(mono)

    bold = QTextCharFormat(base)
    bold.setFontWeight(QFont.Bold)

    type_fmt = QTextCharFormat(base)
    type_fmt.setForeground(QColor(_TYPE_COLOR))

    value_fmt = QTextCharFormat(base)
    value_fmt.setForeground(QColor(_VALUE_COLOR))

    formats = (base, type_fmt, value_fmt)

    cursor.insertText(decoded.get("function") or "?", bold)
    cursor.insertText("(\n", base)
    for arg in decoded.get("args") or []:
        _insert_arg(cursor, arg, indent=1, formats=formats)
    cursor.insertText(")", base)


def _insert_arg(cursor, arg: dict, *, indent: int, formats) -> None:
    """Write one ``arg`` node (leaf or struct branch) into the
    document at the current cursor, indented to ``indent`` levels."""
    base, type_fmt, value_fmt = formats
    pad = "    " * indent
    cursor.insertText(pad + (arg.get("name") or "") + ": ", base)
    cursor.insertText(arg.get("type") or "", type_fmt)
    cursor.insertText(" = ", base)
    children = arg.get("children")
    if children is not None:
        if children:
            cursor.insertText("{\n", base)
            for child in children:
                _insert_arg(cursor, child, indent=indent + 1, formats=formats)
            cursor.insertText(pad + "},\n", base)
        else:
            cursor.insertText("{},\n", base)
    else:
        value = arg.get("value")
        cursor.insertText("" if value is None else str(value), value_fmt)
        cursor.insertText(",\n", base)


def _is_full_history(txs: list[Transaction]) -> bool:
    """True iff ``txs`` already represents the entire sent history of
    a wallet — used to decide whether the next refresh can early-exit
    on hash overlap, or whether it has to walk every page.

    Sent nonces are 0-based and strictly monotonic per sender, so the
    cache is complete iff nonce 0 is present AND every value between
    0 and max(nonce) appears. Returns False for empty input — an
    empty cache could be a brand-new wallet OR a never-fetched one,
    and a re-walk costs at most one Blockscout call to confirm."""
    if not txs:
        return False
    nonces = {t.nonce for t in txs}
    return 0 in nonces and len(nonces) == max(nonces) + 1


class TransactionsWorker(QThread):
    """Fetch ONE page of (sent) transactions from Blockscout.

    Single-page-per-fetch is what enables the "load on scroll" UX:
    the plugin kicks one worker on tab open (page 1), then one more
    per scroll-to-bottom (page 2, 3, …). Auto-walking the entire
    history at once is too aggressive for accounts with thousands of
    txs (e.g. the 0x7a16… test address has 17 000+ sent).

    ``sent_only`` filters out received entries before the signal —
    their nonces are the *sender's* and would break the nonce-desc
    sort. ``has_more`` distinguishes "this was a normal full page"
    from "Blockscout returned fewer than we asked, so we've reached
    the end" — lets the plugin stop fetching without trying another
    empty round-trip."""

    # Object signal carries Python objects (avoids qint64 marshalling).
    fetched = Signal(int, str, int, object, bool)
    # (chain_id, addr_lower, page_idx, list[Transaction], has_more)
    failed = Signal(str)

    def __init__(self, source: TransactionSource, chain, address: str,
                 page: int = 1, page_size: int = 50,
                 sent_only: bool = True, parent=None):
        super().__init__(parent)
        self.source = source
        self.chain = chain
        self.address = address
        self.page = page
        self.page_size = page_size
        self.sent_only = sent_only

    def run(self) -> None:
        viewer = self.address.lower()
        try:
            raw = self.source.list_transactions(
                self.chain, self.address,
                page=self.page, limit=self.page_size,
            )
            # A partial page means Blockscout has nothing more — used
            # by the plugin to flag the (chain, addr) as exhausted.
            has_more = len(raw) >= self.page_size
            page = raw
            if self.sent_only:
                page = [t for t in raw if t.from_addr.lower() == viewer]
            self.fetched.emit(
                self.chain.chain_id, viewer, self.page, page, has_more,
            )
        except Exception as e:
            self.failed.emit(str(e))


class TransactionsPlugin(Plugin):
    name = "Transactions"

    # How many rows to render at first (and to extend by on each
    # scroll-to-bottom event). Chosen so the initial open is snappy
    # even for accounts with thousands of cached txs.
    INITIAL_BATCH = 50

    def __init__(
        self,
        source: Optional[TransactionSource] = None,
        disk_cache: Optional[TransactionCache] = None,
        abi_source: Optional[BlockscoutAbiSource] = None,
        abi_cache: Optional[AbiCache] = None,
    ):
        super().__init__()
        # Source / cache injection both let tests pass fakes.
        self._source: TransactionSource = source or BlockscoutTransactionSource()
        self._disk_cache = disk_cache if disk_cache is not None else TransactionCache()
        # ABI machinery for the details dialog. Lazy fetch + disk-
        # cache so each contract address is looked up at most once.
        self._abi_source = abi_source if abi_source is not None else BlockscoutAbiSource()
        self._abi_cache = abi_cache if abi_cache is not None else AbiCache()
        # In-memory cache, keyed by (chain_id, address_lower). Hydrated
        # lazily from the disk cache on first ``on_account_changed`` for
        # a (chain, addr) — that's what prevents the empty → populated
        # flicker on startup.
        self._cache: dict[tuple[int, str], list[Transaction]] = {}
        # Active fetches — prevents duplicate Blockscout calls when
        # on_activated / on_account_changed / on_chain_changed all fire
        # close together (e.g. user clicks a new account while the tab
        # is open). Also coalesces repeated scroll-to-bottom triggers.
        self._in_flight: set[tuple[int, str]] = set()
        # Per-key paging state for the load-on-scroll UX.
        # next_page = page index to fetch on the next scroll-to-bottom.
        # exhausted = we've fetched the last page (a partial page came
        # back, OR the cached set now includes nonce 0) — further
        # network scrolls are ignored for this account.
        # displayed_count = how many of the cached rows are currently
        # rendered on the table. Bounded growth via INITIAL_BATCH +
        # scroll-driven appends keeps rendering O(visible), not
        # O(cache) — critical for accounts with thousands of cached
        # entries where rebuilding the whole table freezes the UI.
        self._next_page: dict[tuple[int, str], int] = {}
        self._exhausted: set[tuple[int, str]] = set()
        self._displayed_count: dict[tuple[int, str], int] = {}
        # Which (chain, addr) the panel's table currently shows. Used
        # to skip the show_transactions rebuild when on_activated fires
        # for the same view we already painted — Qt then preserves the
        # scrollbar and the user's scrolled-in batches stay intact.
        self._rendered_for: Optional[tuple[int, str]] = None
        # The widget is built lazily so the plugin can be instantiated
        # outside a Qt event loop (useful in pure-Python imports).
        self._panel = None

    # --- Plugin contract ----------------------------------------------------

    def widget(self) -> QWidget:
        if self._panel is None:
            self._panel = TransactionListPanel()
            self._panel.scrolled_to_bottom.connect(self._on_scroll_bottom)
            self._panel.tx_details_requested.connect(self._show_tx_details)
        return self._panel

    def _show_tx_details(self, tx: Transaction) -> None:
        if self.host is None or self._panel is None:
            return
        chain = self.host.current_chain()
        dialog = TransactionDetailsDialog(
            tx, chain,
            abi_source=self._abi_source,
            abi_cache=self._abi_cache,
            start_worker=self.host.start_worker,
            parent=self._panel,
        )
        dialog.show()

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
                # Estimate where to resume Blockscout pagination based
                # on cache size. Assumes ~50 sent txs per Blockscout
                # page (i.e. sent_ratio = 1.0); if the actual ratio is
                # lower the auto-advance walk picks up the slack. With
                # a 1213-entry cache and page_size 50 we jump straight
                # to page ~25, avoiding the ~5s walk through pages 2..24
                # that all return entries we already have.
                if key not in self._next_page and disk:
                    self._next_page[key] = max(2, (len(disk) // 50) + 1)
        # Re-render only when the panel currently shows a *different*
        # view. If it's the same (chain, addr) we already painted
        # (e.g. user just toggled away to Tokens and back), leaving
        # the table alone preserves both content AND the scroll
        # position — exactly what tabs in a browser do.
        view_changed = self._rendered_for != key
        if view_changed and cached is not None:
            # Render the whole cache up-front. QTableWidget handles a
            # few thousand rows fine when populated in one shot with
            # updates suspended; the load-on-scroll work then becomes
            # purely network-driven (fetch older pages from Blockscout
            # only when the user scrolls past the cache).
            self._displayed_count[key] = len(cached)
            self._panel.show_transactions(cached)
            self._rendered_for = key
        elif view_changed and (force_fetch or self._is_active()):
            self._displayed_count[key] = 0
            self._panel.show_loading()
            self._rendered_for = key

        if not (force_fetch or self._is_active()):
            return
        if not self._source.supports(chain):
            self._panel.show_error(
                f"Transactions aren't available for {chain.name}."
            )
            return
        # If we already hold the wallet's full sent history (nonce 0
        # present + contiguous), there's nothing newer to refresh and
        # nothing older to scroll for. Skip the network call.
        if _is_full_history(cached or []):
            self._exhausted.add(key)
            return
        # Always (re-)fetch page 1 on open: cheapest way to pick up
        # txs the user might have sent from another wallet client
        # since the last visit. Older pages come from scroll.
        self._fetch_page(key, address, page=1)

    def _fetch_page(self, key, address: str, page: int,
                    walk_on_overlap: bool = False) -> None:
        """Kick a single-page fetch. No-op if a fetch for this key is
        already in flight or if the history is known to be exhausted.

        ``walk_on_overlap`` distinguishes the two fetch reasons:
          - False (default): "refresh newest" — fetch page 1 to pick up
            anything new, but if the page returns only entries already
            cached, stop. Used on _refresh (tab activation / account
            select). Tab switching costs at most one HTTP call.
          - True: "load older" — the user has scrolled past the cache
            and wants more history. If the page returns only overlap
            (typical when resuming an interrupted backfill), advance
            to the next page until we find genuinely new older data.
        """
        if self.host is None or self._panel is None:
            return
        if key in self._in_flight or key in self._exhausted:
            return
        chain = self.host.current_chain()
        self._in_flight.add(key)
        worker = TransactionsWorker(
            self._source, chain, address, page=page,
        )
        worker.fetched.connect(
            lambda c, a, p, t, m, w=walk_on_overlap:
                self._on_page_fetched(c, a, p, t, m, walk_on_overlap=w)
        )
        worker.failed.connect(
            lambda msg, k=key: self._on_failed(k, msg)
        )
        self.host.start_worker(worker)

    def _on_scroll_bottom(self) -> None:
        """Panel says the user reached the bottom — the whole cache
        is already rendered, so this always means "fetch older from
        the network". Auto-advance walks Blockscout pages until we
        land on one with new data (deep caches typically overlap with
        several Blockscout pages before fresh history begins)."""
        if self.host is None:
            return
        addr = self.host.selected_address
        if not addr:
            return
        chain = self.host.current_chain()
        key = (chain.chain_id, addr.lower())
        next_page = self._next_page.get(key, 1)
        self._fetch_page(key, addr, page=next_page, walk_on_overlap=True)

    def _on_page_fetched(self, chain_id: int, address_lower: str,
                         page_idx: int, page: list, has_more: bool,
                         walk_on_overlap: bool = False) -> None:
        """One page arrived. Merge it into the cache, persist, advance
        the paging cursor, and incrementally update the visible table
        — never rebuilding it from the full cache (which can be tens
        of thousands of rows). ``walk_on_overlap`` mirrors the flag
        set by _fetch_page: True for scroll-driven calls (keep walking
        through cached overlap until new data lands), False for the
        cheap refresh-newest path."""
        key = (chain_id, address_lower)
        self._in_flight.discard(key)
        existing = self._cache.get(key) or []
        existing_hashes = {t.hash for t in existing}
        merged = merge_txs(page, existing)
        self._cache[key] = merged
        self._disk_cache.save(chain_id, address_lower, merged)

        self._next_page[key] = max(
            self._next_page.get(key, 1), page_idx + 1,
        )
        new_rows = [t for t in page if t.hash not in existing_hashes]

        if not has_more or _is_full_history(merged):
            self._exhausted.add(key)
        elif walk_on_overlap and not new_rows:
            # Scroll-driven fetch returned only entries we already
            # have cached — walk forward until we hit genuinely new
            # older data. (Refresh-newest fetches don't take this
            # branch; they're one-shot.)
            self._fetch_page(
                key, address_lower, page=page_idx + 1,
                walk_on_overlap=True,
            )

        # Only touch the panel if the user is still on this view.
        if (self.host is None
                or self.host.selected_address is None
                or self.host.selected_address.lower() != address_lower
                or self.host.current_chain().chain_id != chain_id):
            return
        if not new_rows:
            return
        # Newer entries (refresh case, nonce above old top) prepend;
        # older entries (scroll case) append. Both grow the visible
        # window without re-rendering the rest of the table.
        shown = self._displayed_count.get(key, 0)
        top_nonce = existing[0].nonce if existing else -1
        newer = [t for t in new_rows if t.nonce > top_nonce]
        older = [t for t in new_rows if t.nonce <= top_nonce]
        if newer:
            newer.sort(key=lambda t: t.nonce, reverse=True)
            self._panel.prepend_transactions(newer)
            shown += len(newer)
        # Append older rows only if the user has already scrolled
        # past the existing window — otherwise they'd appear between
        # the displayed top section and the not-yet-revealed cache
        # entries below, which would look weird. We just save them
        # to the cache and let the next scroll-to-bottom reveal them.
        if older and shown >= len(existing):
            older.sort(key=lambda t: t.nonce, reverse=True)
            self._panel.append_transactions(older)
            shown += len(older)
        self._displayed_count[key] = shown

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
    account, newest first.

    Signals:
      scrolled_to_bottom      — user reached the bottom; load more.
      tx_details_requested    — user double-clicked or hit
                                "Show details…" in the context menu.
                                Plugin opens TransactionDetailsDialog
                                with ABI decoding.
    """

    scrolled_to_bottom = Signal()
    tx_details_requested = Signal(object)   # Transaction instance

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
        self.table.cellDoubleClicked.connect(self._on_double_click)
        # ElideMiddle on the view lets the Hash column adapt: the full
        # hash is stored in the cell, and Qt truncates at paint time
        # only as much as needed to fit the column width — so the
        # rendered text grows as the user widens the column.
        # Short-text cells (Status/Nonce/Time, all ResizeToContents)
        # always fit, so this setting only ever takes effect on Hash.
        self.table.setTextElideMode(Qt.ElideMiddle)
        # Scroll-to-bottom drives the load-more UX.
        self.table.verticalScrollBar().valueChanged.connect(
            self._on_scroll_change
        )
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

    def _on_scroll_change(self, value: int) -> None:
        """Emit ``scrolled_to_bottom`` when the user reaches the
        bottom of the table. The 4-pixel slack matches Qt's default
        item-view fuzz so a kinetic scroll that stops a hair short
        still triggers."""
        bar = self.table.verticalScrollBar()
        if bar.maximum() > 0 and value >= bar.maximum() - 4:
            self.scrolled_to_bottom.emit()

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
        # Suspend repaints + sort + scrollbar updates while we
        # populate. For tables with a few thousand rows this drops
        # render time from ~1-2 s to sub-200ms by avoiding O(N)
        # per-item recalculations.
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(txs))
            for row, tx in enumerate(txs):
                self._populate_row(row, tx)
        finally:
            self.table.setUpdatesEnabled(True)

    def append_transactions(self, txs: list[Transaction]) -> None:
        """Add rows at the bottom of the existing list (older entries
        for our nonce-desc sort). No setRowCount-on-the-whole-cache —
        only the new rows get materialized."""
        if not txs:
            return
        self.status_lbl.setVisible(False)
        start = self.table.rowCount()
        self.table.setRowCount(start + len(txs))
        for offset, tx in enumerate(txs):
            self._populate_row(start + offset, tx)

    def prepend_transactions(self, txs: list[Transaction]) -> None:
        """Add rows at the top of the existing list (newer entries —
        used when a page-1 refresh discovers txs the user sent from
        another wallet client). ``txs`` is expected newest-first;
        we insertRow in reverse so each ends up at row 0 in order."""
        if not txs:
            return
        self.status_lbl.setVisible(False)
        for tx in reversed(txs):
            self.table.insertRow(0)
            self._populate_row(0, tx)

    def _populate_row(self, row: int, tx: Transaction) -> None:
        """Render one tx into ``row``. Shared by show / append /
        prepend so the cell shape stays consistent across paths.
        The full Transaction is stored on the Hash cell's UserRole
        so handlers (explorer, details dialog) can recover it."""
        status = QTableWidgetItem("✓" if tx.success else "✗")
        status.setTextAlignment(Qt.AlignCenter)
        status.setToolTip("Success" if tx.success else "Reverted")

        nonce = QTableWidgetItem(str(tx.nonce))
        nonce.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

        time_item = QTableWidgetItem(_format_datetime(tx.timestamp))

        # Full hash as the cell text; the view elides it in the middle
        # at paint time based on the column width, so widening the
        # column reveals more characters until the whole 0x… fits.
        hash_item = QTableWidgetItem(tx.hash)
        hash_item.setFont(QFont("monospace"))
        hash_item.setToolTip(tx.hash)
        hash_item.setData(Qt.UserRole, tx)

        self.table.setItem(row, 0, status)
        self.table.setItem(row, 1, nonce)
        self.table.setItem(row, 2, time_item)
        self.table.setItem(row, 3, hash_item)

    def _tx_at(self, row: int) -> Optional[Transaction]:
        item = self.table.item(row, 3)
        if item is None:
            return None
        data = item.data(Qt.UserRole)
        return data if isinstance(data, Transaction) else None

    def _on_double_click(self, row: int, _col: int) -> None:
        tx = self._tx_at(row)
        if tx is not None:
            self.tx_details_requested.emit(tx)

    def _open_in_explorer(self, tx: Transaction) -> None:
        if self._chain is None or not self._chain.explorer:
            return
        url = f"{self._chain.explorer.rstrip('/')}/tx/{tx.hash}"
        QDesktopServices.openUrl(QUrl(url))

    def _on_context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        tx = self._tx_at(item.row())
        if tx is None:
            return
        menu = QMenu(self)
        act_details = menu.addAction("Show transaction details…")
        act_open = menu.addAction("Open in block explorer")
        act_open.setEnabled(bool(self._chain and self._chain.explorer))
        act_copy_hash = menu.addAction("Copy tx hash")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen is act_details:
            self.tx_details_requested.emit(tx)
        elif chosen is act_open:
            self._open_in_explorer(tx)
        elif chosen is act_copy_hash:
            QApplication.clipboard().setText(tx.hash)


# --- transaction details dialog + ABI fetch worker ------------------------


class AbiFetchWorker(QThread):
    """Look up the ABI for a contract address. Checks the disk cache
    first (positive hits and the unverified-sentinel both short-
    circuit the HTTP call) and falls back to a Blockscout fetch.

    Emits ``ready(abi)`` where ``abi`` is the parsed list of fragments,
    ``False`` for known-unverified, or ``None`` on transient errors."""

    ready = Signal(object)

    def __init__(self, source: BlockscoutAbiSource, cache: AbiCache,
                 chain_id: int, address: str, parent=None):
        super().__init__(parent)
        self.source = source
        self.cache = cache
        self.chain_id = chain_id
        self.address = address

    def run(self) -> None:
        cached = self.cache.load(self.chain_id, self.address)
        if cached is not None:
            self.ready.emit(cached)
            return
        try:
            abi = self.source.fetch(self.chain_id, self.address)
        except Exception as e:
            log.warning("ABI fetch failed for %s/%s: %s",
                        self.chain_id, self.address, e)
            self.ready.emit(None)
            return
        # Persist verified ABIs AND the negative sentinel — both save
        # the next dialog the round-trip.
        self.cache.save(self.chain_id, self.address, abi)
        self.ready.emit(abi)


class TransactionDetailsDialog(QDialog):
    """Modal-ish dialog showing the full tx record.

    Calldata decoding runs asynchronously: the dialog opens with a
    "(decoding…)" placeholder, kicks an AbiFetchWorker, and fills in
    the function name + arguments when the worker returns. The
    explorer link button is always available regardless of ABI state.
    """

    def __init__(self, tx: Transaction, chain, *,
                 abi_source: BlockscoutAbiSource,
                 abi_cache: AbiCache,
                 start_worker,
                 parent=None):
        super().__init__(parent)
        self.tx = tx
        self.chain = chain
        self._abi_source = abi_source
        self._abi_cache = abi_cache
        self._start_worker = start_worker

        self.setWindowTitle(f"Transaction {tx.hash[:10]}…")
        self.resize(720, 480)

        v = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        v.addLayout(form)

        mono = QFont("monospace")

        def _label(text: str, *, monospace: bool = False) -> QLabel:
            lbl = QLabel(text)
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if monospace:
                lbl.setFont(mono)
            lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            return lbl

        form.addRow("Status:", _label("✓ Success" if tx.success else "✗ Reverted"))
        form.addRow("Nonce:", _label(str(tx.nonce)))
        dt = datetime.datetime.fromtimestamp(tx.timestamp)
        form.addRow("Date:", _label(dt.strftime("%c")))
        form.addRow("Timestamp:", _label(f"{tx.timestamp} (unix)"))
        form.addRow("Block:", _label(str(tx.block_number)))
        form.addRow("Hash:", _label(tx.hash, monospace=True))
        form.addRow("From:", _label(tx.from_addr, monospace=True))
        to_text = tx.to_addr or "(contract creation)"
        form.addRow("To:", _label(to_text, monospace=True))
        # Value rendered through wei_to_ether (Decimal) — never float.
        if tx.value_wei:
            ether = wei_to_ether(tx.value_wei)
            value_text = f"{ether} {chain.symbol}  ({tx.value_wei} wei)"
        else:
            value_text = "0"
        form.addRow("Value:", _label(value_text))
        form.addRow("Method ID:", _label(tx.method_id or "(none — plain transfer)",
                                        monospace=True))

        # Decoded call goes in a read-only QTextEdit so we can render
        # syntax-highlighted HTML (function bold, types and values
        # coloured). QPlainTextEdit would only do flat text.
        self.decoded_view = QTextEdit()
        self.decoded_view.setReadOnly(True)
        self.decoded_view.setFont(mono)
        self.decoded_view.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding,
        )
        form.addRow("Decoded call:", self.decoded_view)

        # Buttons row: Explorer + Close.
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        explorer_btn = QPushButton("Open in block explorer")
        explorer_btn.setEnabled(bool(chain.explorer))
        explorer_btn.clicked.connect(self._open_explorer)
        buttons.addButton(explorer_btn, QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

        # Start ABI fetch + decode (only when there's calldata).
        if tx.input_data and tx.input_data not in ("0x", "0X") and tx.to_addr:
            self.decoded_view.setPlainText("(decoding…)")
            worker = AbiFetchWorker(
                self._abi_source, self._abi_cache,
                chain.chain_id, tx.to_addr,
            )
            worker.ready.connect(self._on_abi_ready)
            self._start_worker(worker)
        elif not tx.to_addr:
            self.decoded_view.setPlainText("(contract creation — no method call)")
        else:
            self.decoded_view.setPlainText("(plain value transfer — no calldata)")

    def _on_abi_ready(self, abi) -> None:
        if abi is False:
            self.decoded_view.setPlainText(
                "(contract source is not verified on Blockscout — "
                "no ABI available for decoding)"
            )
            return
        if abi is None:
            self.decoded_view.setPlainText(
                "(failed to fetch ABI from Blockscout — try again later)"
            )
            return
        decoded = decode_call(abi, self.tx.input_data, address=self.tx.to_addr)
        if decoded is None:
            self.decoded_view.setPlainText(
                "(ABI available but this calldata didn't match any "
                "function in it — possibly a fallback or proxy call)"
            )
            return
        _render_decoded(self.decoded_view, decoded)

    def _open_explorer(self) -> None:
        if not self.chain.explorer:
            return
        url = f"{self.chain.explorer.rstrip('/')}/tx/{self.tx.hash}"
        QDesktopServices.openUrl(QUrl(url))
