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
from typing import Optional

from PySide6.QtCore import QEvent, QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import (
    QAction, QBrush, QColor, QDesktopServices, QIcon, QPalette,
)
from PySide6.QtWidgets import (
    QApplication, QHeaderView, QInputDialog, QMenu, QPushButton, QSizePolicy,
    QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..ens_app import (
    ENS_APP_URL, VERIFY_WAIT_S, EnsCache, EnsName, EnsNode, EnsRecords,
    OwnershipCheck, build_tree, expiry_status, fetch_name, lookup_owned_names,
    name_warning, read_records, verified_read_records, verify_names,
)
from ..plugin import Plugin

log = logging.getLogger("qeth.plugins.ens")

ENS_CHAIN_ID = 1                       # ENS lives on Ethereum mainnet
_OWNERSHIP_WAIT_S = 25.0                # cold-sidecar grace for the ownership pass
_NAME_ROLE = Qt.ItemDataRole.UserRole          # stores the EnsName on a row
_LOADED_ROLE = Qt.ItemDataRole.UserRole + 1    # records-loaded flag
_VALUE_ROLE = Qt.ItemDataRole.UserRole + 2     # copyable value on a record row
_UNSAFE_ROLE = Qt.ItemDataRole.UserRole + 3    # confusable / non-normalized name
_STATUS_ROLE = Qt.ItemDataRole.UserRole + 4    # "ok" | "warn" — the line's status
_EXPIRY_SORT_ROLE = Qt.ItemDataRole.UserRole + 5  # expiry timestamp for sorting
_TYPE_RANK_ROLE = Qt.ItemDataRole.UserRole + 6    # sort tier (see _RANK_*)

_NAME_COL = 0
_EXPIRES_COL = 1

# A name's children sort by type tier first: owned subdomains, then the
# contenthash, then the other records — alphabetically within each tier.
_RANK_SUBDOMAIN = 0
_RANK_CONTENT = 1
_RANK_RECORD = 2

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
_OWNED_TIP = (
    "Ownership proof-verified on-chain via a Helios light client — your "
    "address is the controller or registrant of this name."
)
_CONTROL_TIP = (
    "You control this subdomain (proof-verified) — its parent owner delegated "
    "it to you and can reassign it unless the relevant NameWrapper fuses are "
    "burned. A subdomain has no registrar NFT of its own."
)
_RESOLVED_TIP = "Resolved address proof-verified on-chain via a Helios light client."
_MISMATCH_TIP = (
    "⚠ The indexer's address differs from the proof-verified resolution — "
    "showing (and copying) the verified address."
)
_RECORD_TIP = "Record proof-verified on-chain via a Helios light client."
_WRAPPED_NOTE = "\nHeld via the ENS NameWrapper (ERC-1155)."

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


class _EnsItemDelegate(QStyledItemDelegate):
    """Make the ENS tree look like the wallet / token / tx lists: table-height
    rows, focus-aware selection (full highlight when focused, a quieter blend
    when not — so an inactive panel recedes), and no hover/focus-rect tint.

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
        hl = option.palette.color(QPalette.ColorRole.Highlight)
        if focused:
            return hl
        base = option.palette.color(QPalette.ColorRole.Base)
        return QColor((hl.red() * 11 + base.red() * 9) // 20,
                      (hl.green() * 11 + base.green() * 9) // 20,
                      (hl.blue() * 11 + base.blue() * 9) // 20)

    def _row_fill_rect(self, option, index):
        # On the tree column, extend the fill left over the indent so the
        # selection reaches the edge (a no-op on the other columns).
        if index.column() == 0:
            return option.rect.adjusted(-option.rect.left(), 0, 0, 0)
        return option.rect

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        opt.state &= ~QStyle.StateFlag.State_MouseOver   # no hover tint
        opt.state &= ~QStyle.StateFlag.State_HasFocus    # no dotted focus rect
        fill = self._selection_fill(option)
        if fill is not None:
            painter.fillRect(self._row_fill_rect(option, index), fill)
            tc = option.palette.color(
                QPalette.ColorRole.HighlightedText if fill.lightness() < 140
                else QPalette.ColorRole.Text)
            opt.state &= ~QStyle.StateFlag.State_Selected   # bg already painted
            for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText):
                opt.palette.setColor(role, tc)
            for role in (QPalette.ColorRole.Base, QPalette.ColorRole.AlternateBase):
                opt.palette.setColor(role, fill)
        super().paint(painter, opt, index)


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
                 resolver: "Optional[str]" = None):
        super().__init__(parent)
        self._chain = chain
        self._name = name
        self._wait_s = wait_s
        self._client = client          # warm EthClient reused across expands
        self._resolver = resolver      # cached per-name resolver (skips a round)

    def run(self) -> None:
        rec, ok = read_records(self._chain, self._name,
                               client=self._client, resolver=self._resolver)
        self.ready.emit(self._name, rec, False, ok)
        vrec, verified = verified_read_records(
            self._chain, self._name, wait_s=self._wait_s)
        if verified:                   # verified ⇒ the read landed (ok)
            self.ready.emit(self._name, vrec, True, True)


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

    # Trailing column = verification status, shown as a fixed-size icon (a text
    # ✓/⚠ glyph gets emoji presentation on some themes and changes the row
    # height). The one icon covers the whole line — name ownership and the
    # resolved value together.
    COLS = ["Name", "Expires", "Resolves to", ""]
    _STATUS_COL = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items_by_name: dict[str, QTreeWidgetItem] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.COLS))
        self.tree.setHeaderLabels(self.COLS)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
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
        item.setIcon(0, self._sub_icon if is_sub else self._domain_icon)
        item.setData(0, _NAME_ROLE, n)
        item.setData(0, _LOADED_ROLE, False)
        item.setData(0, _TYPE_RANK_ROLE, _RANK_SUBDOMAIN)
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

    def mark_verified(self, states: "dict[str, OwnershipCheck]",
                      address: str) -> "list[str]":
        """Apply the batched on-chain verification to the rows and return the
        names DROPPED as indexer lies.

        Ownership is the chain's call: a name the chain proves you control gets a
        ✓; a discovered name the chain says you DON'T own (controller and
        registrant are someone else) isn't real — the indexer over-reported it —
        so we remove it from the tree entirely. Pinned (custom) names are never
        removed: watching a name you don't own is intentional. Resolution still
        gets a ✓ / ⚠-corrected badge. Only ever called with proof-verified
        state, so acting on it is sound."""
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
            # Resolved-to: trust the proof — replace (and make copyable) the
            # verified address on a difference; flag a true mismatch.
            mismatch = False
            if st.resolved_address:
                shown = n.resolved_address if isinstance(n, EnsName) else None
                if shown and shown.lower() != st.resolved_address.lower():
                    mismatch = True
                if shown is None or mismatch:
                    if isinstance(n, EnsName):
                        n.resolved_address = st.resolved_address
                    item.setText(2, st.resolved_address)
                    item.setToolTip(2, _MISMATCH_TIP if mismatch else _RESOLVED_TIP)
            # One status icon for the whole line (ownership + resolution).
            owned = st.owned_by(address)
            if mismatch:
                self._set_status(item, "warn", _MISMATCH_TIP)
            elif owned:
                is_sub = isinstance(n, EnsName) and n.is_subdomain
                tip = _CONTROL_TIP if is_sub else _OWNED_TIP
                if st.wrapped:
                    tip += _WRAPPED_NOTE
                if st.resolved_address:
                    tip += "\n" + _RESOLVED_TIP
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
        elif value:
            menu.addAction("Copy value", lambda: _clip(str(value)))
        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(pos))


def _clip(text: str) -> None:
    QApplication.clipboard().setText(text)


def _fmt_expiry(ts: int) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


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
        self._rec_cache: "dict[str, tuple[EnsRecords, bool]]" = {}
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

    # --- plugin contract --------------------------------------------------

    def widget(self) -> QWidget:
        if self._panel is None:
            self._panel = EnsPanel()
            self._panel.records_requested.connect(self._on_records_requested)
            self._panel.add_custom_requested.connect(self._on_add_custom)
        return self._panel

    def action_widgets(self) -> "list[QWidget]":
        if self._add_btn is None:
            # Match the Tokens pane's add button exactly: a flat 28×28
            # list-add ("+") icon button, not a framed text button.
            btn = QPushButton()
            btn.setIcon(QIcon.fromTheme(
                "list-add",
                btn.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder)))
            btn.setToolTip("Pin an ENS name to always show")
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
        self._denied.update(self._panel.mark_verified(states, address))

    # --- records (lazy) ---------------------------------------------------

    def _on_records_requested(self, name: str) -> None:
        if self._panel is None:
            return
        # Paint cached records instantly (memory → disk), then refresh.
        cached = self._rec_cache.get(name.lower())
        if cached is None:
            cached = self._cache.load_records(ENS_CHAIN_ID, name)
            if cached is not None:
                self._rec_cache[name.lower()] = cached
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
            resolver=self._resolver_cache.get(name.lower()))
        worker.ready.connect(self._on_records_ready)
        self._start(worker)

    def _on_records_ready(self, name: str, rec: EnsRecords,
                          verified: bool, ok: bool) -> None:
        # A read that didn't land (transient RPC/sidecar glitch) comes back empty
        # but is NOT authoritative — keep whatever's already shown rather than
        # wipe good records. (This was the "records vanished on a glitch" bug.)
        if not ok:
            return
        # The two-phase worker emits unverified then (maybe) verified. Don't let
        # the late unverified phase of a refresh clobber a cached verified
        # result with a worse one; otherwise newest wins.
        prev = self._rec_cache.get(name.lower())
        if prev is not None and prev[1] and not verified:
            return
        self._rec_cache[name.lower()] = (rec, verified)
        self._cache.save_records(ENS_CHAIN_ID, name, rec, verified)
        if self._panel is not None:
            self._panel.add_records(name, rec, verified)

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

    # --- worker lifetime --------------------------------------------------

    def _start(self, worker: QThread) -> None:
        host = self.host
        if host is not None and hasattr(host, "start_worker"):
            host.start_worker(worker)
        else:
            worker.start()
