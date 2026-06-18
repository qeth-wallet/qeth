"""ENS plugin — a tab showing the ENS names an account owns as a tree.

Read-only v1 (see ``docs/ens-app.md``): keyless discovery via BENS, names →
owned subdomains → records (address, IPFS contenthash, text records) as tree
items with distinct icons, expiry status, caching, custom-name pinning, and
context actions (open in the ENS app, copy name / resolved address).

Isolated like every plugin: depends only on the ``Host`` protocol + the Qt-free
``qeth.ens_app`` data layer, mounted with one ``add_plugin`` line. ENS is
mainnet-only, so the plugin pins to chain 1 regardless of the viewing chain.
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar, Optional

from PySide6.QtCore import QEvent, QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import (
    QBrush, QColor, QDesktopServices, QIcon, QPalette,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHeaderView, QInputDialog, QLineEdit, QMenu, QPushButton,
    QSizePolicy, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTableWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..ens_app import (
    ENS_APP_URL, TEXT_KEYS, VERIFY_WAIT_S, EnsCache, EnsName, EnsNode,
    EnsRecords, OwnershipCheck, build_tree, expiry_status, fetch_name,
    lookup_owned_names, name_warning, read_records, verified_read_records,
    verify_names,
)
from ..plugin import Plugin

log = logging.getLogger("qeth.plugins.ens")

ENS_CHAIN_ID = 1                       # ENS lives on Ethereum mainnet
_OWNERSHIP_WAIT_S = 25.0                # cold-sidecar grace for the ownership pass
# After a write confirms we re-read at the chain head. The fast RPC read already
# reflects the new value, but Helios's *verified* head lags the execution RPC by
# a few slots, so its first proof can still show the OLD value. On a post-write
# (forced) refresh, retry the verified read until it agrees with the freshly-read
# value — so the ✓ lands on the new value instead of repainting the old one.
_VERIFY_CATCHUP_TRIES = 8
_VERIFY_CATCHUP_DELAY_S = 4.0
_NAME_ROLE = Qt.ItemDataRole.UserRole          # stores the EnsName on a row
_LOADED_ROLE = Qt.ItemDataRole.UserRole + 1    # records-loaded flag
_VALUE_ROLE = Qt.ItemDataRole.UserRole + 2     # copyable value on a record row
_UNSAFE_ROLE = Qt.ItemDataRole.UserRole + 3    # confusable / non-normalized name
_STATUS_ROLE = Qt.ItemDataRole.UserRole + 4    # "ok" | "warn" — the line's status
_EXPIRY_SORT_ROLE = Qt.ItemDataRole.UserRole + 5  # expiry timestamp for sorting
_TYPE_RANK_ROLE = Qt.ItemDataRole.UserRole + 6    # sort tier (see _RANK_*)

_NAME_COL = 0
_EXPIRES_COL = 1

# Rows sort by type tier first (alphabetically within each tier): real domains
# (2LDs), then subdomains — including orphan subdomains that surface at the top
# level — then a name's contenthash, then its other records.
_RANK_DOMAIN = 0
_RANK_SUBDOMAIN = 1
_RANK_CONTENT = 2
_RANK_RECORD = 3

_WARN_COLOR = QColor(176, 0, 32)               # red — scam/look-alike marker


def _kind_rank(item: QTreeWidgetItem) -> int:
    """A name's children sort by tier: subdomains, then contenthash, then other
    records (the _RANK_* set, stored per item). Unset → sorts with the records."""
    r = item.data(0, _TYPE_RANK_ROLE)
    return r if r is not None else _RANK_RECORD


class _SortItem(QTreeWidgetItem):
    """Tree item with column-aware sorting: subdomains always group above records
    (regardless of column or direction); within a group the Expires column orders
    by real expiry timestamp (not the displayed 'expiring soon' / date text) and
    names sort case-insensitively. Names with no expiry sort last."""

    def __lt__(self, other: "QTreeWidgetItem") -> bool:
        tree = self.treeWidget()
        # Type grouping is primary and stays subdomains-first even when the
        # column is sorted descending (Qt reverses the comparator, so flip).
        ra, rb = _kind_rank(self), _kind_rank(other)
        if ra != rb:
            desc = (tree is not None and tree.header().sortIndicatorOrder()
                    == Qt.SortOrder.DescendingOrder)
            return (ra > rb) if desc else (ra < rb)
        col = tree.sortColumn() if tree is not None else _NAME_COL
        if col == _EXPIRES_COL:
            a = self.data(_EXPIRES_COL, _EXPIRY_SORT_ROLE)
            b = other.data(_EXPIRES_COL, _EXPIRY_SORT_ROLE)
            return (a if a is not None else float("inf")) < \
                   (b if b is not None else float("inf"))
        return self.text(col).casefold() < other.text(col).casefold()

# Verified-via-Helios markers. On the address column a leading glyph (not
# trailing) survives the tree's ElideMiddle, which keeps both ends visible.
_OWNED_TIP = "Owner — cryptographically verified"
_CONTROL_TIP = "Subdomain you control — verified"
_RESOLVED_TIP = "Address cryptographically verified"
_RECORD_TIP = "Cryptographically verified"
_WRAPPED_NOTE = " · wrapped"

# Expiry-status → (column-1 text, colour). Theme-neutral fixed colours: this is
# a status chip, not palette-driven text.
_EXPIRY_STYLE = {
    "active":   (None,            None),
    "expiring": ("expiring soon", QColor(180, 95, 0)),     # amber
    "grace":    ("in grace",      QColor(176, 0, 32)),      # red
    "expired":  ("expired",       QColor(120, 120, 120)),   # grey
    "none":     (None,            None),
}


def _icon(theme_name: str, fallback: QStyle.StandardPixmap) -> QIcon:
    """A themed icon, falling back to a built-in Qt standard icon so something
    always renders regardless of the user's icon theme."""
    ic = QIcon.fromTheme(theme_name)
    if not ic.isNull():
        return ic
    app = QApplication.instance()
    if isinstance(app, QApplication):
        return app.style().standardIcon(fallback)
    return QIcon()


_TABLE_ROW_H = 0


