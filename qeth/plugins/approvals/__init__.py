"""Approvals plugin — progressive full-history scan → live tree of allowances.

A ScanWorker pages the selected account's WHOLE tx history (explorer, block-
cursor walk, resumable from the tx cache), emits each fetched batch for the
plugin to merge into the tx cache on the MAIN thread (TransactionCache has no
lock), and every few pages checks the newly discovered approve() (token,
spender) pairs via multicall — so the tree fills in as the scan runs. A bottom
progress bar tracks it and can be stopped. Side effect: when the scan completes,
the account's full history is cached (the Transactions tab stops refetching).

Modify/revoke actions land in later commits; this file is the read-only scan +
tree.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from eth_utils import to_checksum_address
from PySide6.QtCore import QEvent, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QMenu,
    QProgressBar, QPushButton, QSizePolicy, QStyle, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ... import QULONGLONG
from ...chain import EthClient
from ...formatting import format_balance, short_addr
from ...plugin import Plugin
from ...token_metadata import TokenMetadataCache
from ...transactions import (
    BlockscoutTransactionSource, EtherscanV2TransactionSource,
    RoutedTransactionSource,
)
from ..transactions import _encode_approve, _is_full_history
from .discovery import ApprovalRow, approve_pairs_in, fetch_allowances
from .revoke_queue import RevokeQueue

if TYPE_CHECKING:
    from decimal import Decimal

    from ...transactions import Transaction

log = logging.getLogger(__name__)

_ROW_ROLE = Qt.ItemDataRole.UserRole          # leaf: ApprovalRow
_TOKEN_ROLE = Qt.ItemDataRole.UserRole + 1    # token node: token address (lower)
_USD_SORT_ROLE = Qt.ItemDataRole.UserRole + 2  # float: USD exposure (∞ = unlimited)
_MIN_COL_W = 48                                # neither column shrinks below this
_COL_GAP = 16                                  # breathing room left of the (right-aligned) amount


def _icon(names: tuple[str, ...], fallback: QStyle.StandardPixmap) -> QIcon:
    """A themed icon from the first of ``names`` the icon theme provides, with a
    built-in Qt standard icon as a last resort so something always renders
    (icon-name coverage is patchy across themes)."""
    for name in names:
        ic = QIcon.fromTheme(name)
        if not ic.isNull():
            return ic
    app = QApplication.instance()
    if isinstance(app, QApplication):
        return app.style().standardIcon(fallback)
    return QIcon()


# Action → (theme-name candidates, Qt standard-icon fallback). Shared by the
# buttons and the context menu so both carry the same glyph (house standard).
_IC_MODIFY = (("document-edit", "edit-rename", "accessories-text-editor"),
              QStyle.StandardPixmap.SP_FileDialogDetailedView)
_IC_REVOKE = (("edit-delete", "list-remove", "user-trash"),
              QStyle.StandardPixmap.SP_TrashIcon)
_IC_COPY = (("edit-copy",), QStyle.StandardPixmap.SP_FileDialogContentsView)
_IC_EXPLORER = (("internet-web-browser", "web-browser", "applications-internet"),
                QStyle.StandardPixmap.SP_ComputerIcon)
_IC_REFRESH = (("view-refresh", "reload"), QStyle.StandardPixmap.SP_BrowserReload)
_IC_STOP = (("media-playback-stop", "process-stop"),
            QStyle.StandardPixmap.SP_MediaStop)
_IC_SELECT_ALL = (("edit-select-all", "select-all", "checkbox"),
                  QStyle.StandardPixmap.SP_FileDialogListView)

# At/above this an allowance reads as "unlimited" — the same threshold the
# approve dialog's Unlimited toggle uses (2**255 is half the uint256 space,
# far past any honest cap, so it captures 2**256-1 and its near-max sentinels
# that would otherwise render as a 70-plus-digit number).
_UNLIMITED_MIN = 2 ** 255


def _format_allowance(raw: int, decimals: int) -> str:
    """Compact, symbol-free allowance for the tree's Allowance column (the
    token symbol is already the parent branch). Near-max sentinels collapse to
    "unlimited"; everything else goes through ``format_balance`` so a large but
    finite cap shows as ``1.23 × 10¹⁰`` rather than a horizontally-scrolling
    run of digits."""
    if raw >= _UNLIMITED_MIN:
        return "unlimited"
    from decimal import Decimal
    scaled = Decimal(raw) / (Decimal(10) ** decimals) if decimals > 0 else Decimal(raw)
    # Format via float so %g is clean in BOTH directions — a Decimal keeps its
    # trailing zeros ("9.12000 × 10¹⁰") and turns round thousands scientific
    # ("1.5 × 10³"); float gives "9.12 × 10¹⁰" and "1500". 6 sig figs fits float.
    return format_balance(float(scaled))


def _row_usd(r: ApprovalRow):
    """USD value of the allowance cap (``amount × unit price``), or None for an
    unlimited or unpriced allowance. Derived from ``allowance`` each call so it
    stays correct after a reconcile edits the amount in place."""
    if r.price_usd is None or r.allowance >= _UNLIMITED_MIN:
        return None
    from decimal import Decimal
    scaled = (Decimal(r.allowance) / (Decimal(10) ** r.decimals)
              if r.decimals > 0 else Decimal(r.allowance))
    return scaled * r.price_usd


def _row_sort_value(r: ApprovalRow) -> float:
    """Numeric exposure key for sorting: unlimited outranks everything (∞), a
    priced cap sorts by its USD value, an unpriced finite cap sorts as 0."""
    if r.allowance >= _UNLIMITED_MIN:
        return float("inf")
    usd = _row_usd(r)
    return float(usd) if usd is not None else 0.0


def _allowance_cell(r: ApprovalRow) -> str:
    """Allowance column text: just the compact token amount. (USD is not shown —
    it read as clutter — but the priced value still drives the by-exposure sort
    via ``_row_sort_value``.)"""
    return _format_allowance(r.allowance, r.decimals)


class _ApprovalItem(QTreeWidgetItem):
    """Tree item that sorts the Allowance column by a numeric USD role (stored in
    ``_USD_SORT_ROLE``) instead of its display text, and the identity column by
    casefolded text — the same trick the ENS tree uses for its expiry column."""

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, QTreeWidgetItem):
            return NotImplemented
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else 0
        if col == 1:
            a = self.data(1, _USD_SORT_ROLE)
            b = other.data(1, _USD_SORT_ROLE)
            return (a if a is not None else 0.0) < (b if b is not None else 0.0)
        return self.text(0).casefold() < other.text(0).casefold()


# --- worker ---------------------------------------------------------------

class ScanWorker(QThread):
    """Pages the account's full history newest-first, resuming from the cache
    snapshot; interruptible between pages (the Stop button)."""

    batch_fetched = Signal(QULONGLONG, str, object)      # cid, addr_l, [Transaction] (NEW rows)
    rows_ready = Signal(QULONGLONG, str, object)         # cid, addr_l, [ApprovalRow]
    progress = Signal(QULONGLONG, str, object, object)   # cid, addr_l, sent_seen, nonce_total
    scan_done = Signal(QULONGLONG, str, bool)            # cid, addr_l, complete

    PAGE = 100
    PAIR_BATCH_EVERY = 3          # re-check allowances every K pages
    MAX_ATTEMPTS = 3

    def __init__(self, chain, address: str, source, snapshot, metadata_cache,
                 *, label_source=None, price_source=None, known_pairs=None,
                 client_factory=None, parent=None):
        super().__init__(parent)
        self._chain = chain
        self._address = address
        self._source = source
        self._snapshot = list(snapshot)
        self._meta = metadata_cache
        self._label_source = label_source
        self._price_source = price_source
        self._known_pairs = set(known_pairs or ())   # cached pairs to re-check
        self._priced: dict[str, Decimal | None] = {}   # token -> unit price (memo)
        self._client_factory = client_factory or EthClient

    def run(self) -> None:
        cid = self._chain.chain_id
        addr_l = self._address.lower()
        try:
            client = self._client_factory(self._chain)
            seen = {t.hash for t in self._snapshot}
            all_pairs = self._known_pairs | set(
                approve_pairs_in(self._snapshot, self._address))
            checked: set[tuple[str, str]] = set()
            self._emit_rows(client, cid, addr_l, all_pairs, checked)

            try:
                total = client.get_transaction_count(self._address, "latest")
            except Exception:
                total = 0
            sent_seen = sum(1 for t in self._snapshot
                            if t.from_addr.lower() == addr_l)
            self.progress.emit(cid, addr_l, sent_seen, total)

            # A fully-cached account no longer re-pages its whole history — it
            # only fetches the NEW tail (txs above what's cached) to catch fresh
            # approvals, then stops the moment it reaches already-seen txs.
            tail_only = _is_full_history(self._snapshot)
            snapshot_oldest = min((t.block_number for t in self._snapshot),
                                  default=None)
            cursor: int | None = None
            jumped = snapshot_oldest is None
            pages = 0
            while not self.isInterruptionRequested():
                raw = self._fetch_page(cursor)
                if raw is None:                          # persistent explorer failure
                    self.scan_done.emit(cid, addr_l, False)
                    return
                if not raw:
                    break
                new = [t for t in raw if t.hash not in seen]
                if new:
                    seen.update(t.hash for t in new)
                    self.batch_fetched.emit(cid, addr_l, new)
                    sent_seen += sum(1 for t in new if t.from_addr.lower() == addr_l)
                    all_pairs |= approve_pairs_in(new, self._address)
                pages += 1
                self.progress.emit(cid, addr_l, min(sent_seen, total or sent_seen),
                                   total)
                raw_oldest = min(t.block_number for t in raw)
                if not new:                              # reached already-cached txs
                    if tail_only:
                        break                            # incremental: tail done
                    # A page fully within the cached newest span → skip below it.
                    if (not jumped and snapshot_oldest is not None
                            and raw_oldest > snapshot_oldest):
                        cursor = snapshot_oldest
                        jumped = True
                        continue
                if pages % self.PAIR_BATCH_EVERY == 0:
                    self._emit_rows(client, cid, addr_l, all_pairs, checked)
                if len(raw) < self.PAGE:                 # short page = start of history
                    break
                if cursor is not None and raw_oldest >= cursor:   # no progress guard
                    break
                cursor = raw_oldest

            self._emit_rows(client, cid, addr_l, all_pairs, checked)
            self.scan_done.emit(cid, addr_l, not self.isInterruptionRequested())
        except Exception:
            log.debug("approvals scan failed", exc_info=True)
            self.scan_done.emit(cid, addr_l, False)

    def _fetch_page(self, cursor: int | None) -> list[Transaction] | None:
        import time
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                return self._source.list_transactions(
                    self._chain, self._address, page=1, limit=self.PAGE,
                    before_block=cursor)
            except Exception:
                if attempt < self.MAX_ATTEMPTS - 1:
                    time.sleep(0.5 * (attempt + 1))
        return None

    def _emit_rows(self, client, cid: int, addr_l: str,
                   all_pairs: set[tuple[str, str]],
                   checked: set[tuple[str, str]]) -> None:
        todo = all_pairs - checked
        if not todo:
            return
        checked.update(todo)
        found = fetch_allowances(client, self._address, todo)
        if not found:
            return
        tokens = sorted({t for (t, _s) in found})
        missing = self._meta.missing(cid, tokens)
        if missing:
            try:
                self._meta.put_many(cid, client.multicall_erc20_metadata(missing))
            except Exception:
                log.debug("approvals metadata read failed", exc_info=True)
        labels = self._fetch_labels(cid, sorted({s for (_t, s) in found}))
        finite = sorted({t for (t, _s), v in found.items() if v < _UNLIMITED_MIN})
        prices = self._fetch_prices(finite)
        rows = []
        for (token, spender), value in found.items():
            m = self._meta.get(cid, token) or {}
            rows.append(ApprovalRow(
                token=token, spender=to_checksum_address(spender), allowance=value,
                symbol=m.get("symbol") or "", name=m.get("name") or "",
                decimals=int(m.get("decimals") or 18),
                spender_label=labels.get(spender, ""),
                price_usd=prices.get(token)))
        self.rows_ready.emit(cid, addr_l, rows)

    def _fetch_prices(self, tokens: list[str]) -> dict[str, Decimal | None]:
        """USD unit prices for ``tokens`` (memoized, so streaming batches don't
        re-quote). Best-effort — an outage just leaves the caps unpriced."""
        if self._price_source is None or not tokens:
            return {}
        need = [t for t in tokens if t not in self._priced]
        if need:
            quotes: dict = {}
            try:
                quotes = self._price_source.fetch(self._chain, need)
            except Exception:
                log.debug("approvals price fetch failed", exc_info=True)
            for t in need:
                q = quotes.get(t)
                self._priced[t] = q.price_usd if q is not None else None
        return {t: self._priced.get(t) for t in tokens}

    def _fetch_labels(self, cid: int, spenders: list[str]) -> dict[str, str]:
        """Keyless public name-tags for the spender contracts ("Uniswap:
        Universal Router", …), so a leaf reads as WHO it approved, not a bare
        address. Resilient — one bad fetch just leaves the addresses bare."""
        if self._label_source is None or not spenders:
            return {}
        try:
            return self._label_source.fetch_labels(cid, spenders)
        except Exception:
            log.debug("approvals label fetch failed", exc_info=True)
            return {}


class ReconcileWorker(QThread):
    """Re-reads ``allowance(owner, spender)`` for a specific set of pairs after
    a modify/revoke mines, so the tree reflects the on-chain truth (a reverted
    revoke keeps its old value; a successful one reads 0). Emits every requested
    pair — 0 for any that dropped out of ``fetch_allowances`` (which keeps >0
    only) so the plugin knows to remove the leaf."""

    reconciled = Signal(QULONGLONG, str, object)   # cid, addr_l, {(token, spender): value}

    def __init__(self, chain, owner: str, pairs, *, client_factory=None, parent=None):
        super().__init__(parent)
        self._chain = chain
        self._owner = owner
        self._pairs = list(pairs)
        self._client_factory = client_factory or EthClient

    def run(self) -> None:
        cid = self._chain.chain_id
        addr_l = self._owner.lower()
        values: dict[tuple[str, str], int] = dict.fromkeys(self._pairs, 0)
        try:
            client = self._client_factory(self._chain)
            found = fetch_allowances(client, self._owner, self._pairs)
            values.update(found)
        except Exception:
            log.debug("approvals reconcile failed", exc_info=True)
            return                              # leave leaves as-is; no false removals
        self.reconciled.emit(cid, addr_l, values)


# --- panel ----------------------------------------------------------------

class ApprovalsPanel(QWidget):
    modify_requested = Signal(object)      # ApprovalRow   (commit 2)
    revoke_requested = Signal(object)      # [ApprovalRow] (commit 3)
    refresh_requested = Signal()
    stop_requested = Signal()
    copied = Signal(str)

    # Two columns only: the identity (token symbol / spender name-or-address)
    # and the allowance. The full spender address isn't a column — it lives in
    # the leaf tooltip + the Copy action — so the tree never needs the width a
    # 42-char address would force. The identity column middle-elides (the
    # Accounts-tab trick) and the horizontal scrollbar is switched off, so a
    # long name or address truncates gracefully instead of scrolling.
    COLS: ClassVar[list[str]] = ["Token / Spender", "Allowance"]

    def __init__(self, host=None, parent=None):
        super().__init__(parent)
        self._host = host
        self._token_items: dict[str, QTreeWidgetItem] = {}
        self._hovered: QTreeWidgetItem | None = None
        self._sort_col = 0
        self._sort_order = Qt.SortOrder.AscendingOrder    # token A→Z by default
        self._col0_frac: float | None = None              # user's split ratio
        self._syncing = False                             # re-entrancy guard
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.COLS))
        self.tree.setHeaderLabels(self.COLS)
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        # Hover / selection reveals a named spender's ACTUAL address (so it can
        # be eyeballed / checked on the explorer without losing the name label).
        self.tree.setMouseTracking(True)
        self.tree.itemEntered.connect(self._on_item_entered)
        self.tree.viewport().installEventFilter(self)
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        hh = self.tree.header()
        # QTreeView defaults stretchLastSection=True, which force-stretched the
        # last (Allowance) column — stranding the amount with dead space no drag
        # could reclaim. Off, with BOTH columns Interactive + a manual split
        # (see _layout_columns / _on_section_resized): the divider drags, the two
        # columns always sum to the viewport, and Allowance starts content-wide.
        hh.setStretchLastSection(False)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hh.setMinimumSectionSize(_MIN_COL_W)
        hh.setSectionsClickable(True)
        hh.setSortIndicatorShown(True)
        hh.setSortIndicator(self._sort_col, self._sort_order)
        hh.sectionClicked.connect(self._on_header_clicked)
        hh.sectionResized.connect(self._on_section_resized)
        v.addWidget(self.tree, 1)

        self.status_lbl = QLabel("")
        self.status_lbl.setVisible(False)
        v.addWidget(self.status_lbl)

        # Scan progress: a bar with a small media-player-style Stop sign to its
        # right, shown only while scanning (hidden as one unit).
        self._scan_bar = QWidget()
        bar = QHBoxLayout(self._scan_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)
        # No "%p%" text (it inflated the bar's box and threw the button off);
        # just the bar. The bar keeps its natural height and the Stop button
        # fills to exactly that (Ignored vertical), so they line up under any
        # theme without a per-theme fixed pixel height.
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setSizePolicy(QSizePolicy.Policy.Expanding,
                                    QSizePolicy.Policy.Fixed)
        bar.addWidget(self.progress, 1)
        self.btn_stop = QToolButton()
        self.btn_stop.setIcon(_icon(*_IC_STOP))
        self.btn_stop.setToolTip("Stop scanning")
        self.btn_stop.setAutoRaise(True)
        self.btn_stop.setSizePolicy(QSizePolicy.Policy.Fixed,
                                    QSizePolicy.Policy.Ignored)
        bar.addWidget(self.btn_stop)
        self._scan_bar.setVisible(False)
        v.addWidget(self._scan_bar)

        # One morphing primary button, so the action row stays narrow enough
        # that the pane (and the eliding identity column) can size down: it's
        # "Modify" for a single selected row and turns into "Revoke (N)" the
        # moment any boxes are checked. (The context menu still offers both
        # per-row — parity is kept there.)
        self._ic_modify = _icon(*_IC_MODIFY)
        self._ic_revoke = _icon(*_IC_REVOKE)
        self._action_mode = "modify"
        self.btn_action = QPushButton("&Modify")
        self.btn_action.setIcon(self._ic_modify)
        self.btn_action.clicked.connect(self._on_action_clicked)
        # Icon-only buttons render frameless (flat), like a toolbar.
        self.btn_select_all = QPushButton()
        self.btn_select_all.setIcon(_icon(*_IC_SELECT_ALL))
        self.btn_select_all.setToolTip("Check / uncheck all")
        self.btn_select_all.clicked.connect(self._toggle_select_all)
        self.btn_copy = QPushButton()
        self.btn_copy.setIcon(_icon(*_IC_COPY))
        self.btn_copy.setToolTip("Copy spender address")
        self.btn_copy.clicked.connect(self._copy_spender)
        self.btn_explorer = QPushButton()
        self.btn_explorer.setIcon(_icon(*_IC_EXPLORER))
        self.btn_explorer.setToolTip("Open spender in the block explorer")
        self.btn_explorer.clicked.connect(self._open_selected_in_explorer)
        for b in (self.btn_select_all, self.btn_copy, self.btn_explorer):
            b.setFlat(True)

        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.itemSelectionChanged.connect(self._refresh_reveal)
        self.tree.itemChanged.connect(self._update_buttons)
        self._update_buttons()

        if self._host is not None:
            self._host.icon_cache().icon_ready.connect(self._on_icon_ready)

    def action_widgets(self) -> list[QWidget]:
        return [self.btn_action, self.btn_select_all, self.btn_copy,
                self.btn_explorer]

    # --- scan lifecycle ---------------------------------------------------
    def begin_scan(self) -> None:
        self.clear()
        self.progress.setRange(0, 0)                 # indeterminate until first progress
        self._scan_bar.setVisible(True)              # bar + Stop as one unit
        self._set_status("")

    def set_progress(self, seen: int, total: int) -> None:
        if total and total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(seen, total))
        else:
            self.progress.setRange(0, 0)

    def finish_scan(self, complete: bool) -> None:
        self._scan_bar.setVisible(False)
        if self.tree.topLevelItemCount() == 0:
            self._set_status("No active approvals found"
                             if complete else "Scan stopped — no approvals found so far")
        elif not complete:
            self._set_status("Scan stopped — showing what was found so far")
        else:
            self._set_status("")

    def clear(self) -> None:
        self.tree.clear()
        self._token_items.clear()
        self._set_status("")
        self._update_buttons()

    # --- population -------------------------------------------------------
    def append_rows(self, rows: list[ApprovalRow]) -> None:
        self.tree.blockSignals(True)                 # populate without churning buttons
        for r in rows:
            self._add_row(r)
        self.tree.blockSignals(False)
        self._apply_sort()                           # recompute sums, sort, expandAll
        self._layout_columns()                       # keep the split filling the width
        self._update_buttons()
        self._refresh_reveal()

    def _all_leaves(self) -> list[QTreeWidgetItem]:
        out: list[QTreeWidgetItem] = []
        for ti in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(ti)
            if node is None:
                continue
            out.extend(node.child(ci) for ci in range(node.childCount()))
        return out

    def checked_leaves(self) -> list[ApprovalRow]:
        """Every spender leaf the user has ticked (via its own box or its token
        node, which auto-checks the subtree)."""
        out: list[ApprovalRow] = []
        for leaf in self._all_leaves():
            if leaf.checkState(0) == Qt.CheckState.Checked:
                r = leaf.data(0, _ROW_ROLE)
                if isinstance(r, ApprovalRow):
                    out.append(r)
        return out

    def _toggle_select_all(self) -> None:
        """Check every leaf, or uncheck them all if they're already checked —
        so a whole account's caps can be batch-revoked in one go."""
        leaves = self._all_leaves()
        if not leaves:
            return
        all_on = all(le.checkState(0) == Qt.CheckState.Checked for le in leaves)
        target = Qt.CheckState.Unchecked if all_on else Qt.CheckState.Checked
        self.tree.blockSignals(True)
        for ti in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(ti)
            if node is not None:                      # setting the token node
                node.setCheckState(0, target)         # auto-propagates to its leaves
        self.tree.blockSignals(False)
        self._update_buttons()

    def _token_node(self, r: ApprovalRow) -> QTreeWidgetItem:
        node = self._token_items.get(r.token)
        if node is not None:
            return node
        node = _ApprovalItem(self.tree)
        node.setText(0, f"{r.symbol} ({short_addr(r.token)})" if r.symbol
                     else short_addr(r.token))
        node.setData(0, _TOKEN_ROLE, r.token)
        node.setToolTip(0, f"{r.name or r.symbol}\n{r.token}")
        node.setFlags(node.flags() | Qt.ItemFlag.ItemIsUserCheckable
                      | Qt.ItemFlag.ItemIsAutoTristate)
        node.setCheckState(0, Qt.CheckState.Unchecked)
        self._token_items[r.token] = node
        self._apply_token_icon(node, r.token)
        return node

    @staticmethod
    def _leaf_text(r: ApprovalRow, reveal: bool) -> str:
        """Column-0 text for a spender leaf: its name-tag normally, its actual
        address when ``reveal`` (hover / selection) — or always the address
        when there's no name-tag. ElideMiddle truncates a long value to fit."""
        if r.spender_label and not reveal:
            return r.spender_label
        return r.spender

    def _add_row(self, r: ApprovalRow) -> None:
        # Upsert: a fresh scan re-emits pairs already shown (from the cache) —
        # refresh the existing leaf in place rather than duplicating it.
        existing = self._leaf_for(r.token, r.spender)
        if existing is not None:
            self._fill_leaf(existing, r)
            return
        leaf = _ApprovalItem(self._token_node(r))
        leaf.setFlags(leaf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        leaf.setCheckState(0, Qt.CheckState.Unchecked)
        self._fill_leaf(leaf, r)

    def _fill_leaf(self, leaf: QTreeWidgetItem, r: ApprovalRow) -> None:
        leaf.setText(0, self._leaf_text(r, reveal=leaf is self._hovered))
        leaf.setText(1, _allowance_cell(r))
        leaf.setTextAlignment(
            1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        leaf.setData(1, _USD_SORT_ROLE, _row_sort_value(r))
        leaf.setToolTip(0, f"{r.spender_label}\n{r.spender}"
                        if r.spender_label else r.spender)
        leaf.setData(0, _ROW_ROLE, r)

    def all_rows(self) -> list[ApprovalRow]:
        """Every displayed ApprovalRow (for persisting the cache)."""
        out: list[ApprovalRow] = []
        for ti in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(ti)
            if node is None:
                continue
            for ci in range(node.childCount()):
                r = node.child(ci).data(0, _ROW_ROLE)
                if isinstance(r, ApprovalRow):
                    out.append(r)
        return out

    def prune_to(self, pairs: set[tuple[str, str]]) -> None:
        """Drop leaves whose (token, spender) a fresh scan didn't re-confirm —
        i.e. caps revoked outside qeth that now read zero."""
        self.tree.blockSignals(True)
        for ti in range(self.tree.topLevelItemCount() - 1, -1, -1):
            node = self.tree.topLevelItem(ti)
            if node is None:
                continue
            for ci in range(node.childCount() - 1, -1, -1):
                leaf = node.child(ci)
                r = leaf.data(0, _ROW_ROLE)
                if (isinstance(r, ApprovalRow)
                        and (r.token.lower(), r.spender.lower()) not in pairs):
                    if leaf is self._hovered:
                        self._hovered = None
                    node.removeChild(leaf)
            if node.childCount() == 0:
                self._token_items.pop(node.data(0, _TOKEN_ROLE), None)
                self.tree.takeTopLevelItem(ti)
        self.tree.blockSignals(False)
        self._update_buttons()

    # --- sorting (manual: live setSortingEnabled would re-sort on hover) ---
    def _on_header_clicked(self, col: int) -> None:
        if col == self._sort_col:
            self._sort_order = (
                Qt.SortOrder.DescendingOrder
                if self._sort_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder)
        else:
            self._sort_col = col
            # Allowance defaults to highest-exposure-first; identity to A→Z.
            self._sort_order = (Qt.SortOrder.DescendingOrder if col == 1
                                else Qt.SortOrder.AscendingOrder)
        self._apply_sort()

    def _apply_sort(self) -> None:
        self._recompute_token_totals()
        self.tree.header().setSortIndicator(self._sort_col, self._sort_order)
        self.tree.sortItems(self._sort_col, self._sort_order)
        self.tree.expandAll()

    def _recompute_token_totals(self) -> None:
        """Each token node's Allowance-sort key = the summed USD exposure of its
        spenders (∞ if any is unlimited), so sorting by allowance ranks tokens by
        total exposure."""
        for ti in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(ti)
            if node is None:
                continue
            total = 0.0
            for ci in range(node.childCount()):
                v = node.child(ci).data(1, _USD_SORT_ROLE)
                total += float(v) if v is not None else 0.0
            node.setData(1, _USD_SORT_ROLE, total)

    # --- hover / selection address reveal ---------------------------------
    def _on_item_entered(self, item: QTreeWidgetItem, column: int) -> None:
        self._hovered = item
        self._refresh_reveal()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.tree.viewport():
            et = event.type()
            if et == QEvent.Type.Leave and self._hovered is not None:
                self._hovered = None
                self._refresh_reveal()
            elif et == QEvent.Type.Resize:
                self._layout_columns()               # keep the split filling the width
        return super().eventFilter(obj, event)

    # --- column split (two Interactive columns that always fill the width) --
    def _layout_columns(self, *, refit: bool = False) -> None:
        vp = self.tree.viewport().width()
        if vp <= 2 * _MIN_COL_W:
            return
        if refit or self._col0_frac is None:          # first fill: allowance fits content
            self.tree.resizeColumnToContents(1)
            c1 = self.tree.columnWidth(1) + _COL_GAP  # + a gap before the amount
            c1 = min(max(c1, _MIN_COL_W), vp - _MIN_COL_W)
            self._col0_frac = (vp - c1) / vp
        c0 = max(_MIN_COL_W, min(int(vp * self._col0_frac), vp - _MIN_COL_W))
        self._syncing = True
        self.tree.setColumnWidth(0, c0)
        self.tree.setColumnWidth(1, vp - c0)
        self._syncing = False

    def _on_section_resized(self, idx: int, _old: int, new: int) -> None:
        if self._syncing:
            return                                    # our own setColumnWidth
        vp = self.tree.viewport().width()
        if vp <= 2 * _MIN_COL_W:
            return
        c0 = new if idx == 0 else vp - new            # divider drag → both adjust
        c0 = max(_MIN_COL_W, min(c0, vp - _MIN_COL_W))
        self._col0_frac = c0 / vp
        self._syncing = True
        self.tree.setColumnWidth(0, c0)
        self.tree.setColumnWidth(1, vp - c0)
        self._syncing = False

    def _refresh_reveal(self) -> None:
        """Rewrite each named leaf's column-0 text so the hovered / selected one
        shows its address and the rest show their name-tag."""
        hovered = self._hovered
        current = self.tree.currentItem()
        self.tree.blockSignals(True)
        for ti in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(ti)
            if node is None:
                continue
            for ci in range(node.childCount()):
                leaf = node.child(ci)
                r = leaf.data(0, _ROW_ROLE)
                if not isinstance(r, ApprovalRow) or not r.spender_label:
                    continue
                reveal = leaf is hovered or (leaf is current and leaf.isSelected())
                want = self._leaf_text(r, reveal)
                if leaf.text(0) != want:
                    leaf.setText(0, want)
        self.tree.blockSignals(False)

    def _apply_token_icon(self, node: QTreeWidgetItem, token: str) -> None:
        """Set the token node's coin icon from the shared cache; kick a
        background fetch (repaints via ``icon_ready``) when it's not cached."""
        if self._host is None:
            return
        cid = self._host.current_chain().chain_id
        cache = self._host.icon_cache()
        pix = cache.get(cid, token)
        if pix is not None:
            node.setIcon(0, QIcon(pix))
            return
        info = self._host.token_info(cid, token)
        url = getattr(info, "logo_uri", None)
        if url:
            cache.request(cid, token, url)

    def _on_icon_ready(self, cid: int, contract: str) -> None:
        node = self._token_items.get(contract.lower())
        if node is None or self._host is None:
            return
        pix = self._host.icon_cache().get(cid, contract)
        if pix is not None:
            node.setIcon(0, QIcon(pix))

    def _set_status(self, text: str) -> None:
        self.status_lbl.setText(text)
        self.status_lbl.setVisible(bool(text))

    # --- optimistic updates (from broadcast / reconcile) ------------------
    def _leaf_for(self, token: str, spender: str) -> QTreeWidgetItem | None:
        node = self._token_items.get(token.lower())
        if node is None:
            return None
        sp = spender.lower()
        for i in range(node.childCount()):
            leaf = node.child(i)
            r = leaf.data(0, _ROW_ROLE)
            if isinstance(r, ApprovalRow) and r.spender.lower() == sp:
                return leaf
        return None

    def mark_pending(self, token: str, spender: str) -> None:
        """Show the leaf as in-flight while its modify/revoke tx confirms."""
        leaf = self._leaf_for(token, spender)
        if leaf is not None:
            leaf.setText(1, "pending…")
            leaf.setDisabled(True)

    def update_allowance(self, token: str, spender: str, value: int) -> None:
        """Re-render a leaf's allowance from an authoritative re-read."""
        leaf = self._leaf_for(token, spender)
        if leaf is None:
            return
        r = leaf.data(0, _ROW_ROLE)
        if isinstance(r, ApprovalRow):
            r.allowance = value
            leaf.setText(1, _allowance_cell(r))
            leaf.setData(1, _USD_SORT_ROLE, _row_sort_value(r))
        leaf.setDisabled(False)

    def remove_leaf(self, token: str, spender: str) -> None:
        """Drop a spender leaf (allowance now zero); drop the token node too when
        it has no spenders left."""
        node = self._token_items.get(token.lower())
        leaf = self._leaf_for(token, spender)
        if node is None or leaf is None:
            return
        node.removeChild(leaf)
        if node.childCount() == 0:
            idx = self.tree.indexOfTopLevelItem(node)
            if idx >= 0:
                self.tree.takeTopLevelItem(idx)
            self._token_items.pop(token.lower(), None)
        self._update_buttons()

    # --- selection / actions ---------------------------------------------
    def _selected_leaf(self) -> ApprovalRow | None:
        it = self.tree.currentItem()
        if it is None:
            return None
        data = it.data(0, _ROW_ROLE)
        return data if isinstance(data, ApprovalRow) else None

    def _update_buttons(self) -> None:
        has_leaf = self._selected_leaf() is not None
        n_checked = len(self.checked_leaves())
        self.btn_copy.setEnabled(has_leaf)
        self.btn_explorer.setEnabled(has_leaf and self._explorer_base() is not None)
        self.btn_select_all.setEnabled(bool(self._all_leaves()))
        # Checked boxes → batch Revoke; else a selected row → Modify; else off.
        if n_checked > 0:
            self._action_mode = "revoke"
            self.btn_action.setText(f"&Revoke ({n_checked})")
            self.btn_action.setIcon(self._ic_revoke)
            self.btn_action.setToolTip("Set the checked allowances to zero")
            self.btn_action.setEnabled(True)
        else:
            self._action_mode = "modify"
            self.btn_action.setText("&Modify")
            self.btn_action.setIcon(self._ic_modify)
            self.btn_action.setToolTip("Set a new allowance for the selected spender")
            self.btn_action.setEnabled(has_leaf)

    def _on_action_clicked(self) -> None:
        if self._action_mode == "revoke":
            rows = self.checked_leaves()
            if rows:
                self.revoke_requested.emit(rows)
        else:
            r = self._selected_leaf()
            if r is not None:
                self.modify_requested.emit(r)

    def _copy_spender(self) -> None:
        r = self._selected_leaf()
        if r is not None:
            QApplication.clipboard().setText(r.spender)
            self.copied.emit(r.spender)

    def _explorer_base(self) -> str | None:
        if self._host is None:
            return None
        base = getattr(self._host.current_chain(), "explorer", "") or ""
        return base.rstrip("/") or None

    def _open_in_explorer(self, address: str) -> None:
        base = self._explorer_base()
        if base and address:
            QDesktopServices.openUrl(QUrl(f"{base}/address/{address}"))

    def _open_selected_in_explorer(self) -> None:
        r = self._selected_leaf()
        if r is not None:
            self._open_in_explorer(r.spender)

    def _on_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        r = item.data(0, _ROW_ROLE) if item is not None else None
        if isinstance(r, ApprovalRow):
            self._open_in_explorer(r.spender)

    def _on_context_menu(self, pos) -> None:
        it = self.tree.itemAt(pos)
        r = it.data(0, _ROW_ROLE) if it is not None else None
        r = r if isinstance(r, ApprovalRow) else None
        token = it.data(0, _TOKEN_ROLE) if it is not None else None
        menu = QMenu(self)
        act_modify = menu.addAction(_icon(*_IC_MODIFY), "Modify Approval…")
        act_modify.setEnabled(r is not None)
        act_revoke = menu.addAction(_icon(*_IC_REVOKE), "Revoke Approval")
        act_revoke.setEnabled(r is not None)
        menu.addSeparator()
        act_copy_sp = menu.addAction(_icon(*_IC_COPY), "Copy spender address")
        act_copy_sp.setEnabled(r is not None)
        act_copy_tok = menu.addAction(_icon(*_IC_COPY), "Copy token address")
        act_copy_tok.setEnabled(r is not None or token is not None)
        act_explorer = menu.addAction(_icon(*_IC_EXPLORER), "Open spender in explorer")
        act_explorer.setEnabled(r is not None and self._explorer_base() is not None)
        menu.addSeparator()
        act_refresh = menu.addAction(_icon(*_IC_REFRESH), "Rescan history")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is act_modify and r is not None:
            self.modify_requested.emit(r)
        elif chosen is act_revoke and r is not None:
            self.revoke_requested.emit([r])
        elif chosen is act_explorer and r is not None:
            self._open_in_explorer(r.spender)
        elif chosen is act_copy_sp and r is not None:
            QApplication.clipboard().setText(r.spender)
            self.copied.emit(r.spender)
        elif chosen is act_copy_tok:
            addr = r.token if r is not None else token
            if addr:
                QApplication.clipboard().setText(addr)
                self.copied.emit(addr)
        elif chosen is act_refresh:
            self.refresh_requested.emit()


# --- plugin ---------------------------------------------------------------

class ApprovalsPlugin(Plugin):
    name = "Approvals"

    def __init__(self, store):
        super().__init__()
        self._store = store
        self._panel: ApprovalsPanel | None = None
        self._loaded_for: tuple[int, str] | None = None
        self._epoch = 0
        self._metadata = TokenMetadataCache()
        self._source = RoutedTransactionSource(
            EtherscanV2TransactionSource(
                lambda: getattr(store, "etherscan_api_key", None)),
            BlockscoutTransactionSource())
        from ..transactions.contract_identity import ContractIdentitySource
        # Keyless: spender name-tags come from the free Blockscout metadata
        # service (fetch_labels needs no Etherscan key).
        self._label_source = ContractIdentitySource(lambda: None)
        from ...pricing import (
            ChainedPriceSource, DefiLlamaPrices, OnChainVaultPrices,
        )
        # USD valuation of finite allowances — DefiLlama (keyless) first, then
        # on-chain for vault/LP shares it can't quote (mirrors the Tokens tab).
        self._price_source = ChainedPriceSource(DefiLlamaPrices(), OnChainVaultPrices())
        self._workers: set[QThread] = set()
        self._scan: ScanWorker | None = None
        self._queue: RevokeQueue | None = None
        self._reconcile_pending: set[tuple[str, str]] = set()
        self._reconcile_timer = QTimer(self)
        self._reconcile_timer.setSingleShot(True)
        self._reconcile_timer.setInterval(600)     # debounce bursts of confirms
        self._reconcile_timer.timeout.connect(self._run_reconcile)
        from ...transactions_cache import TransactionCache
        self._disk_cache = TransactionCache()
        from .cache import ApprovalsCache
        self._cache = ApprovalsCache()               # persisted allowances + last block
        self._last_block = 0
        self._scan_pairs: set[tuple[str, str]] = set()   # pairs a live scan re-confirmed

    def widget(self) -> QWidget:
        if self._panel is None:
            self._panel = ApprovalsPanel(host=self.host)
            self._panel.refresh_requested.connect(lambda: self._kick(force=True))
            self._panel.stop_requested.connect(self._stop_scan)
            self._panel.copied.connect(self._on_copied)
            self._panel.modify_requested.connect(self._on_modify)
            self._panel.revoke_requested.connect(self._on_revoke)
        return self._panel

    def action_widgets(self) -> list[QWidget]:
        return self._panel.action_widgets() if self._panel is not None else []

    def on_account_changed(self, address: str | None) -> None:
        self._invalidate()
        if self._panel is not None:
            self._panel.clear()
            if self._panel.isVisible():
                self._kick()

    def on_chain_changed(self) -> None:
        self.on_account_changed(self.host.selected_address if self.host else None)

    def on_activated(self) -> None:
        self._kick()

    def shutdown(self) -> None:
        self._invalidate()

    # --- internals --------------------------------------------------------
    def _current_view(self) -> tuple[int, str] | None:
        if self.host is None:
            return None
        addr = self.host.selected_address
        if not addr:
            return None
        return (self.host.current_chain().chain_id, addr.lower())

    def _invalidate(self) -> None:
        self._epoch += 1
        self._loaded_for = None
        self._stop_scan()
        self._abort_queue()
        self._reconcile_pending.clear()
        self._reconcile_timer.stop()

    def _stop_scan(self) -> None:
        # The host deleteLater()s a finished worker (ui.py start_worker), so a
        # naturally-completed scan leaves self._scan a stale wrapper whose C++
        # object is gone — requestInterruption() on it raised "already deleted"
        # and aborted the account switch. Guard with isValid, then forget it.
        from shiboken6 import isValid
        scan, self._scan = self._scan, None
        if scan is not None and isValid(scan):
            scan.requestInterruption()

    def _forget_scan(self, worker: QThread) -> None:
        if self._scan is worker:
            self._scan = None

    def _abort_queue(self) -> None:
        if self._queue is not None:
            self._queue.abort()

    # --- modify / revoke --------------------------------------------------
    def _on_modify(self, row: ApprovalRow) -> None:
        self._open_approve(row, f"Modify {row.symbol or 'token'} approval")

    def _on_revoke(self, rows) -> None:
        rows = [r for r in rows if isinstance(r, ApprovalRow)]
        if not rows:
            return
        if len(rows) == 1:
            self._open_approve(rows[0], f"Revoke {rows[0].symbol or 'token'}")
            return
        self._start_revoke_queue(rows)

    def _approve_request(self, row: ApprovalRow):
        """``(SigningRequest, chain)`` for ``approve(spender, 0)`` from the
        selected owner, or ``None`` when there's no host/owner."""
        if self.host is None:
            return None
        owner = self.host.selected_address
        if not owner:
            return None
        from eth_utils import to_checksum_address

        from ...signing import SigningRequest
        chain = self.host.current_chain()
        req = SigningRequest(
            chain_id=chain.chain_id,
            from_addr=to_checksum_address(owner),
            to_addr=to_checksum_address(row.token),
            value_wei=0,
            data=_encode_approve(to_checksum_address(row.spender), 0),
        )
        return req, chain

    def _open_approve(self, row: ApprovalRow, label: str) -> None:
        built = self._approve_request(row)
        if built is None or self.host is None:
            return
        req, chain = built
        pair = (row.token.lower(), row.spender.lower())
        self.host.request_transaction(
            req, chain, label=label,
            on_broadcast=lambda h, p=pair: self._on_action_broadcast(p),
            on_confirmed=lambda rc, p=pair: self._schedule_reconcile(p))

    def _start_revoke_queue(self, rows: list[ApprovalRow]) -> None:
        self._abort_queue()                            # one batch at a time
        queue = RevokeQueue(rows, self._open_revoke_dialog, parent=self)
        queue.row_broadcast.connect(
            lambda row, h: self._on_action_broadcast(
                (row.token.lower(), row.spender.lower())))
        queue.row_confirmed.connect(
            lambda row, rc: self._schedule_reconcile(
                (row.token.lower(), row.spender.lower())))
        queue.finished.connect(self._on_queue_finished)
        self._queue = queue
        queue.start()

    def _open_revoke_dialog(self, row, index, total,
                            on_broadcast, on_confirmed, on_cancel) -> None:
        built = self._approve_request(row)
        if built is None or self.host is None:
            on_cancel()
            return
        req, chain = built
        label = f"Revoke {row.symbol or 'token'} ({index + 1}/{total})"
        self.host.request_transaction(
            req, chain, label=label,
            on_broadcast=on_broadcast, on_confirmed=on_confirmed,
            on_cancel=on_cancel)

    def _on_queue_finished(self, completed_all: bool) -> None:
        self._queue = None
        if self.host is not None and not completed_all:
            self.host.status_message("Revoke batch cancelled")

    def _on_action_broadcast(self, pair: tuple[str, str]) -> None:
        if self._panel is not None:
            self._panel.mark_pending(*pair)

    def _schedule_reconcile(self, pair: tuple[str, str]) -> None:
        self._reconcile_pending.add(pair)
        self._reconcile_timer.start()

    def _run_reconcile(self) -> None:
        pairs = list(self._reconcile_pending)
        self._reconcile_pending.clear()
        if not pairs or self.host is None:
            return
        owner = self.host.selected_address
        if not owner:
            return
        chain = self.host.current_chain()
        epoch = self._epoch
        worker = ReconcileWorker(chain, owner, pairs)
        worker.reconciled.connect(
            lambda c, a, vals, e=epoch: self._on_reconciled(c, a, vals, e))
        self._start(worker)

    def _on_reconciled(self, chain_id, addr_l, values, epoch) -> None:
        if self._panel is None or not self._fresh(chain_id, addr_l, epoch):
            return
        for (token, spender), value in values.items():
            if value > 0:
                self._panel.update_allowance(token, spender, int(value))
            else:
                self._panel.remove_leaf(token, spender)
                self._scan_pairs.discard((token.lower(), spender.lower()))
        self._persist()                              # keep the cache in step with a revoke

    def _kick(self, *, force: bool = False) -> None:
        view = self._current_view()
        if view is None or self._panel is None or self.host is None:
            return
        if not force and self._loaded_for == view:
            return
        addr = self.host.selected_address
        if not addr:
            return
        self._loaded_for = view
        self._epoch += 1
        epoch = self._epoch
        chain = self.host.current_chain()
        self._scan_pairs = set()
        # Instant paint from the cache; only show the progress bar on a cold
        # scan. A warm scan refreshes the tail silently under the shown rows.
        self._panel.clear()
        cached = self._cache.load(chain.chain_id, addr)
        known_pairs: set[tuple[str, str]] = set()
        if cached and cached[0]:
            rows, self._last_block = cached
            self._panel.append_rows(rows)
            known_pairs = {(r.token.lower(), r.spender.lower()) for r in rows}
        else:
            self._last_block = 0
            self._panel.begin_scan()
        snapshot = self._disk_cache.load(chain.chain_id, addr) or []
        worker = ScanWorker(chain, addr, self._source, snapshot, self._metadata,
                            label_source=self._label_source,
                            price_source=self._price_source,
                            known_pairs=known_pairs)
        worker.batch_fetched.connect(self._on_batch)
        worker.rows_ready.connect(
            lambda c, a, rows, e=epoch: self._on_rows(c, a, rows, e))
        worker.progress.connect(
            lambda c, a, s, t, e=epoch: self._on_progress(c, a, s, t, e))
        worker.scan_done.connect(
            lambda c, a, ok, e=epoch: self._on_done(c, a, ok, e))
        # Drop our reference the moment it finishes, before the host's
        # deleteLater() runs — so _stop_scan never reaches a dead wrapper.
        worker.finished.connect(lambda w=worker: self._forget_scan(w))
        self._scan = worker
        self._start(worker)

    def _start(self, worker: QThread) -> None:
        if self.host is not None and hasattr(self.host, "start_worker"):
            self.host.start_worker(worker)
            return
        self._workers.add(worker)
        worker.finished.connect(lambda w=worker: self._workers.discard(w))
        worker.start()

    def _fresh(self, chain_id: int, addr_l: str, epoch: int) -> bool:
        return epoch == self._epoch and self._current_view() == (chain_id, addr_l)

    def _on_batch(self, chain_id, addr_l, txs) -> None:
        # MAIN thread: the only writer to the tx cache (no lock on it).
        from ...transactions_cache import merge_txs
        existing = self._disk_cache.load(chain_id, addr_l) or []
        merged = merge_txs(list(txs), existing)
        if merged != existing:
            self._disk_cache.save(chain_id, addr_l, merged)

    def _on_rows(self, chain_id, addr_l, rows, epoch) -> None:
        if self._panel is not None and self._fresh(chain_id, addr_l, epoch):
            self._panel.append_rows(rows)
            self._scan_pairs |= {(r.token.lower(), r.spender.lower()) for r in rows}

    def _on_progress(self, chain_id, addr_l, seen, total, epoch) -> None:
        if self._panel is not None and self._fresh(chain_id, addr_l, epoch):
            self._panel.set_progress(int(seen), int(total or 0))

    def _on_done(self, chain_id, addr_l, complete, epoch) -> None:
        if self._panel is None or not self._fresh(chain_id, addr_l, epoch):
            return
        if complete:
            # Drop cached caps the fresh scan didn't re-confirm (revoked
            # elsewhere), then persist the reconciled state + how far we scanned.
            self._panel.prune_to(self._scan_pairs)
            self._persist()
        else:
            # Stopped before reaching the account's oldest txs — don't treat the
            # view as loaded, so the next activation resumes and finishes the
            # un-scanned older tail (the tx cache remembers where we stopped, so
            # the resume skips re-paging what's already cached).
            self._loaded_for = None
        self._panel.finish_scan(bool(complete))

    def _persist(self) -> None:
        view = self._current_view()
        if view is None or self._panel is None:
            return
        cid, addr = view
        blocks = [t.block_number for t in (self._disk_cache.load(cid, addr) or [])]
        self._last_block = max(blocks, default=self._last_block)
        self._cache.save(cid, addr, self._panel.all_rows(), self._last_block)

    def _on_copied(self, text: str) -> None:
        if self.host is not None:
            self.host.status_message(f"Copied {text}")