def _table_row_height() -> int:
    """The style's natural QTableView row height. A QTreeView's rows come out
    shorter under most styles, so the ENS tree looked cramped next to the
    token/tx tables; the delegate raises rows to this. Cached (style is fixed)."""
    global _TABLE_ROW_H
    if not _TABLE_ROW_H:
        ref = QTableWidget(0, 0)
        _TABLE_ROW_H = ref.verticalHeader().defaultSectionSize()
        ref.deleteLater()
    return _TABLE_ROW_H


def _selection_color(palette: QPalette, focused: bool) -> QColor:
    """The row-selection fill: the full highlight when the list is focused, a
    quieter highlight↔base blend when not (so an inactive panel recedes)."""
    hl = palette.color(QPalette.ColorRole.Highlight)
    if focused:
        return hl
    base = palette.color(QPalette.ColorRole.Base)
    return QColor((hl.red() * 11 + base.red() * 9) // 20,
                  (hl.green() * 11 + base.green() * 9) // 20,
                  (hl.blue() * 11 + base.blue() * 9) // 20)


class _EnsItemDelegate(QStyledItemDelegate):
    """Make the ENS tree look like the wallet / token / tx lists: table-height
    rows, focus-aware selection (full highlight when focused, a quieter blend
    when not — so an inactive panel recedes), and no hover/focus-rect tint.

    Paints only the cell rect; the branch (+/-) column's highlight is handled by
    ``_EnsTree.drawBranches`` so the indicator stays drawn on top of it.

    Self-contained on purpose: the main window's delegate reads wallet-only
    roles (and paints account pills), and its label role collides with this
    tree's _LOADED_ROLE."""

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(size.height(), _table_row_height()))
        return size

    def _selection_fill(self, option) -> "Optional[QColor]":
        if not (option.state & QStyle.StateFlag.State_Selected):
            return None
        view = self.parent()
        focused = isinstance(view, QWidget) and view.hasFocus()
        return _selection_color(option.palette, focused)

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        opt.state &= ~QStyle.StateFlag.State_MouseOver   # no hover tint
        opt.state &= ~QStyle.StateFlag.State_HasFocus    # no dotted focus rect
        fill = self._selection_fill(option)
        if fill is not None:
            painter.fillRect(option.rect, fill)
            tc = option.palette.color(
                QPalette.ColorRole.HighlightedText if fill.lightness() < 140
                else QPalette.ColorRole.Text)
            opt.state &= ~QStyle.StateFlag.State_Selected   # bg already painted
            for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText):
                opt.palette.setColor(role, tc)
            for role in (QPalette.ColorRole.Base, QPalette.ColorRole.AlternateBase):
                opt.palette.setColor(role, fill)
        super().paint(painter, opt, index)


class _EnsTree(QTreeWidget):
    """Tree that highlights the branch (+/-) gutter to match a selected row,
    then lets Qt draw the indicator on top — so the +/- stays visible on the
    highlight instead of being painted over."""

    def drawBranches(self, painter, rect, index) -> None:
        model = self.selectionModel()
        if model is not None and model.isSelected(index):
            painter.fillRect(rect, _selection_color(self.palette(), self.hasFocus()))
        super().drawBranches(painter, rect, index)


class _RightIconDelegate(_EnsItemDelegate):
    """Status column: the focus-aware selection background (from the base) plus
    the status icon painted flush against the right edge (the default delegate
    left-aligns the decoration) so the address column keeps the rest."""

    def paint(self, painter, option, index) -> None:
        fill = self._selection_fill(option)
        if fill is not None:
            painter.fillRect(option.rect, fill)
        ic = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(ic, QIcon) and not ic.isNull():
            sz = option.decorationSize
            r = option.rect
            x = r.right() - sz.width() - 2
            y = r.top() + (r.height() - sz.height()) // 2
            ic.paint(painter, x, y, sz.width(), sz.height())


def _record_rows(rec: EnsRecords) -> "list[tuple[str, str, str]]":
    """Flatten records to (icon-key, label, value) rows for the tree."""
    rows: list[tuple[str, str, str]] = []
    for coin, addr in rec.addresses.items():
        rows.append(("address", f"address ({coin})" if coin != "60" else "address", addr))
    if rec.contenthash:
        rows.append(("content", "content", rec.contenthash))
    for key, val in rec.texts.items():
        rows.append(("text", key, val))
    return rows


class EnsNamesWorker(QThread):
    """Discover the names owned by an address (BENS) + pull details for any
    custom-pinned names, off the Qt thread. Emits ``ready(address, names)``."""

    ready = Signal(str, object)        # (address, list[EnsName])

    def __init__(self, address: str, custom_names: "list[str]", parent=None):
        super().__init__(parent)
        self._address = address
        self._custom = list(custom_names)

    def run(self) -> None:
        names = lookup_owned_names(ENS_CHAIN_ID, self._address)
        have = {n.name.lower() for n in names}
        for cn in self._custom:
            if cn.lower() in have:
                continue
            n = fetch_name(ENS_CHAIN_ID, cn) or EnsName(cn, source="custom")
            names.append(n)
        self.ready.emit(self._address, names)


class EnsRecordsWorker(QThread):
    """Read one name's resolver records (lazy, on expand) in two phases so the
    UI never blocks on Helios: first a fast UNVERIFIED read (~1.5 s) for an
    instant paint, then — if a sidecar can prove them — a verified re-read that
    upgrades the rows to ✓. Emits ``ready(name, records, verified, ok)`` once or
    twice — ``ok`` False means the read didn't land (a glitch), so the consumer
    must keep what's already shown rather than wipe it."""

    ready = Signal(str, object, bool, bool)  # (name, EnsRecords, verified, ok)

    def __init__(self, chain, name: str, parent=None,
                 *, wait_s: float = VERIFY_WAIT_S, client=None,
                 resolver: "Optional[str]" = None, catchup: bool = False):
        super().__init__(parent)
        self._chain = chain
        self._name = name
        self._wait_s = wait_s
        self._client = client          # warm EthClient reused across expands
        self._resolver = resolver      # cached per-name resolver (skips a round)
        # Forced (post-write) re-read: wait for Helios's verified head to catch
        # up to the value the fast read already saw, so the ✓ lands on the NEW
        # value rather than the sidecar's momentarily-stale older one.
        self._catchup = catchup

    def run(self) -> None:
        rec, ok = read_records(self._chain, self._name,
                               client=self._client, resolver=self._resolver)
        self.ready.emit(self._name, rec, False, ok)
        tries = _VERIFY_CATCHUP_TRIES if self._catchup else 1
        for attempt in range(tries):
            vrec, verified = verified_read_records(
                self._chain, self._name,
                wait_s=self._wait_s if attempt == 0 else 0.0)
            if not verified:           # no sidecar / can't prove → stop trying
                return
            # Emit once Helios agrees with the head read (or the read didn't
            # land so there's nothing to match, or we're out of catch-up budget).
            if not ok or vrec == rec or attempt == tries - 1:
                self.ready.emit(self._name, vrec, True, True)
                return
            self.msleep(int(_VERIFY_CATCHUP_DELAY_S * 1000))


class EnsVerifyWorker(QThread):
    """Verify the displayed names against on-chain state through Helios — in two
    batched multicalls (ownership + resolved-address), not per-name. Emits
    ``ready(address, states, verified)`` where ``states`` is
    ``{name_lower: OwnershipCheck}``. ``verified`` is True only when a Helios
    sidecar proved the reads; otherwise ``states`` is empty and the rows stay
    unbadged (never blocked, never trusting an unverified re-read)."""

    ready = Signal(str, object, bool)        # (address, states, verified)

    def __init__(self, chain, address: str, names: "list[str]", parent=None,
                 *, wait_s: float = VERIFY_WAIT_S):
        super().__init__(parent)
        self._chain = chain
        self._address = address
        self._names = list(names)
        self._wait_s = wait_s

    def run(self) -> None:
        states, verified = verify_names(
            self._chain, self._names, wait_s=self._wait_s)
        self.ready.emit(self._address, states, verified)


class EnsPanel(QWidget):
    """The tree widget: names → owned subdomains → records."""

    add_custom_requested = Signal()
    records_requested = Signal(str)    # name → load its records (lazy)
    write_requested = Signal(str, str)  # (name, kind) — kind: addr|content|text|record|subdomain
    edit_record_requested = Signal(str, str, str)  # (name, label, value)

    # Trailing column = verification status, shown as a fixed-size icon (a text
    # ✓/⚠ glyph gets emoji presentation on some themes and changes the row
    # height). The one icon covers the whole line — name ownership and the
    # resolved value together.
    COLS: ClassVar[list[str]] = ["Name", "Expires", "Resolves to", ""]
    _STATUS_COL = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items_by_name: dict[str, QTreeWidgetItem] = {}
        self._writable: "set[str]" = set()   # names the user can sign writes for
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tree = _EnsTree()
        self.tree.setColumnCount(len(self.COLS))
        self.tree.setHeaderLabels(self.COLS)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setIconSize(QSize(16, 16))
        # Resolved addresses are full 42-char strings shown in the stretch
        # column; let Qt middle-elide them as the tab narrows (same as the
        # wallet address list) instead of pre-shortening to 0x…tail.
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)      # the address stretches, not status
        hdr.setMinimumSectionSize(16)         # let the address squeeze small
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)  # address
        # Status: a minimal, right-hugging icon column — fixed to the icon width
        # so the address gets every other pixel and squeezes first.
        hdr.setSectionResizeMode(
            self._STATUS_COL, QHeaderView.ResizeMode.Fixed)
        hdr.resizeSection(self._STATUS_COL, 22)
        # Consistent look with the other tabs: table-height rows, focus-aware
        # selection, no hover tint — via a delegate on every column (the status
        # column's variant also right-aligns its icon). A focus repaint on the
        # tree keeps the focused/unfocused selection swap immediate.
        self.tree.setItemDelegate(_EnsItemDelegate(self.tree))
        self.tree.setItemDelegateForColumn(
            self._STATUS_COL, _RightIconDelegate(self.tree))
        self.tree.installEventFilter(self)
        # Click headers to sort by name / expiry (either direction); the
        # Expires column sorts by real timestamp via _SortItem. Default: name A→Z.
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(_NAME_COL, Qt.SortOrder.AscendingOrder)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_menu)
        layout.addWidget(self.tree)

        self._domain_icon = _icon("emblem-web", QStyle.StandardPixmap.SP_DriveNetIcon)
        self._sub_icon = _icon("folder", QStyle.StandardPixmap.SP_DirIcon)
        # Status-column icons: verified-ok vs warning.
        self._ok_icon = _icon("emblem-ok",
                              QStyle.StandardPixmap.SP_DialogApplyButton)
        self._warn_icon = _icon("dialog-warning",
                                QStyle.StandardPixmap.SP_MessageBoxWarning)
        self._rec_icons = {
            "address": _icon("avatar-default", QStyle.StandardPixmap.SP_FileIcon),
            "content": _icon("folder-remote", QStyle.StandardPixmap.SP_FileLinkIcon),
            "text": _icon("text-x-generic", QStyle.StandardPixmap.SP_FileIcon),
        }

    # --- rendering --------------------------------------------------------

    def populate(self, roots: "list[EnsNode]", now_ts: int) -> None:
        # Bulk-insert with sorting off (else the tree re-sorts on every add),
        # then re-enable — which applies the user's current sort column/order.
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        self._items_by_name.clear()
        for node in roots:
            self.tree.addTopLevelItem(self._build(node, now_ts, is_sub=False))
        self.tree.setSortingEnabled(True)

    def _build(self, node: EnsNode, now_ts: int, *, is_sub: bool) -> QTreeWidgetItem:
        n = node.name
        status = expiry_status(n.expiry_ts, now_ts)
        text, colour = _EXPIRY_STYLE.get(status, (None, None))
        exp_col = text or (_fmt_expiry(n.expiry_ts) if n.expiry_ts else "")
        item = _SortItem([n.name, exp_col, n.resolved_address or ""])
        # A subdomain (structurally — even an orphan surfacing at the top level)
        # reads differently from a real 2LD: a folder icon, and it sorts after
        # the domains.
        sub = n.is_subdomain
        item.setIcon(0, self._sub_icon if sub else self._domain_icon)
        item.setData(0, _NAME_ROLE, n)
        item.setData(0, _LOADED_ROLE, False)
        item.setData(0, _TYPE_RANK_ROLE,
                     _RANK_SUBDOMAIN if sub else _RANK_DOMAIN)
        item.setData(_EXPIRES_COL, _EXPIRY_SORT_ROLE, n.expiry_ts)
        if colour is not None:
            item.setForeground(1, QBrush(colour))
        # Confusable / non-normalized name → a warning status (shown immediately,
        # no wait on verification) + a red name, with an explaining tooltip.
        # Flagged so verification never lends it a legitimizing ✓.
        warn = name_warning(n.name)
        if warn is not None:
            item.setData(0, _UNSAFE_ROLE, True)
            item.setForeground(0, QBrush(_WARN_COLOR))
            self._set_status(item, "warn", warn)
            item.setToolTip(0, f"⚠ {warn}")
        elif n.source == "custom":
            item.setToolTip(0, f"{n.name} — pinned")
        self._items_by_name[n.name.lower()] = item
        for child in node.children:
            item.addChild(self._build(child, now_ts, is_sub=True))
        # A name with no owned subdomains still needs to be expandable so the
        # user can pull its records — give it a lazy placeholder.
        if not node.children:
            item.addChild(_SortItem(["…loading records"]))
        return item

    def add_records(self, name: str, rec: EnsRecords,
                    verified: bool = False) -> None:
        item = self._items_by_name.get(name.lower())
        if item is None:
            return
        item.setData(0, _LOADED_ROLE, True)
        # Drop the placeholder AND any previously-rendered record rows (a re-emit
        # — fast→verified upgrade, or a refresh — replaces them). Record rows and
        # the placeholder carry no _NAME_ROLE; owned subdomains do, so they stay.
        for i in range(item.childCount() - 1, -1, -1):
            ch = item.child(i)
            if ch.data(0, _NAME_ROLE) is None:
                item.removeChild(ch)
        rows = _record_rows(rec)
        if not rows:
            note = _SortItem(["no records", "", ""])
            note.setData(0, _TYPE_RANK_ROLE, _RANK_RECORD)
            note.setForeground(0, QBrush(QColor(120, 120, 120)))
            item.addChild(note)
            return
        for icon_key, label, value in rows:
            ch = _SortItem([label, "", value])
            ch.setData(0, _TYPE_RANK_ROLE,
                       _RANK_CONTENT if icon_key == "content" else _RANK_RECORD)
            ch.setIcon(0, self._rec_icons.get(icon_key, self._rec_icons["text"]))
            ch.setData(0, _VALUE_ROLE, value)
            ch.setToolTip(2, value)
            if verified:                # status icon, same as the name rows
                self._set_status(ch, "ok", _RECORD_TIP)
            item.addChild(ch)

    def update_resolved(self, name: str, address: "Optional[str]") -> None:
        """Update a name row's 'Resolves to' column from a fresh head read of its
        ETH address record — so a setAddr change shows on the (possibly
        collapsed) name row, not just in the expanded records. ``None`` clears
        it. Keeps the stored EnsName in sync for copy / sort / mismatch checks."""
        item = self._items_by_name.get(name.lower())
        if item is None:
            return
        n = item.data(0, _NAME_ROLE)
        if isinstance(n, EnsName):
            n.resolved_address = address
        item.setText(2, address or "")
        item.setToolTip(2, _RESOLVED_TIP if address else "")

    def mark_verified(self, states: "dict[str, OwnershipCheck]",
                      address: str) -> "list[str]":
        """Apply the batched on-chain verification to the rows and return the
        names DROPPED as indexer lies.

        Ownership is the chain's call: a name the chain proves you control gets a
        ✓; a discovered name the chain says you DON'T own (controller and
        registrant are someone else) isn't real — the indexer over-reported it —
        so we remove it from the tree entirely. Pinned (custom) names are never
        removed: watching a name you don't own is intentional. The resolved
        address is replaced with the proven head value (no mismatch alarm — the
        indexer just lagged). Only ever called with proof-verified state, so
        acting on it is sound."""
        removed: list[str] = []
        for name_l, st in states.items():
            item = self._items_by_name.get(name_l)
            if item is None:
                continue
            n = item.data(0, _NAME_ROLE)
            is_custom = isinstance(n, EnsName) and n.source == "custom"
            # The chain definitively says this address owns neither role (a
            # different owner, OR no owner — the node doesn't exist): an indexer
            # lie. Drop it. Pinned names are exempt; a failed read isn't a drop.
            if st.disowned_by(address) and not is_custom:
                self._remove_item(item, name_l)
                removed.append(name_l)
                continue
            if item.data(0, _UNSAFE_ROLE):
                continue          # keep the ⚠; never add a ✓ to a look-alike
            # Resolved-to: the proof is read at the chain head, so it IS the
            # current truth — replace the indexer's hint with it (and make it
            # copyable). No "mismatch" alarm: a difference just means the indexer
            # lagged; we simply show the proven, most-recent value.
            if st.resolved_address:
                if isinstance(n, EnsName):
                    n.resolved_address = st.resolved_address
                item.setText(2, st.resolved_address)
                item.setToolTip(2, _RESOLVED_TIP)
            # One status icon for the whole line (ownership + resolution).
            if st.owned_by(address):
                is_sub = isinstance(n, EnsName) and n.is_subdomain
                tip = _CONTROL_TIP if is_sub else _OWNED_TIP
                if st.wrapped:
                    tip += _WRAPPED_NOTE
                self._set_status(item, "ok", tip)
                item.setToolTip(0, tip)
        return removed

    def _set_status(self, item: QTreeWidgetItem, status: str,
                    tooltip: str) -> None:
        """Set the trailing status column's icon + tooltip for a line, ``status``
        in ``{"ok", "warn"}``. An icon (not a text ✓/⚠) keeps the row height
        uniform regardless of the theme's emoji rendering."""
        item.setIcon(self._STATUS_COL,
                     self._ok_icon if status == "ok" else self._warn_icon)
        item.setData(0, _STATUS_ROLE, status)
        item.setToolTip(self._STATUS_COL, tooltip)

    def _remove_item(self, item: QTreeWidgetItem, name_l: str) -> None:
        """Drop a row (top-level or subdomain) from the tree + the index."""
        parent = item.parent()
        if parent is not None:
            parent.removeChild(item)
        else:
            idx = self.tree.indexOfTopLevelItem(item)
            if idx >= 0:
                self.tree.takeTopLevelItem(idx)
        self._items_by_name.pop(name_l, None)

    # --- interaction ------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        # Repaint on focus change so the focused↔unfocused selection swap
        # (hand-painted by the delegate) shows immediately, not on next nudge.
        if obj is self.tree and event.type() in (
                QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.tree.viewport().update()
        return super().eventFilter(obj, event)

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        n = item.data(0, _NAME_ROLE)
        if isinstance(n, EnsName) and not item.data(0, _LOADED_ROLE):
            item.setData(0, _LOADED_ROLE, True)   # guard against re-emit
            self.records_requested.emit(n.name)

    def set_writable(self, names: "set[str]") -> None:
        """Names (lower-case) the user can sign writes for — gates the edit
        actions in the context menu."""
        self._writable = set(names)

    def _on_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self.tree)
        n = item.data(0, _NAME_ROLE)
        value = item.data(0, _VALUE_ROLE)
        if isinstance(n, EnsName):
            menu.addAction("Open in ENS app", lambda: QDesktopServices.openUrl(
                QUrl(ENS_APP_URL.format(name=n.name))))
            menu.addAction("Copy name", lambda: _clip(n.name))
            if n.resolved_address:
                menu.addAction("Copy resolved address",
                               lambda: _clip(n.resolved_address))
            if n.name.lower() in self._writable:
                menu.addSeparator()
                nm = n.name
                menu.addAction("Set ETH address…",
                               lambda: self.write_requested.emit(nm, "addr"))
                menu.addAction("Set content (IPFS)…",
                               lambda: self.write_requested.emit(nm, "content"))
                menu.addAction("Set text record…",
                               lambda: self.write_requested.emit(nm, "text"))
                menu.addAction("Add / change record…",
                               lambda: self.write_requested.emit(nm, "record"))
                menu.addSeparator()
                menu.addAction("Add subdomain…",
                               lambda: self.write_requested.emit(nm, "subdomain"))
        elif value:
            menu.addAction("Copy value", lambda: _clip(str(value)))
            # Record row → offer to edit it (the parent name must be writable).
            parent = item.parent()
            pn = parent.data(0, _NAME_ROLE) if parent is not None else None
            if (isinstance(pn, EnsName) and pn.name.lower() in self._writable):
                label = item.text(0)
                menu.addAction("Edit…", lambda: self.edit_record_requested.emit(
                    pn.name, label, str(value)))
        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(pos))


def _clip(text: str) -> None:
    QApplication.clipboard().setText(text)


def _checksum(text: str) -> "Optional[str]":
    """Checksum a 0x address, or None if it isn't a valid address."""
    from eth_utils import is_address, to_checksum_address
    s = (text or "").strip()
    if not s or not is_address(s):
        return None
    return to_checksum_address(s)


def _fmt_expiry(ts: int) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


# Record types in the general chooser → (label, needs-key, needs-coin).
_RECORD_KINDS = ["ETH address", "Content (IPFS)", "Text record",
                 "Other-chain address"]


class _RecordDialog(QDialog):
    """Choose a record type and enter its value. ``preset`` locks the type
    (used by the quick 'Set text record…' entry); otherwise the type combo is
    shown (the general 'Add / change record…')."""

    def __init__(self, name: str, *, preset: "Optional[str]" = None,
                 key: str = "", coin: str = "", value: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Record · {name}")
        self._form = form = QFormLayout(self)
        self.kind = QComboBox()
        self.kind.addItems(_RECORD_KINDS)
        if preset:
            self.kind.setCurrentText(preset)
            self.kind.setEnabled(False)
        else:
            form.addRow("Type", self.kind)
        from .. import ens_write
        self.key = QComboBox()
        self.key.setEditable(True)
        self.key.addItems(TEXT_KEYS)
        if key:
            self.key.setCurrentText(key)
        self.coin = QComboBox()
        # Only coins whose address is a 20-byte 0x value (ETC + the ENSIP-11 EVM
        # chains) — we can encode those from a plain address; BTC/LTC/DOGE need
        # chain-specific encoders we don't ship.
        self.coin.addItems([c for c, t in ens_write.COIN_TYPES.items()
                            if c != "ETH" and (t == 61 or t >= 0x80000000)])
        if coin:
            self.coin.setCurrentText(coin)
        self.value = QLineEdit(value)
        self.value.setMinimumWidth(360)
        form.addRow("Key", self.key)
        form.addRow("Coin", self.coin)
        form.addRow("Value", self.value)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.kind.currentTextChanged.connect(self._sync)
        self._sync(self.kind.currentText())

    def _sync(self, kind: str) -> None:
        self._form.setRowVisible(self.key, kind == "Text record")
        self._form.setRowVisible(self.coin, kind == "Other-chain address")
        hint = {"ETH address": "0x…", "Content (IPFS)": "ipfs://…",
                "Text record": "value", "Other-chain address": "0x…"}
        self.value.setPlaceholderText(hint.get(kind, ""))

    def result_values(self) -> "tuple[str, str, str]":
        """(kind, key-or-coin, value)."""
        kind = self.kind.currentText()
        extra = (self.key.currentText() if kind == "Text record"
                 else self.coin.currentText() if kind == "Other-chain address"
                 else "")
        return kind, extra, self.value.text().strip()


class _SubnodeDialog(QDialog):
    """Add a subdomain: label + owner."""

    def __init__(self, parent_name: str, self_addr: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Add subdomain of {parent_name}")
        form = QFormLayout(self)
        self.label = QLineEdit()
        self.label.setPlaceholderText("label  (→ label." + parent_name + ")")
        self.owner = QLineEdit(self_addr or "")
        self.owner.setMinimumWidth(360)
        form.addRow("Subdomain", self.label)
        form.addRow("Owner", self.owner)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> "tuple[str, str]":
        return self.label.text().strip(), self.owner.text().strip()


class EnsPlugin(Plugin):
    name = "ENS"

    def __init__(self, store):
        super().__init__()
        self._store = store
        self._cache = EnsCache()
        self._panel: Optional[EnsPanel] = None
        self._loaded_for: Optional[str] = None
        self._add_btn: Optional[QPushButton] = None
        # In-memory records cache (name → (records, verified)) layered over the
        # disk cache, so re-expanding a name is instant within a session too.
        # Both the fast (unverified RPC) and the Helios reads are at the chain
        # HEAD, so ``verified`` True means "proven at latest".
        self._rec_cache: "dict[str, tuple[EnsRecords, bool]]" = {}
        # Names whose records we're re-reading because a write to them just
        # confirmed. For these the head read is authoritative enough to also
        # CLEAR the name-row address (a setAddr to 0x0) — outside this set a
        # records read only sets a present address, never clears one (an absent
        # on-chain addr can just mean the name resolves offchain via CCIP).
        self._force_reread: "set[str]" = set()
        # Per-name resolver (from the ownership pass) — lets a record read skip
        # its resolver-lookup round-trip. Refreshed every load (self-heals a
        # re-pointed resolver). And a warm EthClient reused across record reads
        # so each expand doesn't pay a fresh TLS handshake.
        self._resolver_cache: "dict[str, str]" = {}
        self._read_client = None
        # Names the chain proved this address does NOT own — indexer lies we
        # drop and keep filtered out of re-renders this session. Reset per
        # account; never persisted (a stale denial must never hide a real name).
        self._denied: "set[str]" = set()
        # Write state: the EnsName + on-chain ownership facts per name, so the
        # write actions know the resolver, wrapped flag, and parent expiry.
        self._names_by_l: "dict[str, EnsName]" = {}
        self._owned: "set[str]" = set()      # owned by the selected address
        self._wrapped: "set[str]" = set()    # held by the NameWrapper

    # --- plugin contract --------------------------------------------------

    def widget(self) -> QWidget:
        if self._panel is None:
            self._panel = EnsPanel()
            self._panel.records_requested.connect(self._on_records_requested)
            self._panel.add_custom_requested.connect(self._on_add_custom)
            self._panel.write_requested.connect(self._on_write_requested)
            self._panel.edit_record_requested.connect(self._on_edit_record)
        return self._panel

    def action_widgets(self) -> "list[QWidget]":
        if self._add_btn is None:
            # Match the Tokens pane's add button exactly: a flat 28×28
            # list-add ("+") icon button, not a framed text button.
            btn = QPushButton()
            btn.setIcon(QIcon.fromTheme(
                "list-add",
                btn.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder)))
            btn.setToolTip("Add a name")
            btn.setFlat(True)
            btn.setMaximumSize(28, 28)
            btn.setIconSize(QSize(16, 16))
            btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(self._on_add_custom)
            self._add_btn = btn
        assert self._add_btn is not None
        return [self._add_btn]

    def on_account_changed(self, address: Optional[str]) -> None:
        self._load(address)

    def on_activated(self) -> None:
        if self.host is not None and self._loaded_for != self.host.selected_address:
            self._load(self.host.selected_address)

    # --- loading ----------------------------------------------------------

    def _mainnet(self):
        host = self.host
        if host is not None and hasattr(host, "chain_by_id"):
            ch = host.chain_by_id(ENS_CHAIN_ID)
            if ch is not None:
                return ch
        from ..chains import DEFAULT_CHAINS
        return next((c for c in DEFAULT_CHAINS if c.chain_id == ENS_CHAIN_ID), None)

    def _load(self, address: Optional[str]) -> None:
        if self._panel is None:
            return
        if address != self._loaded_for:
            self._denied.clear()              # denials are per-account
            self._owned.clear()
            self._wrapped.clear()
            if self._panel is not None:
                self._panel.set_writable(set())
        self._loaded_for = address
        if not address:
            self._panel.populate([], int(time.time()))
            return
        # Warm a mainnet Helios sidecar now so on-chain verification of the
        # names is ready (or close) by the time discovery returns. Cheap (one
        # Popen) and a no-op when Helios is absent/disabled.
        chain = self._mainnet()
        if chain is not None:
            try:
                from ..helios import prewarm
                prewarm(chain)
            except Exception:
                log.debug("helios prewarm failed", exc_info=True)
        cached = self._cache.load(ENS_CHAIN_ID, address)
        if cached is not None:
            self._render(cached)
        self._on_refresh()

    def _on_refresh(self) -> None:
        host = self.host
        addr = host.selected_address if host is not None else None
        if not addr:
            return
        worker = EnsNamesWorker(addr, sorted(self._store.custom_ens_names))
        worker.ready.connect(self._on_names_ready)
        self._start(worker)

    def _on_names_ready(self, address: str, names: "list[EnsName]") -> None:
        host = self.host
        if host is None or host.selected_address != address:
            return                                  # view moved on
        self._cache.save(ENS_CHAIN_ID, address, names)
        self._render(names)
        self._verify(address, [n.name for n in names])

    def _render(self, names: "list[EnsName]") -> None:
        if self._panel is None:
            return
        # Keep names the chain already disowned this session filtered out, so a
        # refresh doesn't flash the dropped indexer lies back in. Pinned names
        # are exempt (intentionally watched even when unowned).
        names = [n for n in names
                 if n.source == "custom" or n.name.lower() not in self._denied]
        self._names_by_l = {n.name.lower(): n for n in names}
        self._panel.populate(build_tree(names), int(time.time()))

    # --- verification (batched, Helios) -----------------------------------

    def _verify(self, address: str, names: "list[str]") -> None:
        chain = self._mainnet()
        if chain is None or not names:
            return
        # Generous wait: ownership verification gates dropping indexer lies, so
        # on a cold restart it's worth blocking (in this worker thread) for the
        # just-prewarmed sidecar to finish syncing rather than returning
        # unverified and leaving a lie on screen until the next load.
        worker = EnsVerifyWorker(chain, address, names, wait_s=_OWNERSHIP_WAIT_S)
        worker.ready.connect(self._on_verified)
        self._start(worker)

    def _on_verified(self, address: str, states: "dict[str, OwnershipCheck]",
                     verified: bool) -> None:
        host = self.host
        if not verified or self._panel is None:
            return
        if host is not None and host.selected_address != address:
            return                                  # view moved on
        for name_l, st in states.items():
            if st.resolver:
                self._resolver_cache[name_l] = st.resolver
            if st.owned_by(address):
                self._denied.discard(name_l)     # self-heal if ownership changed
                self._owned.add(name_l)
            else:
                self._owned.discard(name_l)
            if st.wrapped:
                self._wrapped.add(name_l)
            else:
                self._wrapped.discard(name_l)
        self._denied.update(self._panel.mark_verified(states, address))
        self._refresh_writable(address)

    def _can_sign(self, address: str) -> bool:
        """True when the selected account can sign (hot or ledger, not watch-only)."""
        a = (address or "").lower()
        return any(acc.get("address", "").lower() == a
                   and acc.get("source") in ("hot", "ledger")
                   for acc in self._store.accounts)

    def _refresh_writable(self, address: str) -> None:
        # Writable = names the chain proved this address owns, AND the account
        # can actually sign. (Watch-only → read-only.)
        writable = self._owned if self._can_sign(address) else set()
        if self._panel is not None:
            self._panel.set_writable(writable)

    # --- records (lazy) ---------------------------------------------------

    def _on_records_requested(self, name: str, *, force: bool = False) -> None:
        if self._panel is None:
            return
        nl = name.lower()
        if force:
            # A write to this name just CONFIRMED. Drop the stale value so the
            # fresh head read becomes what's shown, rather than re-painting the
            # old cached one first.
            self._force_reread.add(nl)
            self._rec_cache.pop(nl, None)
            self._cache.forget_records(ENS_CHAIN_ID, name)
        else:
            # Paint cached records instantly (memory → disk), then refresh.
            cached = self._rec_cache.get(nl)
            if cached is None:
                cached = self._cache.load_records(ENS_CHAIN_ID, name)
                if cached is not None:
                    self._rec_cache[nl] = cached
            if cached is not None:
                self._panel.add_records(name, cached[0], cached[1])
        chain = self._mainnet()
        if chain is None:
            return
        if self._read_client is None:
            from ..chain import EthClient
            self._read_client = EthClient(chain)
        worker = EnsRecordsWorker(
            chain, name, client=self._read_client,
            resolver=self._resolver_cache.get(nl), catchup=force)
        worker.ready.connect(self._on_records_ready)
        self._start(worker)

    def _on_records_ready(self, name: str, rec: EnsRecords,
                          verified: bool, ok: bool) -> None:
        # A read that didn't land (transient RPC/sidecar glitch) comes back empty
        # but is NOT authoritative — keep whatever's already shown rather than
        # wipe good records. (This was the "records vanished on a glitch" bug.)
        if not ok:
            return
        nl = name.lower()
        # The worker emits the fast unverified read first, then (if a sidecar
        # could prove it) the Helios read — both at the chain head.
        prev = self._rec_cache.get(nl)
        # Don't let the late unverified phase of a refresh clobber a verified
        # result with a worse one; otherwise newest wins.
        if prev is not None and prev[1] and not verified:
            return
        # Don't let a LAGGING verified read regress the value: Helios's verified
        # head can trail the execution RPC by a few slots, so right after a
        # change its proof may still show the OLD value. The fast RPC read (which
        # the chain head already reflects) is the freshest truth — keep it, and
        # let the ✓ land later when Helios's proof finally agrees.
        if verified and prev is not None and not prev[1] and prev[0] != rec:
            return
        self._rec_cache[nl] = (rec, verified)
        self._cache.save_records(ENS_CHAIN_ID, name, rec, verified)
        if self._panel is not None:
            self._panel.add_records(name, rec, verified)
            # Reflect the head ETH address on the name row's "Resolves to" too.
            # A forced re-read (a just-confirmed write) is authoritative, so it
            # also clears a now-empty address; a normal expand only sets a
            # present one (an absent addr may just be served offchain).
            head_addr = rec.addresses.get("60")
            forced = nl in self._force_reread
            if head_addr or forced:
                self._panel.update_resolved(name, head_addr)
        if not verified:
            self._force_reread.discard(nl)   # head applied — back to normal

    # --- add custom -------------------------------------------------------

    def _on_add_custom(self) -> None:
        if self._panel is None:
            return
        text, ok = QInputDialog.getText(
            self._panel, "Add ENS name", "ENS name (e.g. vitalik.eth):")
        name = (text or "").strip().lower()
        if not ok or not name or "." not in name:
            return
        self._store.add_custom_ens_name(name)
        self._on_refresh()

    # --- writes (records + subdomains) ------------------------------------

    def _on_write_requested(self, name: str, kind: str) -> None:
        if self._panel is None:
            return
        if kind == "addr":
            self._write_addr(name)
        elif kind == "content":
            self._write_content(name)
        elif kind == "text":
            self._write_text(name)
        elif kind == "record":
            self._write_record(name)
        elif kind == "subdomain":
            self._add_subdomain(name)

    def _on_edit_record(self, name: str, label: str, value: str) -> None:
        """Re-open the matching editor for an existing record row, prefilled.
        Row labels come from ``_record_rows``: ``address`` / ``address (BTC)`` /
        ``content`` / a text key."""
        lab = label.strip()
        if lab == "address":
            self._write_addr(name, prefill=value)
        elif lab.startswith("address (") and lab.endswith(")"):
            self._write_record(name, preset="Other-chain address",
                               coin=lab[len("address ("):-1], value=value)
        elif lab == "content":
            self._write_content(name, prefill=value)
        else:
            self._write_text(name, key=lab, value=value)

    # --- per-kind editors --------------------------------------------------

    def _cur_records(self, name: str) -> "Optional[EnsRecords]":
        c = self._rec_cache.get(name.lower())
        return c[0] if c is not None else None

    def _write_addr(self, name: str, prefill: str = "") -> None:
        if self._panel is None:
            return
        cur = prefill
        if not cur:
            n = self._names_by_l.get(name.lower())
            cur = (n.resolved_address or "") if n is not None else ""
        text, ok = QInputDialog.getText(
            self._panel, "Set ETH address", f"ETH address for {name}:",
            QLineEdit.EchoMode.Normal, cur or "")
        if not ok:
            return
        from .. import ens_write
        addr = _checksum(text)
        if text.strip() and addr is None:
            self._warn("That doesn't look like a valid 0x address.")
            return
        res = self._ensure_resolver(name)
        if res is None:
            return
        to, data = ens_write.set_addr(res, name, addr or ens_write.ZERO_ADDRESS)
        self._submit_tx(name, to, data, f"Set address · {name}")

    def _write_content(self, name: str, prefill: str = "") -> None:
        if self._panel is None:
            return
        cur = prefill
        if not cur:
            rec = self._cur_records(name)
            cur = (rec.contenthash or "") if rec is not None else ""
        text, ok = QInputDialog.getText(
            self._panel, "Set content", f"IPFS / IPNS URL for {name}:",
            QLineEdit.EchoMode.Normal, cur or "")
        if not ok:
            return
        res = self._ensure_resolver(name)
        if res is None:
            return
        from .. import ens_write
        try:
            to, data = ens_write.set_contenthash(res, name, text.strip())
        except ValueError as e:
            self._warn(str(e))
            return
        self._submit_tx(name, to, data, f"Set content · {name}")

    def _write_text(self, name: str, key: str = "", value: str = "") -> None:
        if self._panel is None:
            return
        dlg = _RecordDialog(name, preset="Text record", key=key, value=value,
                            parent=self._panel)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        _, k, val = dlg.result_values()
        k = k.strip()
        if not k:
            return
        res = self._ensure_resolver(name)
        if res is None:
            return
        from .. import ens_write
        to, data = ens_write.set_text(res, name, k, val)
        self._submit_tx(name, to, data, f"Set {k} · {name}")

    def _write_record(self, name: str, *, preset: "Optional[str]" = None,
                      coin: str = "", value: str = "") -> None:
        if self._panel is None:
            return
        dlg = _RecordDialog(name, preset=preset, coin=coin, value=value,
                            parent=self._panel)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        kind, extra, val = dlg.result_values()
        res = self._ensure_resolver(name)
        if res is None:
            return
        from .. import ens_write
        if kind == "ETH address":
            addr = _checksum(val)
            if val and addr is None:
                self._warn("That doesn't look like a valid 0x address.")
                return
            to, data = ens_write.set_addr(res, name, addr or ens_write.ZERO_ADDRESS)
            label = f"Set address · {name}"
        elif kind == "Content (IPFS)":
            try:
                to, data = ens_write.set_contenthash(res, name, val)
            except ValueError as e:
                self._warn(str(e))
                return
            label = f"Set content · {name}"
        elif kind == "Text record":
            if not extra:
                return
            to, data = ens_write.set_text(res, name, extra, val)
            label = f"Set {extra} · {name}"
        else:                                    # Other-chain address
            coin_type = ens_write.COIN_TYPES.get(extra)
            if coin_type is None:
                return
            addr = _checksum(val)
            if val and addr is None:
                self._warn("That doesn't look like a valid 0x address.")
                return
            payload = ens_write.eth_addr_bytes(addr) if addr else b""
            to, data = ens_write.set_coin_addr(res, name, coin_type, payload)
            label = f"Set {extra} address · {name}"
        self._submit_tx(name, to, data, label)

    def _add_subdomain(self, name: str) -> None:
        if self._panel is None:
            return
        host = self.host
        self_addr = host.selected_address if host is not None else ""
        dlg = _SubnodeDialog(name, _checksum(self_addr or "") or "",
                             parent=self._panel)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        label, owner_in = dlg.values()
        label = label.strip().lower()
        if not label or "." in label:
            if label:
                self._warn("A subdomain label can't contain a dot.")
            return
        owner = _checksum(owner_in)
        if owner is None:
            self._warn("The owner must be a valid 0x address.")
            return
        from .. import ens_write
        to, data = ens_write.add_subnode(
            name, label, owner, wrapped=name.lower() in self._wrapped)
        self._submit_tx(name, to, data,
                        f"Add {label}.{name}", subdomain=True)

    # --- write plumbing ----------------------------------------------------

    def _resolver_for(self, name: str) -> "Optional[str]":
        res = self._resolver_cache.get(name.lower())
        if res and int(res, 16) != 0:
            return res
        return None

    def _ensure_resolver(self, name: str) -> "Optional[str]":
        """The name's resolver, or None — in which case offer to point the name
        at the default public resolver first (records can't be stored without
        one). The user then re-issues the record write after that confirms."""
        res = self._resolver_for(name)
        if res is not None:
            return res
        if self._panel is None:
            return None
        from PySide6.QtWidgets import QMessageBox
        ans = QMessageBox.question(
            self._panel, "No resolver set",
            f"{name} has no resolver, so records can't be stored yet.\n\n"
            "Point it at the default public resolver first? You can set the "
            "record again once that transaction confirms.")
        if ans == QMessageBox.StandardButton.Yes:
            from .. import ens_write
            to, data = ens_write.set_resolver(name)
            self._submit_tx(name, to, data, f"Set resolver · {name}")
        return None

    def _warn(self, text: str) -> None:
        if self._panel is None:
            return
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(self._panel, "ENS", text)

    def _submit_tx(self, name: str, to: str, data: str, label: str,
                   *, subdomain: bool = False) -> None:
        host = self.host
        chain = self._mainnet()
        addr = host.selected_address if host is not None else None
        if (host is None or chain is None or not addr
                or not hasattr(host, "request_transaction")):
            return
        from eth_utils import to_checksum_address
        from ..signing import SigningRequest
        req = SigningRequest(
            chain_id=ENS_CHAIN_ID,
            from_addr=to_checksum_address(addr),
            to_addr=to_checksum_address(to),
            value_wei=0, data=data)
        nm = name

        def _on_confirmed(_receipt: object) -> None:
            # The write mined: refresh against CONFIRMED (chain-head) state right
            # away — a subdomain add re-discovers names; a record write force-
            # re-reads so the new value shows now, marked "confirmed" until
            # finality earns it the ✓ (rather than waiting on finality to show
            # it at all).
            if subdomain:
                self._on_refresh()
            else:
                self._on_records_requested(nm, force=True)

        host.request_transaction(req, chain, label, on_confirmed=_on_confirmed)

    # --- worker lifetime --------------------------------------------------

    def _start(self, worker: QThread) -> None:
        host = self.host
        if host is not None and hasattr(host, "start_worker"):
            host.start_worker(worker)
        else:
            worker.start()
