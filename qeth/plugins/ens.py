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
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, ClassVar
from collections.abc import Callable, Sequence

from PySide6.QtCore import QDate, QEvent, QSize, Qt, QThread, QUrl, Signal
from PySide6.QtGui import (
    QAction, QBrush, QColor, QDesktopServices, QIcon, QKeySequence, QPalette,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCalendarWidget, QComboBox, QDateEdit,
    QDialogButtonBox, QFormLayout, QHeaderView, QLabel,
    QLineEdit, QMenu, QPushButton, QScrollArea, QSizePolicy, QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem, QTableWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..ens_app import (
    ENS_APP_URL, TEXT_KEYS, VERIFY_WAIT_S, EnsCache, EnsName, EnsNode,
    EnsRecords, OwnershipCheck, build_tree, expiry_status, fetch_name,
    lookup_owned_names, name_warning, read_name_states, read_records,
    verified_read_records, verify_names,
)
from ..plugin import Plugin
from ..dialog import Dialog, address_field_min_width, prompt_text
from ..signing import SignerError, SigningRequest
from .transactions import _render_decoded, _TxComposerDialog

log = logging.getLogger("qeth.plugins.ens")

ENS_CHAIN_ID = 1                       # ENS lives on Ethereum mainnet
# Cold-sidecar grace for the ownership pass. Helios's cold-sync time is
# variable (the --load-external-fallback checkpoint fetch): usually a few
# seconds, but sometimes tens. This is a ONE-SHOT wait — if it loses the race
# the verify returns unverified and the worker gives up, so the ✓ never lands
# until a manual refresh (the "helios is ready but no badge" bug). wait_ready
# polls until synced, so a generous timeout just returns as soon as it's ready
# and only actually blocks that long on a genuinely slow/failing sync.
_OWNERSHIP_WAIT_S = 90.0
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
_OWNERSHIP_ROLE = Qt.ItemDataRole.UserRole + 7    # flags a manager/owner row

_NAME_COL = 0
_EXPIRES_COL = 1

# Rows sort by type tier first (alphabetically within each tier): the name's
# manager/owner (its on-chain roles), then real domains (2LDs), then subdomains
# — including orphan subdomains that surface at the top level — then a name's
# contenthash, then its other records.
_RANK_OWNERSHIP = -1
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

    def __lt__(self, other: QTreeWidgetItem) -> bool:
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
_MANAGER_TIP = ("Manager (registry controller) — the role that sets the "
                "resolver, records and subdomains")
_OWNER_TIP = ("Owner (registrant) — holds the name's NFT; can transfer it and "
              "reclaim the manager role")
_PENDING_TIP = ("You hold this name's NFT (read on-chain) — the verified head "
                "is still catching up, so cryptographic proof is pending")
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


def _icon(names: str | Sequence[str],
          fallback: QStyle.StandardPixmap) -> QIcon:
    """A themed icon, taken from the first of ``names`` the active icon theme
    provides; falls back to a built-in Qt standard icon so something always
    renders. Passing several names guards against a theme that lacks the primary
    name silently collapsing distinct actions onto the same generic fallback —
    e.g. Adwaita has no ``configure`` or ``office-calendar``, so "Set manager"
    and "Extend" would otherwise both render as a plain document."""
    for name in ((names,) if isinstance(names, str) else names):
        ic = QIcon.fromTheme(name)
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

    def _selection_fill(self, option) -> QColor | None:
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


class _StatusGlyphDelegate(_EnsItemDelegate):
    """Status column: the focus-aware selection background (from the base) plus
    the status GLYPH (✓ / ⚠︎ / ⏳︎) — a font glyph, not a themed icon, so it's
    identical across theme/size/DPI (see ``_set_status``). Painted flush against
    the right edge (the default delegate left-aligns text), so the resolved-
    address column keeps the rest, and coloured to match selection."""

    def paint(self, painter, option, index) -> None:
        fill = self._selection_fill(option)
        if fill is not None:
            painter.fillRect(option.rect, fill)
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if not text:
            return
        painter.save()
        colour = (option.palette.color(
                      QPalette.ColorRole.HighlightedText if fill.lightness() < 140
                      else QPalette.ColorRole.Text)
                  if fill is not None
                  else option.palette.color(QPalette.ColorRole.Text))
        painter.setPen(colour)
        painter.setFont(option.font)
        painter.drawText(
            option.rect.adjusted(0, 0, -4, 0),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            str(text))
        painter.restore()


def _record_rows(rec: EnsRecords) -> list[tuple[str, str, str]]:
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

    # Generation the worker was spawned in (set by EnsPlugin._on_refresh);
    # the landing slot drops a result from a superseded generation.
    _epoch: int = 0

    def __init__(self, address: str, custom_names: list[str], parent=None):
        super().__init__(parent)
        self._address = address
        self._custom = list(custom_names)

    def run(self) -> None:
        from ..ens_app import (
            _is_eth_2ld, _labelhash, lookup_registrant_names,
        )
        names = lookup_owned_names(ENS_CHAIN_ID, self._address)
        have = {n.name.lower() for n in names}
        # Names held as the registrant but managed elsewhere — BENS's
        # controller-keyed sweep misses these (e.g. crv.eth). Skip the .eth
        # labelhashes BENS already returned so we only resolve the gap.
        skip = {int.from_bytes(_labelhash(n.name.split(".")[0]), "big")
                for n in names if _is_eth_2ld(n.name)}
        for n in lookup_registrant_names(ENS_CHAIN_ID, self._address,
                                         skip_labelhashes=skip):
            if n.name.lower() not in have:
                have.add(n.name.lower())
                names.append(n)
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

    # (name, rec, block, verified, ok, forced) — ``forced`` carries THIS
    # worker's post-write-ness so the landing slot doesn't read it from a
    # plugin-wide flag a concurrent non-forced worker could clear (satellite 4).
    ready = Signal(str, object, object, bool, bool, bool)

    def __init__(self, chain, name: str, parent=None,
                 *, wait_s: float = VERIFY_WAIT_S, client=None,
                 resolver: str | None = None, catchup: bool = False):
        super().__init__(parent)
        self._chain = chain
        self._name = name
        self._wait_s = wait_s
        # None in production → read_records makes its own EthClient on THIS
        # worker's thread (issue #6: no client shared across threads). A test
        # may inject one; a single worker runs alone on its thread, so that's
        # still single-threaded use.
        self._client = client
        self._resolver = resolver      # cached per-name resolver (skips a round)
        # Forced (post-write) re-read: wait for Helios's verified head to catch
        # up to the value the fast read already saw, so the ✓ lands on the NEW
        # value rather than the sidecar's momentarily-stale older one.
        self._catchup = catchup

    def run(self) -> None:
        # _catchup is set iff this worker was spawned for a post-write re-read
        # (catchup=force), so it doubles as this worker's "forced" flag.
        forced = self._catchup
        rec, ok, fast_block = read_records(
            self._chain, self._name,
            client=self._client, resolver=self._resolver)
        self.ready.emit(self._name, rec, fast_block, False, ok, forced)
        tries = _VERIFY_CATCHUP_TRIES if self._catchup else 1
        for attempt in range(tries):
            vrec, verified, vblock = verified_read_records(
                self._chain, self._name,
                wait_s=self._wait_s if attempt == 0 else 0.0)
            if not verified:           # no sidecar / can't prove → stop trying
                return
            # Emit once the verified read reflects the SAME-OR-NEWER chain state
            # as the fast read (its block ≥ the fast read's), so the ✓ lands on
            # the value the head already shows rather than a lagging proof of the
            # old one — or on the last attempt (the block-ordered reducer drops
            # it anyway if it's still behind). A single non-catchup pass emits
            # immediately; the reducer orders it.
            caught_up = (fast_block is None or vblock is None
                         or vblock >= fast_block)
            if not ok or caught_up or attempt == tries - 1:
                self.ready.emit(self._name, vrec, vblock, True, True, forced)
                return
            self.msleep(int(_VERIFY_CATCHUP_DELAY_S * 1000))


class EnsVerifyWorker(QThread):
    """Check the displayed names' ownership in two phases, like the records
    worker: first a fast UNVERIFIED read at the execution head (fresh — reflects
    a just-confirmed tx at once) for an immediate owner/manager update, then the
    Helios-proven read that earns the ✓ and decides drops. Emits
    ``ready(address, states, verified)`` once or twice.

    On a forced post-write refresh (``catchup``), the verified pass waits for
    Helios to agree with the fresh read before emitting, so a lagging proof
    can't overwrite the change the user just made."""

    ready = Signal(str, object, bool)        # (address, states, verified)

    # Generation the worker was spawned in (set by EnsPlugin._verify); the
    # landing slot drops a result from a superseded generation.
    _epoch: int = 0

    def __init__(self, chain, address: str, names: list[str], parent=None,
                 *, wait_s: float = VERIFY_WAIT_S, catchup: bool = False):
        super().__init__(parent)
        self._chain = chain
        self._address = address
        self._names = list(names)
        self._wait_s = wait_s
        self._catchup = catchup

    def run(self) -> None:
        fast, fast_block = read_name_states(self._chain, self._names)
        if fast:
            self.ready.emit(self._address, fast, False)
        tries = _VERIFY_CATCHUP_TRIES if self._catchup else 1
        for attempt in range(tries):
            states, verified, vblock = verify_names(
                self._chain, self._names,
                wait_s=self._wait_s if attempt == 0 else 0.0)
            if not verified:           # no sidecar / can't prove → stop trying
                return
            # When to accept the verified proof. The block-ordering below (verified
            # block ≥ the fast read's) exists ONLY to guard a just-made change on a
            # post-write CATCHUP — a lagging proof must not regress it. On a NORMAL
            # load there's no pending change, and helios's verified head trails the
            # fast read's execution head by slots, so requiring vblock ≥ fast_block
            # there dropped the ✓ on nearly every load (the proof was computed but
            # never emitted → "helios ready but no badge"). A lagging proof of a
            # STABLE owner is still the current owner, so just emit it.
            if not self._catchup:
                caught_up = True
            elif not fast:
                # Catchup with no fresh head to order against: can't confirm the
                # proof reached the change → don't emit a possibly-stale owner; the
                # ✓ lands on a later refresh once a fast read does (satellite 5).
                caught_up = False
            else:
                caught_up = (fast_block is None or vblock is None
                             or vblock >= fast_block)
            if caught_up:
                self.ready.emit(self._address, states, True)
                return
            if attempt < tries - 1:
                self.msleep(int(_VERIFY_CATCHUP_DELAY_S * 1000))
        # Helios never caught up to the fresh read within budget. DON'T emit a
        # proof that still shows the pre-change owner — it would regress the fast
        # read (the freshest truth), revert a just-transferred name's owner and
        # wrongly keep offering Set-manager. The ✓ lands on a later refresh.


def _eth_usd_rate(chain) -> Decimal | None:
    """Current USD price of 1 ETH (DefiLlama), to value a renewal in dollars.
    None if the lookup doesn't land — the dialog then shows ETH only."""
    try:
        from ..prices import DefiLlamaPrices
        res = DefiLlamaPrices().fetch(chain, [], include_native=True)
    except Exception:
        log.debug("ETH/USD rate fetch failed", exc_info=True)
        return None
    p = res.get("")        # "" is the native-asset key
    return p.price_usd if p is not None else None


class EnsRenewPriceWorker(QThread):
    """Read the on-chain renewal price (rentPrice oracle) — and, optionally, the
    ETH/USD rate — off the Qt thread, so the renew dialog can quote a cost
    without blocking. Emits ``ready(price_wei, usd_per_eth)``; either is None if
    that read didn't land. The renewal price is linear in duration, so the
    caller quotes one year here and scales it locally per the chosen term."""

    ready = Signal(object, object)        # (price_wei | None, usd_per_eth | None)

    def __init__(self, chain, label: str, duration_s: int,
                 *, with_usd: bool = False, parent=None):
        super().__init__(parent)
        self._chain = chain
        self._label = label
        self._duration = duration_s
        self._with_usd = with_usd

    def run(self) -> None:
        from ..ens_app import rent_price
        price = rent_price(self._chain, self._label, self._duration)
        usd = _eth_usd_rate(self._chain) if self._with_usd else None
        self.ready.emit(price, usd)


class EnsPanel(QWidget):
    """The tree widget: names → owned subdomains → records."""

    add_custom_requested = Signal()
    records_requested = Signal(str)    # name → load its records (lazy)
    write_requested = Signal(str, str)  # (name, kind) — addr|content|text|record|subdomain|remove
    edit_record_requested = Signal(str, str, str)  # (name, label, value)
    remove_record_requested = Signal(str, str, str)  # (name, label, value)
    copied = Signal(str)               # text just put on the clipboard

    # Trailing column = verification status, shown as a fixed-size icon (a text
    # ✓/⚠ glyph gets emoji presentation on some themes and changes the row
    # height). The one icon covers the whole line — name ownership and the
    # resolved value together.
    COLS: ClassVar[list[str]] = ["Name", "Expires", "Resolves to", ""]
    _STATUS_COL = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items_by_name: dict[str, QTreeWidgetItem] = {}
        self._writable: set[str] = set()   # names the user manages (can set records)
        self._transferable: set[str] = set()   # names the user can transfer (NFT owner)
        self._reclaimable: set[str] = set()    # owner can reclaim manager (unwrapped)
        self._subnode_manageable: set[str] = set()  # subdomains you own the parent of
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
            self._STATUS_COL, _StatusGlyphDelegate(self.tree))
        self.tree.installEventFilter(self)
        # Click headers to sort by name / expiry (either direction); the
        # Expires column sorts by real timestamp via _SortItem. Default: name A→Z.
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(_NAME_COL, Qt.SortOrder.AscendingOrder)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_menu)
        # Ctrl+C copies the selected row's natural value — the name for a name
        # row, the address/value for a record or owner/manager row — scoped to
        # the tree (same idiom as the Tokens table's copy shortcut).
        copy_act = QAction(self.tree)
        copy_act.setShortcut(QKeySequence.StandardKey.Copy)
        copy_act.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut)
        copy_act.triggered.connect(self._copy_current)
        self.tree.addAction(copy_act)
        # Enter / Return on a value row launches its editor (a record's value,
        # or the Set-manager / Transfer dialog for the owner/manager rows).
        edit_act = QAction(self.tree)
        edit_act.setShortcuts([QKeySequence(Qt.Key.Key_Return),
                               QKeySequence(Qt.Key.Key_Enter)])
        edit_act.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut)
        edit_act.triggered.connect(self._edit_current)
        self.tree.addAction(edit_act)
        # Delete removes the selected removable row — a resolver record (you
        # manage the name) or a subdomain (you own its parent). It opens the
        # rich composer to review + sign, exactly like the Remove buttons, so an
        # accidental keypress never deletes anything on its own.
        del_act = QAction(self.tree)
        del_act.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        del_act.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut)
        del_act.triggered.connect(self._remove_current)
        self.tree.addAction(del_act)
        layout.addWidget(self.tree)

        self._domain_icon = _icon("emblem-web", QStyle.StandardPixmap.SP_DriveNetIcon)
        self._sub_icon = _icon("folder", QStyle.StandardPixmap.SP_DirIcon)
        # Status column (ok / warn / pending) is drawn with font GLYPHS, not
        # themed icons — see _set_status for why.
        self._rec_icons = {
            # A chain link for "points to an address". The SP_* last-resort (bare
            # icon-less themes only) is a plain document — NOT SP_FileLinkIcon
            # (tiny corner shortcut-arrow badge) nor SP_ArrowForward (a filled
            # "play" triangle); both read oddly standalone. (No emblem-* names
            # either — those are corner-badge overlays too.)
            "address": _icon(
                ("insert-link", "edit-link", "gtk-jump-to", "mail-attachment"),
                QStyle.StandardPixmap.SP_FileIcon),
            "content": _icon(
                ("folder-remote", "folder-publicshare", "network-server"),
                QStyle.StandardPixmap.SP_DirLinkIcon),
            "text": _icon("text-x-generic", QStyle.StandardPixmap.SP_FileIcon),
        }
        # The two on-chain role rows. Distinct icons so manager (configures the
        # name) and owner (holds its NFT) read apart at a glance: a gear for the
        # manager, a certificate/seal for the owner.
        self._manager_icon = _icon(
            ("configure", "preferences-system", "applications-system",
             "system-run", "preferences-other"),
            QStyle.StandardPixmap.SP_FileDialogDetailedView)
        self._owner_icon = _icon("application-certificate",
                                 QStyle.StandardPixmap.SP_FileIcon)
        # name_lower → the last verified OwnershipCheck, so the manager/owner
        # rows can be (re-)rendered whenever a name's records reload.
        self._ownership: dict[str, OwnershipCheck] = {}
        self._ownership_verified: set[str] = set()   # names with a proven ✓
        # Last-rendered signatures, so an identical re-emit is a no-op (no child
        # churn that could race with the user's expand/collapse).
        self._ownership_sig: dict[str, object] = {}
        self._records_sig: dict[str, object] = {}
        # Signature of the last populated tree (names + expiry status + resolved
        # address + structure). An identical discovery landing then skips the
        # clear+rebuild entirely, so an async re-emit — which fires right after
        # the user's own write, while they're looking at the name they edited —
        # can't collapse their expansions or drop their selection (finding 3g).
        self._populate_sig: object = None
        # Context-menu action icons. The write actions reuse the row icons that
        # already stand for those things (gear = manager, link = address,
        # folder-remote = content, text glyph = text record, folder = subdomain)
        # so the menu entry and the row it edits read as the same concept.
        _sp = QStyle.StandardPixmap
        copy_ic = _icon("edit-copy", _sp.SP_FileIcon)
        self._act_icons = {
            "open": _icon("internet-web-browser", _sp.SP_DialogOpenButton),
            "copy": copy_ic,
            "edit": _icon("document-edit", _sp.SP_FileIcon),
            "renew": _icon(
                ("office-calendar", "x-office-calendar", "appointment-new",
                 "view-calendar", "view-refresh"),
                _sp.SP_BrowserReload),
            "transfer": _icon("go-next", _sp.SP_ArrowForward),
            "manager": self._manager_icon,
            "addr": self._rec_icons["address"],
            "content": self._rec_icons["content"],
            "text": self._rec_icons["text"],
            "record": _icon("document-properties", _sp.SP_FileIcon),
            "subdomain": _icon("folder-new", _sp.SP_FileDialogNewFolder),
            # A trash/delete glyph for the destructive remove actions — distinct
            # from every other action icon so "remove" never collapses onto a
            # neighbour on a sparse theme.
            "remove": _icon(("edit-delete", "user-trash", "list-remove",
                             "trash-empty"), _sp.SP_TrashIcon),
        }
        # Selection-driven action buttons. They're created here but MOUNTED by
        # the slot's shared bottom row (via the plugin's action_widgets), so the
        # ENS actions sit on one line with the chain selector — structurally and
        # stylistically identical to the Tokens / Transactions panels. The
        # current selection's target is cached for the button slots.
        self._cur_name: EnsName | None = None
        self._cur_value: str | None = None
        self._build_action_buttons()
        self.tree.itemSelectionChanged.connect(self._update_action_bar)
        self._update_action_bar()

    # --- selection-driven action buttons ----------------------------------

    def _build_action_buttons(self) -> None:
        """Build the buttons the slot mounts on its shared bottom row. Styled to
        match the other panels: a framed labelled button for the primary actions
        (like Send / Add account) and flat 28×28 icon buttons for the icon-only
        utilities (like the token +/copy/star row)."""
        ic = self._act_icons

        # Parented to the panel so toggling their visibility never spawns a
        # stray top-level window before the slot reparents them onto its row
        # (the panel page is hidden until then; the slot moves them across
        # before it's shown).
        def named(icon: QIcon, text: str, tip: str) -> QPushButton:
            b = QPushButton(text, self)
            b.setIcon(icon)
            b.setIconSize(QSize(16, 16))
            b.setToolTip(tip)
            return b

        def util(icon: QIcon, tip: str) -> QPushButton:
            b = QPushButton(self)
            b.setIcon(icon)
            b.setToolTip(tip)
            b.setFlat(True)
            b.setMaximumSize(28, 28)
            b.setIconSize(QSize(16, 16))
            b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            return b

        # &-mnemonics like the other panels' labelled buttons (Send / Add
        # Account). T and E are free in the main window (Wallets uses A/C/R/S,
        # Tokens uses S; the other &E/&T live in modal dialogs).
        self._b_transfer = named(ic["transfer"], "&Transfer", "Transfer name")
        self._b_renew = named(ic["renew"], "&Extend", "Extend registration")
        self._b_manager = util(ic["manager"], "Set manager")
        self._b_addr = util(ic["addr"], "Set ETH address")
        self._b_content = util(ic["content"], "Set content (IPFS)")
        # The general record editor + subdomain creator — also on the
        # right-click menu; buttoned here so the two surfaces agree (both
        # enabled only when this account manages the name).
        self._b_record = util(ic["record"], "Add or change a record")
        self._b_subdomain = util(ic["subdomain"], "Add subdomain")
        self._b_copyname = util(ic["copy"], "Copy name")
        # Remove a subdomain (you own its parent) — a destructive utility, so
        # icon-only with the trash glyph, enabled only for a removable subdomain.
        self._b_remove = util(ic["remove"], "Remove subdomain")
        # Edit is the labelled primary in record mode (first, framed); Copy and
        # Remove are the icon-only utilities beside it.
        self._b_recedit = named(ic["edit"], "&Edit", "Edit value")
        self._b_reccopy = util(ic["copy"], "Copy value")
        self._b_recremove = util(ic["remove"], "Remove record")
        self._b_add = util(
            _icon("list-add", QStyle.StandardPixmap.SP_FileDialogNewFolder),
            "Add a name")

        self._b_transfer.clicked.connect(lambda: self._emit_name("transfer"))
        self._b_renew.clicked.connect(lambda: self._emit_name("renew"))
        self._b_manager.clicked.connect(lambda: self._emit_name("manager"))
        self._b_addr.clicked.connect(lambda: self._emit_name("addr"))
        self._b_content.clicked.connect(lambda: self._emit_name("content"))
        self._b_record.clicked.connect(lambda: self._emit_name("record"))
        self._b_subdomain.clicked.connect(lambda: self._emit_name("subdomain"))
        self._b_copyname.clicked.connect(self._copy_name)
        self._b_reccopy.clicked.connect(self._copy_value)
        self._b_recedit.clicked.connect(self._edit_current)
        self._b_remove.clicked.connect(self._remove_current)
        self._b_recremove.clicked.connect(self._remove_current)
        self._b_add.clicked.connect(lambda: self.add_custom_requested.emit())

        self._name_btns = [self._b_transfer, self._b_renew, self._b_manager,
                           self._b_addr, self._b_content, self._b_record,
                           self._b_subdomain, self._b_copyname, self._b_remove]
        self._rec_btns = [self._b_recedit, self._b_reccopy, self._b_recremove]

    def action_buttons(self) -> list[QWidget]:
        """The full button list for the slot's bottom row (selection decides
        which are shown/enabled). The "add a name" button is always present."""
        self._update_action_bar()
        return [*self._name_btns, *self._rec_btns, self._b_add]

    def _update_action_bar(self) -> None:
        # Skip while the buttons are detached from a slot row (between tab
        # switches): toggling setVisible on a parentless widget would pop a
        # stray top-level window. on_activated re-runs this once they're
        # remounted. (At construction they're parented to the panel, so the
        # initial pass still sets sensible state.)
        if self._b_add.parent() is None:
            return
        sel = self.tree.selectedItems()
        item = sel[0] if sel else None
        n = item.data(0, _NAME_ROLE) if item is not None else None
        if isinstance(n, EnsName):
            self._set_name_mode(n)
        elif item is not None and item.data(0, _VALUE_ROLE) is not None:
            self._set_record_mode(item)
        else:
            self._set_name_mode(None)

    def _set_name_mode(self, n: EnsName | None) -> None:
        for b in self._rec_btns:
            b.setVisible(False)
        for b in self._name_btns:
            b.setVisible(True)
        self._cur_name = n
        if n is None:
            for b in self._name_btns:
                b.setEnabled(False)
            return
        nl = n.name.lower()
        is_2ld = not n.is_subdomain and n.name.endswith(".eth")
        manages = nl in self._writable
        owns = nl in self._transferable or nl in self._reclaimable
        self._b_copyname.setEnabled(True)
        self._b_transfer.setEnabled(nl in self._transferable)
        self._b_renew.setEnabled(is_2ld and (manages or owns))
        self._b_manager.setEnabled(
            nl in self._reclaimable or nl in self._subnode_manageable)
        self._b_addr.setEnabled(manages)
        self._b_content.setEnabled(manages)
        self._b_record.setEnabled(manages)
        self._b_subdomain.setEnabled(manages)
        # Remove a subdomain: only for a subdomain whose parent you own.
        self._b_remove.setEnabled(
            n.is_subdomain and nl in self._subnode_manageable)

    def _set_record_mode(self, item: QTreeWidgetItem) -> None:
        for b in self._name_btns:
            b.setVisible(False)
        for b in self._rec_btns:
            b.setVisible(True)
        val = item.data(0, _VALUE_ROLE)
        self._cur_value = None if val is None else str(val)
        self._b_reccopy.setEnabled(self._cur_value is not None)
        # Edit launches the right editor for the row: a record's value editor,
        # or — for the manager/owner role rows — the Set-manager / Transfer
        # dialog. Enabled only when that action is actually available.
        self._b_recedit.setEnabled(self._edit_target(item) is not None)
        # Remove clears a resolver record (never the owner/manager role rows).
        self._b_recremove.setEnabled(self._remove_target(item) is not None)

    def _edit_target(self, item: QTreeWidgetItem):
        """What "Edit" does for ``item``: ``("record", name, label, value)`` for
        a resolver-record row, ``("write", name, kind)`` for the manager/owner
        role rows (kind ``manager``→reclaim, ``transfer``), or None when the row
        isn't editable (a name row, or one this account can't change)."""
        if item.data(0, _VALUE_ROLE) is None:
            return None
        parent = item.parent()
        pn = parent.data(0, _NAME_ROLE) if parent is not None else None
        if not isinstance(pn, EnsName):
            return None
        nl = pn.name.lower()
        if item.data(0, _OWNERSHIP_ROLE):
            label = item.text(0)
            if label == "manager" and (nl in self._reclaimable
                                       or nl in self._subnode_manageable):
                return ("write", pn.name, "manager")
            if label == "owner" and nl in self._transferable:
                return ("write", pn.name, "transfer")
            return None
        if nl in self._writable:
            return ("record", pn.name, item.text(0),
                    str(item.data(0, _VALUE_ROLE)))
        return None

    def _edit_item(self, item: QTreeWidgetItem) -> None:
        target = self._edit_target(item)
        if target is None:
            return
        if target[0] == "record":
            _kind, name, label, value = target
            self.edit_record_requested.emit(name, label, value)
        else:
            _kind, name, write_kind = target
            self.write_requested.emit(name, write_kind)

    def _edit_current(self) -> None:
        """Enter / the Edit button — edit the selected value row."""
        sel = self.tree.selectedItems()
        if sel:
            self._edit_item(sel[0])

    def _remove_target(self, item: QTreeWidgetItem):
        """What "Remove" (DEL / the trash button / the menu) does for ``item``:
        ``("record", name, label, value)`` for a resolver-record row on a name
        this account manages, ``("subdomain", name)`` for a subdomain whose
        parent this account owns, or None when the row can't be removed (a 2LD,
        an owner/manager role row, a record on a name you don't manage)."""
        n = item.data(0, _NAME_ROLE)
        if isinstance(n, EnsName):
            nl = n.name.lower()
            if n.is_subdomain and nl in self._subnode_manageable:
                return ("subdomain", n.name)
            return None
        if item.data(0, _VALUE_ROLE) is None or item.data(0, _OWNERSHIP_ROLE):
            return None                  # a role row (owner/manager) isn't a record
        parent = item.parent()
        pn = parent.data(0, _NAME_ROLE) if parent is not None else None
        if not isinstance(pn, EnsName) or pn.name.lower() not in self._writable:
            return None
        return ("record", pn.name, item.text(0),
                str(item.data(0, _VALUE_ROLE)))

    def _remove_item_action(self, item: QTreeWidgetItem) -> None:
        target = self._remove_target(item)
        if target is None:
            return
        if target[0] == "record":
            _kind, name, label, value = target
            self.remove_record_requested.emit(name, label, value)
        else:
            _kind, name = target
            self.write_requested.emit(name, "remove")

    def _remove_current(self) -> None:
        """DEL / a Remove button — remove the selected record or subdomain."""
        sel = self.tree.selectedItems()
        if sel:
            self._remove_item_action(sel[0])

    def _emit_name(self, kind: str) -> None:
        if self._cur_name is not None:
            self.write_requested.emit(self._cur_name.name, kind)

    def _copy(self, text: str | None) -> None:
        """Put ``text`` on the clipboard and announce it (→ status line)."""
        if text:
            _clip(text)
            self.copied.emit(text)

    def _copy_current(self) -> None:
        """Ctrl+C — copy the selected row's natural value: the ENS name for a
        name row, the address/value for a record or owner/manager row."""
        sel = self.tree.selectedItems()
        if not sel:
            return
        item = sel[0]
        n = item.data(0, _NAME_ROLE)
        if isinstance(n, EnsName):
            self._copy(n.name)
            return
        val = item.data(0, _VALUE_ROLE)
        if val is not None:
            self._copy(str(val))

    def _copy_name(self) -> None:
        if self._cur_name is not None:
            self._copy(self._cur_name.name)

    def _copy_value(self) -> None:
        self._copy(self._cur_value)


    # --- rendering --------------------------------------------------------

    def populate(self, roots: list[EnsNode], now_ts: int) -> None:
        sig = tuple(self._node_sig(r, now_ts) for r in roots)
        if sig == self._populate_sig and self._items_by_name:
            return   # identical tree — don't rebuild (would lose fold/selection)
        # Capture the user's fold + selection so a genuine rebuild restores them
        # (a rebuilt tree starts collapsed with nothing selected).
        expanded = {nl for nl, it in self._items_by_name.items()
                    if it.isExpanded()}
        cur = self.tree.currentItem()
        cur_name = cur.data(0, _NAME_ROLE) if cur is not None else None
        selected = (cur_name.name.lower()
                    if isinstance(cur_name, EnsName) else None)
        # Bulk-insert with sorting off (else the tree re-sorts on every add),
        # then re-enable — which applies the user's current sort column/order.
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        self._items_by_name.clear()
        self._ownership.clear()
        self._ownership_verified.clear()
        self._ownership_sig.clear()
        self._records_sig.clear()
        for node in roots:
            self.tree.addTopLevelItem(self._build(node, now_ts, is_sub=False))
        self.tree.setSortingEnabled(True)
        for nl in expanded:
            it = self._items_by_name.get(nl)
            if it is not None:
                it.setExpanded(True)   # re-fires the lazy records load
        if selected is not None:
            it = self._items_by_name.get(selected)
            if it is not None:
                self.tree.setCurrentItem(it)
        self._populate_sig = sig

    @staticmethod
    def _node_sig(node: EnsNode, now_ts: int) -> tuple:
        n = node.name
        return (n.name.lower(), expiry_status(n.expiry_ts, now_ts),
                (n.resolved_address or "").lower(), n.source, n.is_subdomain,
                tuple(EnsPanel._node_sig(c, now_ts) for c in node.children))

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
        nl = name.lower()
        # Idempotent: an identical re-emit (fast→verified with the same values,
        # a refresh that didn't change anything) must not churn the children —
        # rebuilding them mid-interaction can swallow the user's expand/collapse.
        sig = (rec, verified)
        if self._records_sig.get(nl) == sig:
            return
        self._records_sig[nl] = sig
        was_expanded = item.isExpanded()
        # Drop only the placeholder + previously-rendered record rows. The owner/
        # manager role rows (and owned subdomains) are NOT ours to touch — they
        # carry _OWNERSHIP_ROLE / _NAME_ROLE and are managed by the verify pass,
        # so a records reload no longer rebuilds them.
        for i in range(item.childCount() - 1, -1, -1):
            ch = item.child(i)
            if (ch.data(0, _NAME_ROLE) is None
                    and not ch.data(0, _OWNERSHIP_ROLE)):
                item.removeChild(ch)
        # No "no records" placeholder: a 2LD always shows its owner + manager
        # rows (and a subdomain its manager), so an expanded name is never empty.
        for icon_key, label, value in _record_rows(rec):
            ch = _SortItem([label, "", value])
            ch.setData(0, _TYPE_RANK_ROLE,
                       _RANK_CONTENT if icon_key == "content" else _RANK_RECORD)
            ch.setIcon(0, self._rec_icons.get(icon_key, self._rec_icons["text"]))
            ch.setData(0, _VALUE_ROLE, value)
            ch.setToolTip(2, value)
            if verified:                # status icon, same as the name rows
                self._set_status(ch, "ok", _RECORD_TIP)
            item.addChild(ch)
        item.setExpanded(was_expanded)   # mutating children can't toggle the fold

    def _render_ownership_rows(self, item: QTreeWidgetItem,
                               name_l: str) -> None:
        """(Re)build the manager + owner rows at the top of ``item`` from the
        stored OwnershipCheck. Idempotent + skips when nothing changed (so a
        no-op verify pass doesn't churn the children mid-interaction). Shows a
        manager row whenever the registry controller is known and, for .eth
        2LDs, an owner row for the registrant (subdomains have no registrant)."""
        st = self._ownership.get(name_l)
        proven = name_l in self._ownership_verified
        sig = None if st is None else (st.controller, st.registrant, proven)
        if self._ownership_sig.get(name_l) == sig:
            return                       # rows already reflect this state
        self._ownership_sig[name_l] = sig
        was_expanded = item.isExpanded()
        for i in range(item.childCount() - 1, -1, -1):
            ch = item.child(i)
            if ch.data(0, _OWNERSHIP_ROLE):
                item.removeChild(ch)
        if st is None:
            item.setExpanded(was_expanded)
            return
        rows = []
        if st.controller:
            rows.append((self._manager_icon, "manager", st.controller,
                         _MANAGER_TIP))
        if st.registrant:
            rows.append((self._owner_icon, "owner", st.registrant, _OWNER_TIP))
        proven = name_l in self._ownership_verified
        for icon, label, addr, tip in rows:
            shown = _checksum(addr) or addr
            ch = _SortItem([label, "", shown])
            ch.setData(0, _OWNERSHIP_ROLE, True)
            ch.setData(0, _TYPE_RANK_ROLE, _RANK_OWNERSHIP)
            ch.setData(0, _VALUE_ROLE, shown)
            ch.setIcon(0, icon)
            ch.setToolTip(0, tip)
            ch.setToolTip(2, shown)
            # ✓ only once Helios proved it; the fast read shows the fresh value
            # without a green badge.
            if proven:
                self._set_status(ch, "ok", _RECORD_TIP)
            item.addChild(ch)
        item.setExpanded(was_expanded)   # mutating children can't toggle the fold

    def update_resolved(self, name: str, address: str | None) -> None:
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

    def update_expiry(self, name: str, expiry_ts: int | None) -> None:
        """Set a name row's Expires column from the authoritative on-chain
        nameExpires (verify pass) — re-styling the status chip (active /
        expiring / grace / expired) like _build does."""
        item = self._items_by_name.get(name.lower())
        if item is None or not expiry_ts:
            return
        n = item.data(0, _NAME_ROLE)
        if isinstance(n, EnsName):
            n.expiry_ts = expiry_ts
        status = expiry_status(expiry_ts, int(time.time()))
        text, colour = _EXPIRY_STYLE.get(status, (None, None))
        item.setText(_EXPIRES_COL, text or _fmt_expiry(expiry_ts))
        item.setData(_EXPIRES_COL, _EXPIRY_SORT_ROLE, expiry_ts)
        item.setForeground(_EXPIRES_COL,
                           QBrush(colour) if colour is not None else QBrush())

    def mark_verified(self, states: dict[str, OwnershipCheck],
                      address: str, *, verified: bool = True) -> list[str]:
        """Apply an ownership read to the rows and return the names DROPPED.

        Two callers: the fast UNVERIFIED read (``verified=False``) freshens the
        owner/manager rows + resolved address the instant a tx confirms, but
        never drops a name and never paints the green ✓ (an unverified read
        can't be trusted to remove anything or to prove ownership). The
        Helios-proven read (``verified=True``) is the authority: a name it proves
        you control gets the ✓; one it proves you DON'T own (controller and
        registrant both someone else) is an indexer lie and is removed. Pinned
        (custom) names are never dropped; a freshly NFT-discovered
        (``registrant``) name whose proof is still catching up is kept and
        badged "pending" instead."""
        removed: list[str] = []
        for name_l, st in states.items():
            item = self._items_by_name.get(name_l)
            if item is None:
                continue
            n = item.data(0, _NAME_ROLE)
            src = n.source if isinstance(n, EnsName) else ""
            if st.disowned_by(address):
                if not verified:
                    continue          # unverified can't drop or badge a disown
                if src == "registrant":
                    # A FRESH on-chain NFT read surfaced it but the verified head
                    # still lags (a just-transferred name). Keep it, badge "proof
                    # catching up", don't paint the stale rows. A later pass
                    # verifies it green or a refresh drops it.
                    self._set_status(item, "pending", _PENDING_TIP)
                    item.setToolTip(0, _PENDING_TIP)
                    continue
                if src not in ("custom", "subnode"):
                    # A real indexer lie (BENS over-reported). Drop it. Pinned
                    # (custom) names and parent-owned subdomains (subnode) are
                    # exempt — showing them even when this account doesn't
                    # control them is the point (you own the parent).
                    self._remove_item(item, name_l)
                    removed.append(name_l)
                    continue
            # Authoritative on-chain expiry (nameExpires) → correct the Expires
            # column over BENS's grace-inclusive hint.
            if st.expiry:
                self.update_expiry(name_l, st.expiry)
            # Show the on-chain roles (manager / owner) for every kept name —
            # owned or merely watched — once the read definitively landed.
            if st.owner_known:
                self._ownership[name_l] = st
                if verified:
                    self._ownership_verified.add(name_l)
                else:
                    self._ownership_verified.discard(name_l)
                self._render_ownership_rows(item, name_l)
            if item.data(0, _UNSAFE_ROLE):
                continue          # keep the ⚠; never add a ✓ to a look-alike
            # Resolved-to: a head read (proven or fresh) is more current than the
            # indexer's hint — show it. No "mismatch" alarm: a difference just
            # means the indexer lagged.
            if st.resolved_address:
                if isinstance(n, EnsName):
                    n.resolved_address = st.resolved_address
                item.setText(2, st.resolved_address)
                item.setToolTip(2, _RESOLVED_TIP)
            # The line's ✓ is the verified pass's call only — the fast pass shows
            # the fresh values but leaves the badge as it was (no false green, no
            # flicker of an existing ✓).
            if verified and st.owned_by(address):
                is_sub = isinstance(n, EnsName) and n.is_subdomain
                tip = _CONTROL_TIP if is_sub else _OWNED_TIP
                if st.wrapped:
                    tip += _WRAPPED_NOTE
                self._set_status(item, "ok", tip)
                item.setToolTip(0, tip)
        return removed

    def _set_status(self, item: QTreeWidgetItem, status: str,
                    tooltip: str) -> None:
        """Set the trailing status column's glyph + tooltip for a line,
        ``status`` in ``{"ok", "warn", "pending"}``. A FONT GLYPH, not a
        QIcon.fromTheme check: a themed icon (``emblem-ok``/``dialog-ok``) ships
        different art per theme and per size — a pixelized SE98 tick vs a glossy
        square — so the verified ✓ rendered differently machine-to-machine; a
        glyph tracks the font + palette and matches the confirmed-tx column
        everywhere. Trailing U+FE0E on ⚠ / ⏳ forces the text (non-emoji) form so
        the row height stays uniform."""
        glyph = {"ok": "✓", "warn": "⚠︎", "pending": "⏳︎"}.get(status, "⚠︎")
        item.setText(self._STATUS_COL, glyph)
        item.setTextAlignment(self._STATUS_COL, Qt.AlignmentFlag.AlignCenter)
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

    def set_writable(self, names: set[str]) -> None:
        """Names (lower-case) the user can sign writes for — gates the edit
        actions in the context menu."""
        self._writable = set(names)
        self._update_action_bar()

    def set_transferable(self, names: set[str]) -> None:
        """Names (lower-case) the user owns as the registrant (NFT owner) and
        can sign for — gates the "Transfer name" action."""
        self._transferable = set(names)
        self._update_action_bar()

    def set_reclaimable(self, names: set[str]) -> None:
        """Names (lower-case) the user owns as the registrant of an *unwrapped*
        .eth 2LD — only these can reclaim the manager role (``reclaim``); gates
        the "Set manager" action."""
        self._reclaimable = set(names)
        self._update_action_bar()

    def set_subnode_manageable(self, names: set[str]) -> None:
        """Subdomains (lower-case) the user owns the *parent* of — so they can
        (re)assign the subnode's manager via ``setSubnodeOwner``. Also gates
        "Set manager" (the subdomain variant)."""
        self._subnode_manageable = set(names)
        self._update_action_bar()

    def _write_menu_groups(self, n: EnsName) -> list[list[tuple[str, str]]]:
        """The write actions available for ``n`` as ``(label, kind)`` pairs,
        split into the groups the menu separates. Registration-level actions
        (renew / transfer / set-manager on a .eth 2LD) belong to the OWNER, so a
        name you hold as registrant but don't manage — its controller delegated
        elsewhere, e.g. crv.eth — still offers Transfer / Set manager. Record
        actions need the manager (controller) role. Pure → unit-tested."""
        nm = n.name
        nl = nm.lower()
        manages = nl in self._writable                  # controller — records
        owns = nl in self._transferable or nl in self._reclaimable
        is_2ld = not n.is_subdomain and nm.endswith(".eth")
        groups: list[list[tuple[str, str]]] = []
        if is_2ld and (manages or owns):
            reg = [("Extend registration", "renew")]   # anyone can renew
            if nl in self._transferable:
                reg.append(("Transfer name", "transfer"))
            if nl in self._reclaimable:
                reg.append(("Set manager", "manager"))
            groups.append(reg)
        elif n.is_subdomain and nl in self._subnode_manageable:
            # You own the parent → you can (re)assign this subdomain's manager.
            groups.append([("Set manager", "manager")])
        if manages:
            groups.append([
                ("Set ETH address", "addr"),
                ("Set content (IPFS)", "content"),
                ("Set text record", "text"),
                ("Add / change record", "record"),
            ])
            groups.append([("Add subdomain", "subdomain")])
        # Deleting a subdomain (you own its parent) is destructive → its own
        # trailing group, separated from everything above.
        if n.is_subdomain and nl in self._subnode_manageable:
            groups.append([("Remove subdomain", "remove")])
        return groups

    def _on_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        menu = self._build_menu(item)
        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _build_menu(self, item: QTreeWidgetItem) -> QMenu:
        menu = QMenu(self.tree)
        n = item.data(0, _NAME_ROLE)
        value = item.data(0, _VALUE_ROLE)
        ic = self._act_icons
        if isinstance(n, EnsName):
            menu.addAction(ic["open"], "Open in ENS app",
                           lambda: QDesktopServices.openUrl(
                               QUrl(ENS_APP_URL.format(name=n.name))))
            menu.addAction(ic["copy"], "Copy name", lambda: self._copy(n.name))
            if n.resolved_address:
                menu.addAction(ic["copy"], "Copy resolved address",
                               lambda: self._copy(n.resolved_address))
            nm = n.name
            for group in self._write_menu_groups(n):
                menu.addSeparator()
                for label, kind in group:
                    menu.addAction(
                        ic[kind], label,
                        lambda k=kind: self.write_requested.emit(nm, k))
        elif value:
            menu.addAction(ic["copy"], "Copy value",
                           lambda: self._copy(str(value)))
            # Edit launches the row's editor: a record's value, or — on the
            # owner/manager role rows — the Transfer / Set-manager dialog.
            target = self._edit_target(item)
            if target is not None and target[0] == "record":
                menu.addAction(ic["edit"], "Edit",
                               lambda: self._edit_item(item))
            elif target is not None:
                kind = target[2]
                label = "Set manager" if kind == "manager" else "Transfer name"
                menu.addAction(ic[kind], label,
                               lambda: self._edit_item(item))
            # Remove clears a resolver record (never a role row) on a name you
            # manage — a destructive entry, so last.
            rem = self._remove_target(item)
            if rem is not None and rem[0] == "record":
                menu.addAction(ic["remove"], "Remove",
                               lambda: self._remove_item_action(item))
        return menu


def _clip(text: str) -> None:
    QApplication.clipboard().setText(text)


def _checksum(text: str) -> str | None:
    """Checksum a 0x address, or None if it isn't a valid address."""
    from eth_utils import is_address, to_checksum_address
    s = (text or "").strip()
    if not s or not is_address(s):
        return None
    return to_checksum_address(s)


def _fmt_expiry(ts: int) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _qdate_from_ts(ts: int) -> QDate:
    """The local calendar day of a unix timestamp, as a ``QDate``."""
    from datetime import datetime
    d = datetime.fromtimestamp(ts)
    return QDate(d.year, d.month, d.day)


# Record types in the general chooser → (label, needs-key, needs-coin).
_RECORD_KINDS = ["ETH address", "Content (IPFS)", "Text record",
                 "Other-chain address"]


class _RecordFields(QWidget):
    """The record-type/key/coin/value inputs as a reusable field group (no
    buttons, no dialog chrome). ``preset`` locks the type (used by the quick
    'Set text record…' / 'Set ETH address' entries); otherwise the type combo
    is shown (the general 'Add / change record…'). Emits ``changed`` on any
    edit so a host composer can re-preview / re-estimate."""

    changed = Signal()

    def __init__(self, name: str, *, preset: str | None = None,
                 key: str = "", coin: str = "", value: str = "", parent=None):
        super().__init__(parent)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self._form = form
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
        # Wide enough that a full 0x address shows without scrolling.
        self.value.setMinimumWidth(address_field_min_width(self))
        form.addRow("Key", self.key)
        form.addRow("Coin", self.coin)
        form.addRow("Value", self.value)
        self.kind.currentTextChanged.connect(self._sync)
        # Re-preview / re-estimate on any input change.
        self.kind.currentTextChanged.connect(self.changed)
        self.key.currentTextChanged.connect(self.changed)
        self.coin.currentTextChanged.connect(self.changed)
        self.value.textChanged.connect(self.changed)
        self._sync(self.kind.currentText())

    def _sync(self, kind: str) -> None:
        self._form.setRowVisible(self.key, kind == "Text record")
        self._form.setRowVisible(self.coin, kind == "Other-chain address")
        hint = {"ETH address": "0x…", "Content (IPFS)": "ipfs://…",
                "Text record": "value", "Other-chain address": "0x…"}
        self.value.setPlaceholderText(hint.get(kind, ""))

    def result_values(self) -> tuple[str, str, str]:
        """(kind, key-or-coin, value)."""
        kind = self.kind.currentText()
        extra = (self.key.currentText() if kind == "Text record"
                 else self.coin.currentText() if kind == "Other-chain address"
                 else "")
        return kind, extra, self.value.text().strip()


class _RecordDialog(Dialog):
    """Thin standalone-dialog wrapper around ``_RecordFields`` (Ok/Cancel
    chrome). The plugin's write flow now embeds the field group directly in the
    rich composer; this wrapper stays for tests + any standalone use."""

    def __init__(self, name: str, *, preset: str | None = None,
                 key: str = "", coin: str = "", value: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Record · {name}")
        outer = QVBoxLayout(self)
        self.fields = _RecordFields(name, preset=preset, key=key, coin=coin,
                                    value=value)
        outer.addWidget(self.fields)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # Backwards-compatible passthroughs to the field group.
    @property
    def kind(self) -> QComboBox:
        return self.fields.kind

    @property
    def key(self) -> QComboBox:
        return self.fields.key

    @property
    def coin(self) -> QComboBox:
        return self.fields.coin

    @property
    def value(self) -> QLineEdit:
        return self.fields.value

    def result_values(self) -> tuple[str, str, str]:
        return self.fields.result_values()


class _RenewFields(QWidget):
    """The renewal date-picker + live-cost inputs as a reusable field group.

    A date field plus an inline ``QCalendarWidget`` (kept in sync) let the user
    extend to any future day — step the year field to jump whole years, or click
    an exact month/day. The renewal duration is ``new expiry − current expiry``
    and the cost is linear in it, so a one-year quote (``set_quote``) scales to
    any term locally; the ETH/USD figure updates instantly as the date changes.
    ``selected_value_wei`` is what the caller sends (None until quoted). Emits
    ``changed`` whenever the term or the quote changes."""

    changed = Signal()

    # Renewal adds ``duration`` to the registrar's CURRENT expiry, so duration is
    # measured from there. A name in its grace period (expiry already past) is
    # measured from today instead, so the chosen date is the real new expiry.
    def __init__(self, name: str, expiry_ts: int | None, parent=None):
        super().__init__(parent)
        self._price_1y: int | None = None      # wei to renew for one year
        self._usd: Decimal | None = None       # USD per ETH
        self._syncing = False                  # guards the date↔calendar echo
        now = int(time.time())
        self._base_ts = expiry_ts if expiry_ts else now
        floor_ts = max(self._base_ts, now)     # can only extend into the future
        floor_qd = _qdate_from_ts(floor_ts)
        min_qd = floor_qd.addDays(1)           # strictly forward
        default_qd = floor_qd.addYears(1)      # sensible default: +1y
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        if expiry_ts:
            form.addRow("Current expiry", QLabel(_fmt_expiry(expiry_ts)))
        # A compact date field (type / step the year) above an always-visible
        # calendar (no fragile frameless popup) — the two stay in lock-step.
        self.date = QDateEdit()
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.date.setMinimumDate(min_qd)
        self.date.setDate(default_qd)
        form.addRow("New expiry", self.date)
        self.cal = QCalendarWidget()
        self.cal.setGridVisible(True)
        # No ISO week-number column — it sits in the same grid as the day
        # numbers and reads as just-another-column of confusing digits.
        self.cal.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.cal.setMinimumDate(min_qd)
        self.cal.setSelectedDate(default_qd)
        # Wrap the calendar in a QScrollArea purely to inherit the theme's own
        # sunken "view" frame — the inset border item-views and text fields get.
        # Drawing it ourselves (stylesheet bevel) looked synthetic, and a plain
        # QFrame's border is suppressed by the user's Kvantum style; a view
        # frame is drawn natively. widgetResizable keeps the whole calendar
        # visible (it never actually scrolls); the minimum sized to the
        # calendar's hint stops the form from shrinking it into scrollbars.
        cal_scroll = QScrollArea()
        cal_scroll.setWidget(self.cal)
        cal_scroll.setWidgetResizable(True)
        cal_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        cal_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        _hint = self.cal.sizeHint()
        cal_scroll.setMinimumSize(_hint.width() + 8, _hint.height() + 8)
        form.addRow(cal_scroll)
        self._cost_lbl = QLabel("Fetching price…")
        form.addRow("Estimated cost", self._cost_lbl)
        self.date.dateChanged.connect(self._on_date_field)
        self.cal.selectionChanged.connect(self._on_calendar)
        self._refresh()

    def _on_date_field(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        self.cal.setSelectedDate(self.date.date())
        self._syncing = False
        self._refresh()
        self.changed.emit()

    def _on_calendar(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        self.date.setDate(self.cal.selectedDate())
        self._syncing = False
        self._refresh()
        self.changed.emit()

    def set_quote(self, price_1y_wei: int | None, usd_per_eth: Decimal | None) -> None:
        """Apply the one-year price quote (+ ETH/USD rate); refreshes the cost
        and signals ``changed`` so a host composer re-estimates with the value."""
        self._price_1y = price_1y_wei
        self._usd = usd_per_eth
        self._refresh()
        self.changed.emit()

    def _refresh(self) -> None:
        self._cost_lbl.setText(self._cost_text())

    def _cost_text(self) -> str:
        price = self.selected_value_wei()
        if price is None:
            return "Fetching price…"
        from ..chain import wei_to_ether
        eth = wei_to_ether(price)
        text = f"≈ {eth:.4f} ETH"
        if self._usd is not None:
            text += f"   (~${eth * self._usd:,.2f})"
        return text

    def target_ts(self) -> int:
        """Chosen new-expiry instant (local midnight of the picked day)."""
        qd = self.date.date()
        return int(time.mktime((qd.year(), qd.month(), qd.day(),
                                0, 0, 0, 0, 0, -1)))

    def duration_seconds(self) -> int:
        """Seconds to add to the registration = new expiry − the base instant."""
        return max(0, self.target_ts() - self._base_ts)

    def selected_value_wei(self) -> int | None:
        """Total renewal price (wei) for the chosen term, or None if unpriced.
        Linear in duration: the one-year quote scaled to the picked seconds."""
        from .. import ens_write
        if self._price_1y is None:
            return None
        return self._price_1y * self.duration_seconds() // ens_write.SECONDS_PER_YEAR


class _RenewDialog(Dialog):
    """Thin standalone-dialog wrapper around ``_RenewFields`` (Ok/Cancel
    chrome). The plugin's renew flow now embeds the field group in the rich
    composer; this wrapper stays for tests + any standalone use."""

    def __init__(self, name: str, expiry_ts: int | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Extend · {name}")
        outer = QVBoxLayout(self)
        self.fields = _RenewFields(name, expiry_ts)
        outer.addWidget(self.fields)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # Backwards-compatible passthroughs to the field group.
    @property
    def date(self) -> QDateEdit:
        return self.fields.date

    @property
    def cal(self) -> QCalendarWidget:
        return self.fields.cal

    @property
    def _cost_lbl(self) -> QLabel:
        return self.fields._cost_lbl

    @property
    def _price_1y(self) -> int | None:
        return self.fields._price_1y

    def set_quote(self, price_1y_wei: int | None,
                  usd_per_eth: Decimal | None) -> None:
        self.fields.set_quote(price_1y_wei, usd_per_eth)

    def target_ts(self) -> int:
        return self.fields.target_ts()

    def duration_seconds(self) -> int:
        return self.fields.duration_seconds()

    def selected_value_wei(self) -> int | None:
        return self.fields.selected_value_wei()


class _SubnodeFields(QWidget):
    """Add-a-subdomain inputs (label + owner) as a reusable field group. The
    owner field reuses the host composer's address-book picker when given a
    ``make_address_field`` factory; otherwise a plain wide address line. Emits
    ``changed`` on any edit."""

    changed = Signal()

    def __init__(self, parent_name: str, self_addr: str, *,
                 make_address_field=None, parent=None):
        super().__init__(parent)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.label = QLineEdit()
        self.label.setPlaceholderText("label  (→ label." + parent_name + ")")
        if make_address_field is not None:
            owner_widget, self.owner = make_address_field(self_addr or "")
        else:
            self.owner = QLineEdit(self_addr or "")
            # Wide enough that a full 0x owner address shows without scrolling.
            self.owner.setMinimumWidth(address_field_min_width(self))
            owner_widget = self.owner
        form.addRow("Subdomain", self.label)
        form.addRow("Owner", owner_widget)
        self.label.textChanged.connect(self.changed)
        self.owner.textChanged.connect(self.changed)

    def values(self) -> tuple[str, str]:
        return self.label.text().strip(), self.owner.text().strip()


class _SubnodeDialog(Dialog):
    """Thin standalone-dialog wrapper around ``_SubnodeFields`` (Ok/Cancel
    chrome). The plugin's subdomain flow now embeds the field group in the rich
    composer; this wrapper stays for tests + any standalone use."""

    def __init__(self, parent_name: str, self_addr: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Add subdomain of {parent_name}")
        outer = QVBoxLayout(self)
        self.fields = _SubnodeFields(parent_name, self_addr)
        outer.addWidget(self.fields)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    @property
    def label(self) -> QLineEdit:
        return self.fields.label

    @property
    def owner(self) -> QLineEdit:
        return self.fields.owner

    def values(self) -> tuple[str, str]:
        return self.fields.values()


class _RecipientFields(QWidget):
    """A single address input (reusing the composer's address-book picker when
    available) — for the name-transfer op (label "Recipient") and the
    set-manager op (label "Manager", prefilled with the user's own address).
    Emits ``changed`` on edit."""

    changed = Signal()

    def __init__(self, *, make_address_field=None, label: str = "Recipient",
                 initial: str = "", placeholder: str = "0x… recipient address",
                 parent=None):
        super().__init__(parent)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        if make_address_field is not None:
            widget, self.recipient = make_address_field(initial)
        else:
            self.recipient = QLineEdit(initial)
            self.recipient.setMinimumWidth(address_field_min_width(self))
            widget = self.recipient
        self.recipient.setPlaceholderText(placeholder)
        form.addRow(label, widget)
        self.recipient.textChanged.connect(self.changed)

    def value(self) -> str:
        return self.recipient.text().strip()


# --- rich write composer ---------------------------------------------------

@dataclass
class _EnsOp:
    """Config for one ENS write operation, driving ``_EnsWriteComposer``.

    ``make_fields`` builds the input field group (or None for an input-less op,
    e.g. the set-resolver bootstrap). ``build`` produces ``(to, data)`` from the
    name + the field group (calling an ``ens_write`` builder; may raise
    ``ValueError`` on bad input). ``decoded`` yields a synthetic decoded-call
    tree for the live preview. ``value_wei`` is the payable amount (renew); else
    0. ``validate`` returns an error string while the inputs aren't a valid tx
    (Confirm stays disabled), or None when good. ``rediscover`` picks the
    post-confirm refresh (re-run discovery vs. force-reread records). ``payable``
    adds Value/Total summary rows."""

    title: str
    confirm_label: str
    make_fields: Callable[[_EnsWriteComposer], QWidget | None]
    build: Callable[[str, Any], tuple[str, str]]
    decoded: Callable[[str, Any], dict]
    value_wei: Callable[[Any], int] = field(default=lambda _inputs: 0)
    validate: Callable[[Any], str | None] = field(default=lambda _inputs: None)
    rediscover: bool = False
    payable: bool = False
    note: str | None = None    # a wrapped caveat shown under the inputs
    # Confirm-button icon (themed names → SP fallback). Default matches the
    # base composer; transfer overrides it to the shared right-arrow.
    confirm_icon_names: tuple[str, ...] = ()
    confirm_fallback: QStyle.StandardPixmap = QStyle.StandardPixmap.SP_ArrowUp


class _EnsWriteComposer(_TxComposerDialog):
    """The rich Send-shaped composer for an ENS write, driven by an ``_EnsOp``.

    Inherits the whole shell (Details/Events tabs, gas section, fee summary,
    sign flow, address-book picker) from ``_TxComposerDialog``; supplies the
    op's input field group + synthetic decoded preview + request construction
    via the base hooks. The op's payable value (renew) flows through
    ``_build_request`` into both the gas probe and the simulation, so an
    underpaid renew is caught before signing."""

    def __init__(self, op: _EnsOp, name: str, chain, from_addr: str, *,
                 parent=None, **shared):
        # Set before super().__init__ — the base runs our header/decoded hooks
        # while constructing.
        self._op = op
        self._name = name
        self._fields: QWidget | None = None
        self._contract_lbl: QLabel | None = None
        super().__init__(
            chain, from_addr,
            title=op.title,
            confirm_text=op.confirm_label,
            confirm_icon_names=op.confirm_icon_names,
            confirm_fallback=op.confirm_fallback,
            base_fee_text="(estimating…)",
            parent=parent,
            **shared,
        )
        # Kick an initial gas estimate + the sim if the inputs are already a
        # valid tx (input-less ops, or ops with a sensible default).
        try:
            self._kick_gas(self._build_request())
        except SignerError:
            pass

    # --- header / fields (base hooks) ------------------------------

    def _build_header_rows(self, header: QFormLayout, outer) -> None:
        header.addRow("Operating on:", self._value_label(self._name))
        self._fields = self._op.make_fields(self)
        if self._fields is not None:
            header.addRow(self._fields)
        # An op-specific caveat (e.g. transfer moves ownership but leaves the
        # manager role behind) — a wrapped, dimmed note under the inputs.
        if self._op.note:
            note = QLabel(f"ⓘ {self._op.note}")
            note.setWordWrap(True)
            pal = note.palette()
            pal.setColor(QPalette.ColorRole.WindowText,
                         pal.color(QPalette.ColorRole.PlaceholderText))
            note.setPalette(pal)
            header.addRow(note)
        # Static "Contract:" row — the on-chain target of this write (the
        # resolver / registry / controller / NameWrapper). Filled once the
        # inputs build a valid request (the target is input-independent, so it
        # shows as soon as the call can be constructed).
        self._contract_lbl = self._value_label("…", monospace=True)
        header.addRow("Contract:", self._contract_lbl)


    def _wire_inputs(self) -> None:
        fields = self._fields
        if fields is None:
            return
        changed = getattr(fields, "changed", None)
        if changed is not None:
            changed.connect(self._update_state)
            changed.connect(self.request_simulation)
            changed.connect(self._reestimate_timer.start)

    # --- request construction (base hooks) -------------------------

    def _build_request(self) -> SigningRequest:
        from eth_utils import to_checksum_address
        try:
            to, data = self._op.build(self._name, self._fields)
        except ValueError as e:
            raise SignerError(str(e)) from e
        msg = self._op.validate(self._fields)
        if msg is not None:
            raise SignerError(msg)
        return SigningRequest(
            chain_id=ENS_CHAIN_ID,
            from_addr=self._from_addr,
            to_addr=to_checksum_address(to),
            value_wei=self._op.value_wei(self._fields),
            data=data,
        )

    def _inputs_valid(self) -> bool:
        if self._op.validate(self._fields) is not None:
            return False
        try:
            self._build_request()
        except SignerError:
            return False
        return True

    def _set_inputs_enabled(self, enabled: bool) -> None:
        if self._fields is not None:
            self._fields.setEnabled(enabled)

    def _reestimate_gas(self) -> None:
        try:
            probe = self._build_request()
        except SignerError:
            return
        self._kick_gas(probe)

    # --- preview + totals (base hooks) -----------------------------

    def _refresh_decoded_view(self) -> None:
        self._update_contract_row()
        try:
            decoded = self._op.decoded(self._name, self._fields)
        except Exception:
            self.decoded_view.setPlainText("(fill in the fields above)")
            return
        _render_decoded(self.decoded_view, decoded,
                        known_addresses=self._known_addresses)

    def _update_contract_row(self) -> None:
        if self._contract_lbl is None:
            return
        from eth_utils import to_checksum_address
        try:
            to, _data = self._op.build(self._name, self._fields)
        except ValueError:
            return
        self._contract_lbl.setText(to_checksum_address(to))



class EnsPlugin(Plugin):
    name = "ENS"

    def __init__(self, store):
        super().__init__()
        self._store = store
        self._cache = EnsCache()
        self._panel: EnsPanel | None = None
        self._loaded_for: str | None = None
        # In-memory records cache (name → (records, block, verified)) layered
        # over the disk cache, so re-expanding a name is instant within a session
        # too. ``block`` is the height the shown value was read at; a read at an
        # older block (a lagging verified proof, a still-in-flight pre-write
        # worker) can never regress it, and an equal-block read only upgrades
        # unverified → verified. ``verified`` True means "proven at that block".
        self._rec_cache: dict[str, tuple[EnsRecords, int, bool]] = {}
        # (Post-write-ness now travels per-worker on the ready signal's
        # ``forced`` flag — see EnsRecordsWorker / satellite 4 — so a concurrent
        # non-forced worker can't clear it out from under a forced one. When a
        # forced read lands, its head value is authoritative enough to CLEAR the
        # name-row address, e.g. a setAddr to 0x0; a normal read only sets a
        # present one, since an absent on-chain addr may just be served offchain.)
        # Fallback worker tracking for a host with no start_worker (tests /
        # minimal hosts): keeps a running QThread referenced so it isn't GC'd
        # mid-run (Qt aborts the process then), self-evicting on finished.
        self._workers: set[QThread] = set()
        # Per-name resolver (from the ownership pass) — lets a record read skip
        # its resolver-lookup round-trip. Refreshed every load (self-heals a
        # re-pointed resolver).
        self._resolver_cache: dict[str, str] = {}
        # Names the chain proved this address does NOT own — indexer lies we
        # drop and keep filtered out of re-renders this session. Reset per
        # account; never persisted (a stale denial must never hide a real name).
        self._denied: set[str] = set()
        # Write state: the EnsName + on-chain ownership facts per name, so the
        # write actions know the resolver, wrapped flag, and parent expiry.
        self._names_by_l: dict[str, EnsName] = {}
        self._owned: set[str] = set()      # owned by the selected address
        self._wrapped: set[str] = set()    # held by the NameWrapper
        # Names the selected address is the *registrant* (NFT owner) of — the
        # only role that can transfer the name. A subset of _owned (which also
        # counts controller-only), tracked separately to gate "Transfer name…".
        self._registrant: set[str] = set()
        # Names the selected address *manages* (is the registry controller of) —
        # the role that can set records/resolver/subdomains. The other subset of
        # _owned, used to gate the record-write actions (a registrant who isn't
        # the manager can't set records until they reclaim the manager role).
        self._controller: set[str] = set()
        # Subdomains whose PARENT this account controls — can (re)assign the
        # subnode's manager via setSubnodeOwner (gates the subdomain "Set
        # manager"). Recomputed in _refresh_writable.
        self._subnode_manageable: set[str] = set()
        # One-shot: the next verify pass should wait for the verified head to
        # catch up (set after a write confirms; see _on_refresh).
        self._verify_catchup = False
        # Generation counter, bumped per refresh (a new account load or a
        # post-write rediscover). Every discovery / verify worker captures it
        # at spawn; a landing whose captured epoch is stale is dropped. This is
        # what stops a still-running load-time verify worker from repainting the
        # OLD owner (with a green ✓) over the fresh read a post-write refresh
        # already applied, and a stale discovery from overwriting the cache with
        # an older name set — races that address-equality alone can't catch
        # (both workers are for the same address).
        self._epoch = 0

    # --- plugin contract --------------------------------------------------

    def widget(self) -> QWidget:
        if self._panel is None:
            self._panel = EnsPanel()
            self._panel.records_requested.connect(self._on_records_requested)
            self._panel.add_custom_requested.connect(self._on_add_custom)
            self._panel.write_requested.connect(self._on_write_requested)
            self._panel.edit_record_requested.connect(self._on_edit_record)
            self._panel.remove_record_requested.connect(self._on_remove_record)
            self._panel.copied.connect(self._on_copied)
        return self._panel

    def _on_copied(self, text: str) -> None:
        if self.host is not None:
            self.host.status_message(f"Copied {text} to clipboard", 3000)

    def action_widgets(self) -> list[QWidget]:
        # The ENS write/copy/add buttons mount on the slot's shared bottom row
        # (one line with the chain selector), exactly like the Tokens panel —
        # the panel owns them; selection drives which are shown/enabled.
        return self._panel.action_buttons() if self._panel is not None else []

    def on_account_changed(self, address: str | None) -> None:
        # Ignore a re-broadcast of the account we're already showing: reloading
        # restarts discovery + verification, which bumps the verify generation
        # and DROPS an in-flight Helios ownership proof (the ✓ then never lands).
        # Only for a real address — None means "no selection", which must always
        # clear. A real account change still differs from _loaded_for and reloads;
        # a genuine re-verify goes through _on_refresh directly, not this path.
        if address is not None and address == self._loaded_for:
            return
        self._load(address)

    def on_activated(self) -> None:
        if self.host is not None and self._loaded_for != self.host.selected_address:
            self._load(self.host.selected_address)
        # The slot has just (re)mounted our action buttons onto its row — sync
        # their shown/enabled state to the current selection.
        if self._panel is not None:
            self._panel._update_action_bar()

    # --- loading ----------------------------------------------------------

    def _mainnet(self):
        host = self.host
        if host is not None and hasattr(host, "chain_by_id"):
            ch = host.chain_by_id(ENS_CHAIN_ID)
            if ch is not None:
                return ch
        from ..chains import DEFAULT_CHAINS
        return next((c for c in DEFAULT_CHAINS if c.chain_id == ENS_CHAIN_ID), None)

    def _load(self, address: str | None) -> None:
        if self._panel is None:
            return
        if address != self._loaded_for:
            self._denied.clear()              # denials are per-account
            self._owned.clear()
            self._wrapped.clear()
            self._registrant.clear()
            self._controller.clear()
            if self._panel is not None:
                self._panel.set_writable(set())
                self._panel.set_transferable(set())
                self._panel.set_reclaimable(set())
                self._panel.set_subnode_manageable(set())
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

    def _on_refresh(self, *, catchup: bool = False) -> None:
        host = self.host
        addr = host.selected_address if host is not None else None
        if not addr:
            return
        # A new generation: any discovery / verify worker still in flight from
        # before this refresh is now stale and must not land (see _epoch).
        self._epoch += 1
        # A post-write refresh waits for the verified head to catch up to the
        # change (so a lagging proof can't overwrite it); a normal load doesn't.
        self._verify_catchup = catchup
        worker = EnsNamesWorker(addr, sorted(self._store.custom_ens_names))
        # Tag the worker with its generation and connect a BOUND method (not a
        # lambda): a lambda isn't receiver-tracked, so a worker outliving a torn-
        # down plugin would fire into the deleted object (segfault). The slot
        # reads the generation off the emitting worker (self.sender()).
        worker._epoch = self._epoch
        worker.ready.connect(self._on_names_ready)
        self._start(worker)

    def _on_names_ready(self, address: str, names: list[EnsName],
                        *, epoch: int | None = None) -> None:
        if epoch is None:
            epoch = getattr(self.sender(), "_epoch", None)
        if epoch is not None and epoch != self._epoch:
            return                                  # superseded generation
        host = self.host
        if host is None or host.selected_address != address:
            return                                  # view moved on
        # Cache only the account's OWN discovered names (the cross-account
        # surfacing below is derived, not this account's).
        self._cache.save(ENS_CHAIN_ID, address, names)
        self._render(names)                          # augments + sets _names_by_l
        self._verify(address, list(self._names_by_l))  # verify the surfaced set too

    def _with_cross_account_subdomains(
            self, names: list[EnsName]) -> list[EnsName]:
        """Surface subdomains discovered under the wallet's OTHER accounts whose
        parent the current account owns — so a name's owner sees (and can then
        manage) its subdomains even when another of their accounts still holds
        them. Sourced from the per-account cache (a hint; verify confirms), so
        no manual pinning is needed. Tagged ``source="subnode"`` — kept even
        when the current account doesn't control them (it owns the parent)."""
        owned = {n.name.lower() for n in names}
        have = set(owned)
        cur = (self._loaded_for or "").lower()
        extra: list[EnsName] = []
        for acct in self._store.accounts:
            addr = acct.get("address", "")
            if not addr or addr.lower() == cur:
                continue
            for n in (self._cache.load(ENS_CHAIN_ID, addr) or []):
                nl, p = n.name.lower(), n.parent
                if (n.is_subdomain and p and p.lower() in owned
                        and nl not in have):
                    have.add(nl)
                    extra.append(EnsName(n.name, owner=n.owner,
                                         source="subnode"))
        return names + extra

    def _render(self, names: list[EnsName]) -> None:
        if self._panel is None:
            return
        # Keep names the chain already disowned this session filtered out, so a
        # refresh doesn't flash the dropped indexer lies back in. Pinned +
        # parent-owned subdomains are exempt (intentionally shown even unowned).
        names = [n for n in names
                 if n.source in ("custom", "subnode")
                 or n.name.lower() not in self._denied]
        names = self._with_cross_account_subdomains(names)
        self._names_by_l = {n.name.lower(): n for n in names}
        self._panel.populate(build_tree(names), int(time.time()))

    # --- verification (batched, Helios) -----------------------------------

    def _verify(self, address: str, names: list[str]) -> None:
        chain = self._mainnet()
        if chain is None or not names:
            return
        # Generous wait: ownership verification gates dropping indexer lies, so
        # on a cold restart it's worth blocking (in this worker thread) for the
        # just-prewarmed sidecar to finish syncing rather than returning
        # unverified and leaving a lie on screen until the next load.
        worker = EnsVerifyWorker(chain, address, names, wait_s=_OWNERSHIP_WAIT_S,
                                 catchup=self._verify_catchup)
        self._verify_catchup = False        # consume the one-shot flag
        # Bound method + worker tag, not a lambda (receiver-tracked → no fire
        # into a torn-down plugin; see _on_refresh).
        worker._epoch = self._epoch
        worker.ready.connect(self._on_verified)
        self._start(worker)

    def _on_verified(self, address: str, states: dict[str, OwnershipCheck],
                     verified: bool, *, epoch: int | None = None) -> None:
        # Two passes land here: the fast unverified read (verified=False) for an
        # immediate owner/manager/role-gate update, then the Helios-proven read
        # (verified=True) that earns the ✓ and decides drops. Both carry fresh
        # roles — the verified one only after it caught up (see EnsVerifyWorker).
        if epoch is None:
            epoch = getattr(self.sender(), "_epoch", None)
        if epoch is not None and epoch != self._epoch:
            # A newer refresh superseded the generation this worker was spawned
            # in — dropping it stops a lagging verified pass from repainting the
            # pre-refresh (e.g. pre-write) owner over the fresh read.
            return
        host = self.host
        if self._panel is None:
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
            # Registrant (NFT owner) — for a wrapped name this is the ERC-1155
            # holder (substituted in by the verify read). Only the registrant
            # can transfer; controller-only ownership can't.
            if st.registrant and st.registrant.lower() == address.lower():
                self._registrant.add(name_l)
            else:
                self._registrant.discard(name_l)
            # Controller (registry manager) — the role that can set records. A
            # registrant who isn't also the controller can't, until they reclaim.
            if st.controller and st.controller.lower() == address.lower():
                self._controller.add(name_l)
            else:
                self._controller.discard(name_l)
        self._denied.update(
            self._panel.mark_verified(states, address, verified=verified))
        self._refresh_writable(address)

    def _can_sign(self, address: str) -> bool:
        """True when the selected account can sign (hot or ledger, not watch-only)."""
        a = (address or "").lower()
        return any(acc.get("address", "").lower() == a
                   and acc.get("source") in ("hot", "ledger")
                   for acc in self._store.accounts)

    def _refresh_writable(self, address: str) -> None:
        # Each action gates on the role that can actually sign it (and the
        # account must be a signer at all — watch-only → read-only):
        #   • record/resolver/subdomain writes → the manager (controller);
        #   • transfer → the registrant (NFT owner);
        #   • set-manager (reclaim) → the registrant of an *unwrapped* name
        #     (a wrapped name's controller is managed through the NameWrapper).
        can_sign = self._can_sign(address)
        # Subdomains whose PARENT this account controls (and both unwrapped) —
        # the owner of a name can (re)assign its subnodes' managers via
        # registry.setSubnodeOwner, so gate "Set manager" on those too.
        self._subnode_manageable = {
            nl for nl, n in self._names_by_l.items()
            if n.is_subdomain and n.parent
            and n.parent.lower() in self._controller
            and n.parent.lower() not in self._wrapped
            and nl not in self._wrapped
        }
        subnode_mgr = self._subnode_manageable
        if self._panel is not None:
            self._panel.set_writable(self._controller if can_sign else set())
            self._panel.set_transferable(
                self._registrant if can_sign else set())
            self._panel.set_reclaimable(
                (self._registrant - self._wrapped) if can_sign else set())
            self._panel.set_subnode_manageable(
                subnode_mgr if can_sign else set())

    # --- records (lazy) ---------------------------------------------------

    def _on_records_requested(self, name: str, *, force: bool = False) -> None:
        if self._panel is None:
            return
        nl = name.lower()
        if force:
            # A write to this name just CONFIRMED. KEEP the cached anchor: the
            # fresh post-write read carries a NEWER block and wins by the
            # reducer, while a still-in-flight pre-write worker's read carries an
            # older block and is dropped. Popping it (as before) left the guards
            # with no anchor, so that stale worker landed unguarded. The
            # forced-ness travels with the worker (catchup=force → the ready
            # signal's `forced`), not a plugin-wide flag (satellite 4).
            pass
        else:
            # Paint cached records instantly (memory → disk), then refresh.
            cached = self._rec_cache.get(nl)
            if cached is None:
                cached = self._cache.load_records(ENS_CHAIN_ID, name)
                if cached is not None:
                    self._rec_cache[nl] = cached
            if cached is not None:
                self._panel.add_records(name, cached[0], cached[2])
        chain = self._mainnet()
        if chain is None:
            return
        # No shared EthClient: each worker creates its own inside run() (on its
        # own thread). EthClient owns a requests.Session + web3 provider stack +
        # mutable failover state, none thread-safe — sharing one across the
        # concurrent record workers (one per expanded name) could interleave
        # session/failover state (issue #6). A fresh client per expand costs a
        # TLS handshake; correctness wins, and expands are user-paced.
        worker = EnsRecordsWorker(
            chain, name,
            resolver=self._resolver_cache.get(nl), catchup=force)
        worker.ready.connect(self._on_records_ready)
        self._start(worker)

    def _on_records_ready(self, name: str, rec: EnsRecords,
                          block, verified: bool, ok: bool,
                          forced: bool = False) -> None:
        # A read that didn't land (transient RPC/sidecar glitch) comes back empty
        # but is NOT authoritative — keep whatever's already shown rather than
        # wipe good records. (This was the "records vanished on a glitch" bug.)
        if not ok:
            return
        nl = name.lower()
        # Block-ordered reducer — the single rule that replaces the old
        # verified-ratchet + lagging-proof + value-agreement guards:
        #   accept iff this read saw a NEWER block than what's shown, or the
        #   SAME block and it upgrades unverified → verified.
        # So a lagging verified proof (older block) can never regress a fresher
        # fast read regardless of its ✓ (3b/3c), and a live fast read of a
        # CHANGED record at a newer block replaces even a cached verified value
        # — including on a Helios-less session (3e). block-less reads are the
        # weakest (0): never override an ordered value.
        b = int(block) if block is not None else 0
        prev = self._rec_cache.get(nl)   # (rec, block, verified) | None
        if prev is not None:
            prec, pblock, pverified = prev
            if b < pblock:
                return   # older read — can't regress
            if b == pblock and not (verified and not pverified):
                return   # same block, no verified upgrade → nothing new
            # A newer read of the SAME value that happens to be unverified must
            # not flicker the ✓ off — the proof is still valid for an unchanged
            # value (the verified catch-up would just re-earn it).
            if not verified and pverified and rec == prec:
                verified = True
        self._rec_cache[nl] = (rec, b, verified)
        self._cache.save_records(ENS_CHAIN_ID, name, rec, b, verified)
        if self._panel is not None:
            self._panel.add_records(name, rec, verified)
            # Reflect the head ETH address on the name row's "Resolves to" too.
            # A forced re-read (a just-confirmed write) is authoritative, so it
            # also clears a now-empty address; a normal expand only sets a
            # present one (an absent addr may just be served offchain).
            head_addr = rec.addresses.get("60")
            if head_addr or forced:
                self._panel.update_resolved(name, head_addr)

    # --- add custom -------------------------------------------------------

    def _on_add_custom(self) -> None:
        if self._panel is None:
            return
        text, ok = prompt_text(
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
        elif kind == "renew":
            self._renew(name)
        elif kind == "transfer":
            self._transfer(name)
        elif kind == "manager":
            self._set_manager(name)
        elif kind == "remove":
            self._remove_subdomain(name)

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

    def _on_remove_record(self, name: str, label: str, value: str) -> None:
        """Clear a resolver record (DEL / the Remove button / the menu). The row
        exists, so its name already has a resolver — no resolver gate. The
        composer shows the decoded clearing call for review + signing."""
        if self._panel is None:
            return
        res = self._resolver_for(name)
        if res is None:
            return
        self._open_composer(name, self._remove_record_op(name, res, label, value))

    def _remove_subdomain(self, name: str) -> None:
        """Delete a subdomain whose parent this account owns (unwrapped), via
        registry.setSubnodeRecord(parent, label, 0, 0, 0)."""
        if self._panel is None or name.lower() not in self._subnode_manageable:
            return
        self._open_composer(
            name, self._remove_subnode_op(name),
            after_confirm=lambda: self._purge_name_from_caches(name))

    # --- per-kind editors --------------------------------------------------

    def _cur_records(self, name: str) -> EnsRecords | None:
        c = self._rec_cache.get(name.lower())
        return c[0] if c is not None else None

    def _write_addr(self, name: str, prefill: str = "") -> None:
        if self._panel is None:
            return
        res = self._ensure_resolver(name)
        if res is None:
            return
        cur = prefill
        if not cur:
            n = self._names_by_l.get(name.lower())
            cur = (n.resolved_address or "") if n is not None else ""
        self._open_composer(name, self._record_op(
            name, res, preset="ETH address", value=cur,
            confirm_label="Set address"))

    def _write_content(self, name: str, prefill: str = "") -> None:
        if self._panel is None:
            return
        res = self._ensure_resolver(name)
        if res is None:
            return
        cur = prefill
        if not cur:
            rec = self._cur_records(name)
            cur = (rec.contenthash or "") if rec is not None else ""
        self._open_composer(name, self._record_op(
            name, res, preset="Content (IPFS)", value=cur,
            confirm_label="Set content"))

    def _write_text(self, name: str, key: str = "", value: str = "") -> None:
        if self._panel is None:
            return
        res = self._ensure_resolver(name)
        if res is None:
            return
        self._open_composer(name, self._record_op(
            name, res, preset="Text record", key=key, value=value,
            confirm_label="Set text record"))

    def _write_record(self, name: str, *, preset: str | None = None,
                      coin: str = "", value: str = "") -> None:
        if self._panel is None:
            return
        res = self._ensure_resolver(name)
        if res is None:
            return
        self._open_composer(name, self._record_op(
            name, res, preset=preset, coin=coin, value=value,
            confirm_label="Save record"))

    def _add_subdomain(self, name: str) -> None:
        if self._panel is None:
            return
        host = self.host
        self_addr = host.selected_address if host is not None else ""
        self._open_composer(
            name, self._subnode_op(name, _checksum(self_addr or "") or ""))

    # --- renewal -----------------------------------------------------------

    def _expiry_of(self, name: str) -> int | None:
        n = self._names_by_l.get(name.lower())
        return n.expiry_ts if n is not None else None

    def _renew(self, name: str) -> None:
        from ..ens_app import _is_eth_2ld
        if self._panel is None or not _is_eth_2ld(name):
            return
        chain = self._mainnet()
        if chain is None:
            return
        self._open_composer(name, self._renew_op(name, chain))

    # --- transfer ----------------------------------------------------------

    def _transfer(self, name: str) -> None:
        from ..ens_app import _is_eth_2ld
        if self._panel is None or not _is_eth_2ld(name):
            return
        host = self.host
        addr = host.selected_address if host is not None else None
        if not addr:
            return
        self._open_composer(name, self._transfer_op(name, addr))

    # --- set manager (reclaim) --------------------------------------------

    def _set_manager(self, name: str) -> None:
        from ..ens_app import _is_eth_2ld
        if self._panel is None:
            return
        host = self.host
        addr = host.selected_address if host is not None else ""
        self_addr = _checksum(addr or "") or ""
        nl = name.lower()
        if EnsName(name).is_subdomain:
            # Subdomain: (re)assign its manager through the parent, if we own the
            # parent. Wrapped subnodes go through the NameWrapper — not yet.
            if nl in self._subnode_manageable:
                self._open_composer(
                    name, self._set_subnode_manager_op(name, self_addr))
        elif _is_eth_2ld(name) and nl not in self._wrapped:
            # 2LD: the registrant reclaims via the BaseRegistrar.
            self._open_composer(
                name, self._set_manager_op(name, self_addr))

    # --- op construction ---------------------------------------------------

    def _record_op(self, name: str, res: str, *, preset: str | None = None,
                   key: str = "", coin: str = "", value: str = "",
                   confirm_label: str = "Save record") -> _EnsOp:
        """One op covering every resolver-record write (addr / content / text /
        coin). ``preset`` locks the type; the build/validate/decoded callables
        dispatch on the field group's chosen kind, mirroring the old
        ``_write_record`` logic — but now lazily, on Confirm, inside the rich
        composer (validation surfaces as a disabled Confirm, not a popup)."""
        from .. import ens_write

        def make_fields(_composer: _EnsWriteComposer) -> QWidget:
            return _RecordFields(name, preset=preset, key=key, coin=coin,
                                 value=value)

        def build(nm: str, fields: Any) -> tuple[str, str]:
            kind, extra, val = fields.result_values()
            if kind == "ETH address":
                addr = _checksum(val)
                if val and addr is None:
                    raise ValueError("That doesn't look like a valid 0x address.")
                return ens_write.set_addr(res, nm, addr or ens_write.ZERO_ADDRESS)
            if kind == "Content (IPFS)":
                return ens_write.set_contenthash(res, nm, val)
            if kind == "Text record":
                if not extra.strip():
                    raise ValueError("Enter a record key.")
                return ens_write.set_text(res, nm, extra.strip(), val)
            coin_type = ens_write.COIN_TYPES.get(extra)
            if coin_type is None:
                raise ValueError("Pick a coin.")
            addr = _checksum(val)
            if val and addr is None:
                raise ValueError("That doesn't look like a valid 0x address.")
            payload = ens_write.eth_addr_bytes(addr) if addr else b""
            return ens_write.set_coin_addr(res, nm, coin_type, payload)

        def decoded(nm: str, fields: Any) -> dict:
            kind, extra, val = fields.result_values()
            node = {"name": "node", "type": "bytes32", "value": nm}
            if kind == "ETH address":
                return {"function": "setAddr", "args": [
                    node, {"name": "a", "type": "address",
                           "value": _checksum(val) or ens_write.ZERO_ADDRESS}]}
            if kind == "Content (IPFS)":
                return {"function": "setContenthash", "args": [
                    node, {"name": "hash", "type": "bytes",
                           "value": val or "(clear)"}]}
            if kind == "Text record":
                return {"function": "setText", "args": [
                    node, {"name": "key", "type": "string", "value": extra},
                    {"name": "value", "type": "string", "value": val}]}
            return {"function": "setAddr", "args": [
                node, {"name": "coinType", "type": "uint256",
                       "value": str(ens_write.COIN_TYPES.get(extra, "?"))},
                {"name": "a", "type": "bytes",
                 "value": _checksum(val) or "(clear)"}]}

        return _EnsOp(
            title=f"{confirm_label} · {name}", confirm_label=confirm_label,
            make_fields=make_fields, build=build, decoded=decoded)

    def _subnode_op(self, name: str, self_addr: str) -> _EnsOp:
        from .. import ens_write
        wrapped = name.lower() in self._wrapped

        def make_fields(composer: _EnsWriteComposer) -> QWidget:
            return _SubnodeFields(
                name, self_addr,
                make_address_field=composer._make_address_field)

        def _parse(fields: Any) -> tuple[str, str]:
            label, owner_in = fields.values()
            label = label.strip().lower()
            if not label:
                raise ValueError("Enter a subdomain label.")
            if "." in label:
                raise ValueError("A subdomain label can't contain a dot.")
            owner = _checksum(owner_in)
            if owner is None:
                raise ValueError("The owner must be a valid 0x address.")
            return label, owner

        def build(nm: str, fields: Any) -> tuple[str, str]:
            label, owner = _parse(fields)
            return ens_write.add_subnode(nm, label, owner, wrapped=wrapped)

        def decoded(nm: str, fields: Any) -> dict:
            label, owner_in = fields.values()
            return {"function": "setSubnodeRecord", "args": [
                {"name": "parentNode", "type": "bytes32", "value": nm},
                {"name": "label", "type": "string", "value": label or "…"},
                {"name": "owner", "type": "address",
                 "value": _checksum(owner_in) or owner_in or "0x…"}]}

        return _EnsOp(
            title=f"Add subdomain · {name}", confirm_label="Add subdomain",
            make_fields=make_fields, build=build, decoded=decoded,
            rediscover=True)

    def _renew_op(self, name: str, chain) -> _EnsOp:
        from .. import ens_write
        label = name.split(".")[0]
        expiry_ts = self._expiry_of(name)

        def make_fields(composer: _EnsWriteComposer) -> QWidget:
            fields = _RenewFields(name, expiry_ts)
            # Quote one year's price + ETH/USD once; the field group scales it
            # to the chosen term locally (renewal cost is linear in duration).
            # The quote's ``set_quote`` emits ``changed`` → the composer re-
            # estimates gas + re-simulates with the now-known payable value.
            quote = EnsRenewPriceWorker(
                chain, label, ens_write.SECONDS_PER_YEAR, with_usd=True)
            quote.ready.connect(fields.set_quote)
            composer._start_worker(quote)
            return fields

        def build(nm: str, fields: Any) -> tuple[str, str]:
            return ens_write.renew(label, fields.duration_seconds())

        def value_wei(fields: Any) -> int:
            # Overpay slightly: the price is USD-denominated and reconverted to
            # wei at execution time, so ETH dropping between now and mining would
            # make the exact amount short and revert. The controller refunds the
            # excess, so a 10% buffer is free insurance. This value flows through
            # _build_request into the gas probe AND the simulation.
            price = fields.selected_value_wei()
            if price is None:
                return 0
            return price + price // 10

        def validate(fields: Any) -> str | None:
            if fields.duration_seconds() <= 0:
                return "Pick a later expiry date."
            if fields.selected_value_wei() is None:
                return "Fetching renewal price…"
            return None

        def decoded(nm: str, fields: Any) -> dict:
            return {"function": "renew", "args": [
                {"name": "name", "type": "string", "value": label},
                {"name": "duration", "type": "uint256",
                 "value": str(fields.duration_seconds())}]}

        return _EnsOp(
            title=f"Renew · {name}", confirm_label="Renew",
            make_fields=make_fields, build=build, decoded=decoded,
            value_wei=value_wei, validate=validate, rediscover=True,
            payable=True)

    def _transfer_op(self, name: str, from_addr: str) -> _EnsOp:
        from eth_utils import to_checksum_address

        from .. import ens_write
        wrapped = name.lower() in self._wrapped
        sender = to_checksum_address(from_addr)

        def make_fields(composer: _EnsWriteComposer) -> QWidget:
            return _RecipientFields(
                make_address_field=composer._make_address_field)

        def build(nm: str, fields: Any) -> tuple[str, str]:
            to = _checksum(fields.value())
            if to is None:
                raise ValueError("The recipient must be a valid 0x address.")
            return ens_write.transfer_name(nm, sender, to, wrapped=wrapped)

        def decoded(nm: str, fields: Any) -> dict:
            to = _checksum(fields.value()) or fields.value() or "0x…"
            return {"function": "safeTransferFrom", "args": [
                {"name": "from", "type": "address", "value": sender},
                {"name": "to", "type": "address", "value": to},
                {"name": "tokenId", "type": "uint256", "value": nm}]}

        note = (
            "This transfers the wrapped name — both ownership and the manager "
            "role move together." if wrapped else
            "This moves ownership (the name's NFT). The manager role stays with "
            "you until the new owner reclaims it.")
        return _EnsOp(
            title=f"Transfer · {name}", confirm_label="Transfer name",
            make_fields=make_fields, build=build, decoded=decoded,
            rediscover=True, note=note,
            # Same right-arrow as the Transfer button (and the tokens Send).
            confirm_icon_names=("go-next",),
            confirm_fallback=QStyle.StandardPixmap.SP_ArrowForward)

    def _set_manager_op(self, name: str, self_addr: str) -> _EnsOp:
        from .. import ens_write

        def make_fields(composer: _EnsWriteComposer) -> QWidget:
            # Default to the user's own address — reclaiming the manager role to
            # yourself (so you can set records) is the common case.
            return _RecipientFields(
                make_address_field=composer._make_address_field,
                label="Manager", initial=self_addr,
                placeholder="0x… manager address")

        def build(nm: str, fields: Any) -> tuple[str, str]:
            mgr = _checksum(fields.value())
            if mgr is None:
                raise ValueError("The manager must be a valid 0x address.")
            return ens_write.set_manager(nm, mgr)

        def decoded(nm: str, fields: Any) -> dict:
            mgr = _checksum(fields.value()) or fields.value() or "0x…"
            return {"function": "reclaim", "args": [
                {"name": "id", "type": "uint256", "value": nm},
                {"name": "owner", "type": "address", "value": mgr}]}

        return _EnsOp(
            title=f"Set manager · {name}", confirm_label="Set manager",
            make_fields=make_fields, build=build, decoded=decoded,
            rediscover=True,
            note="The manager (registry controller) is the role that sets the "
                 "resolver, records and subdomains. As the owner you can "
                 "reclaim it — to yourself or anyone else.")

    def _set_subnode_manager_op(self, name: str, self_addr: str) -> _EnsOp:
        """Set the manager of a subdomain you own the PARENT of, via
        registry.setSubnodeOwner — the parent controller's power to reassign a
        subnode. Defaults to yourself (so you can then edit its records)."""
        from .. import ens_write
        parent = name.split(".", 1)[1]
        label = name.split(".", 1)[0]

        def make_fields(composer: _EnsWriteComposer) -> QWidget:
            return _RecipientFields(
                make_address_field=composer._make_address_field,
                label="Manager", initial=self_addr,
                placeholder="0x… manager address")

        def build(_nm: str, fields: Any) -> tuple[str, str]:
            mgr = _checksum(fields.value())
            if mgr is None:
                raise ValueError("The manager must be a valid 0x address.")
            return ens_write.set_subnode_manager(parent, label, mgr)

        def decoded(_nm: str, fields: Any) -> dict:
            mgr = _checksum(fields.value()) or fields.value() or "0x…"
            return {"function": "setSubnodeOwner", "args": [
                {"name": "node", "type": "bytes32", "value": parent},
                {"name": "label", "type": "bytes32", "value": label},
                {"name": "owner", "type": "address", "value": mgr}]}

        return _EnsOp(
            title=f"Set manager · {name}", confirm_label="Set manager",
            make_fields=make_fields, build=build, decoded=decoded,
            rediscover=True,
            note=f"You own {parent}, so you can set this subdomain's manager. "
                 "Set it to yourself to then edit its records.")

    def _remove_record_op(self, name: str, res: str, label: str,
                          value: str) -> _EnsOp:
        """Clear a single resolver record — the removal of the ``label`` row.
        ``label`` is the tree row label from ``_record_rows``: ``address`` /
        ``address (COIN)`` / ``content`` / a text key. Input-less: the value is
        forced empty (0x0 / b"" / ""), so the composer shows the decoded
        clearing call + a note and the user just reviews + signs."""
        from .. import ens_write
        lab = label.strip()

        def _coin_of(lb: str) -> str | None:
            if lb.startswith("address (") and lb.endswith(")"):
                return lb[len("address ("):-1]
            return None

        def build(nm: str, _fields: Any) -> tuple[str, str]:
            if lab == "address":
                return ens_write.set_addr(res, nm, ens_write.ZERO_ADDRESS)
            coin = _coin_of(lab)
            if coin is not None:
                coin_type = ens_write.COIN_TYPES.get(coin)
                if coin_type is None:
                    raise ValueError("Unknown coin.")
                return ens_write.set_coin_addr(res, nm, coin_type, b"")
            if lab == "content":
                return ens_write.set_contenthash(res, nm, "")
            return ens_write.set_text(res, nm, lab, "")

        def decoded(nm: str, _fields: Any) -> dict:
            node = {"name": "node", "type": "bytes32", "value": nm}
            if lab == "address":
                return {"function": "setAddr", "args": [
                    node, {"name": "a", "type": "address",
                           "value": ens_write.ZERO_ADDRESS}]}
            coin = _coin_of(lab)
            if coin is not None:
                return {"function": "setAddr", "args": [
                    node, {"name": "coinType", "type": "uint256",
                           "value": str(ens_write.COIN_TYPES.get(coin, "?"))},
                    {"name": "a", "type": "bytes", "value": "(clear)"}]}
            if lab == "content":
                return {"function": "setContenthash", "args": [
                    node, {"name": "hash", "type": "bytes", "value": "(clear)"}]}
            return {"function": "setText", "args": [
                node, {"name": "key", "type": "string", "value": lab},
                {"name": "value", "type": "string", "value": "(clear)"}]}

        shown = f" ({value})" if value else ""
        return _EnsOp(
            title=f"Remove record · {name}", confirm_label="Remove record",
            make_fields=lambda _composer: None, build=build, decoded=decoded,
            note=f"This clears the “{lab}” record{shown}.",
            confirm_icon_names=("edit-delete", "user-trash", "list-remove"),
            confirm_fallback=QStyle.StandardPixmap.SP_TrashIcon)

    def _remove_subnode_op(self, name: str) -> _EnsOp:
        """Delete a subdomain you own the PARENT of, via
        registry.setSubnodeRecord(parent, label, 0, 0, 0) — clears its owner,
        resolver and records. Input-less + rediscovers (the name disappears)."""
        from .. import ens_write
        parent = name.split(".", 1)[1]
        label = name.split(".", 1)[0]

        def build(_nm: str, _fields: Any) -> tuple[str, str]:
            return ens_write.remove_subnode(parent, label)

        def decoded(_nm: str, _fields: Any) -> dict:
            return {"function": "setSubnodeRecord", "args": [
                {"name": "parentNode", "type": "bytes32", "value": parent},
                {"name": "label", "type": "bytes32", "value": label},
                {"name": "owner", "type": "address",
                 "value": ens_write.ZERO_ADDRESS},
                {"name": "resolver", "type": "address",
                 "value": ens_write.ZERO_ADDRESS},
                {"name": "ttl", "type": "uint64", "value": "0"}]}

        return _EnsOp(
            title=f"Remove subdomain · {name}",
            confirm_label="Remove subdomain",
            make_fields=lambda _composer: None, build=build, decoded=decoded,
            rediscover=True,
            note=f"This deletes {name}. As the owner of {parent} you can remove "
                 "the subdomain — clearing its owner, resolver and records.",
            confirm_icon_names=("edit-delete", "user-trash", "list-remove"),
            confirm_fallback=QStyle.StandardPixmap.SP_TrashIcon)

    def _set_resolver_op(self, name: str) -> _EnsOp:
        """The input-less set-resolver bootstrap (the resolver-gate target)."""
        from .. import ens_write

        def build(nm: str, _fields: Any) -> tuple[str, str]:
            return ens_write.set_resolver(nm)

        def decoded(nm: str, _fields: Any) -> dict:
            return {"function": "setResolver", "args": [
                {"name": "node", "type": "bytes32", "value": nm},
                {"name": "resolver", "type": "address",
                 "value": ens_write.PUBLIC_RESOLVER}]}

        return _EnsOp(
            title=f"Set resolver · {name}", confirm_label="Set resolver",
            make_fields=lambda _composer: None, build=build, decoded=decoded)

    # --- write plumbing ----------------------------------------------------

    def _resolver_for(self, name: str) -> str | None:
        res = self._resolver_cache.get(name.lower())
        if res and int(res, 16) != 0:
            return res
        return None

    def _ensure_resolver(self, name: str) -> str | None:
        """The name's resolver, or None — in which case offer to point the name
        at the default public resolver first (records can't be stored without
        one). A composer can't target a nonexistent resolver, so this stays a
        PRE-STEP: on accept we open the set-resolver composer and defer the
        record write (the user re-issues it once that confirms)."""
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
            self._open_composer(name, self._set_resolver_op(name))
        return None

    def _warn(self, text: str) -> None:
        if self._panel is None:
            return
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(self._panel, "ENS", text)

    def _open_composer(self, name: str, op: _EnsOp,
                       *, after_confirm: Callable[[], None] | None = None) -> None:
        """Open the rich ``_EnsWriteComposer`` for ``op`` via the host opener,
        wiring the post-confirm refresh (rediscover vs. force-reread records).
        ``after_confirm`` runs first on confirmation (e.g. purge a deleted
        subdomain from caches). Falls back to a no-op when the host can't host a
        composer (mirrors the old ``request_transaction`` capability check)."""
        host = self.host
        chain = self._mainnet()
        addr = host.selected_address if host is not None else None
        if (host is None or chain is None or not addr
                or not hasattr(host, "open_ens_composer")):
            return
        from eth_utils import to_checksum_address
        nm = name

        def _on_confirmed(_receipt: object) -> None:
            # The write mined: refresh against CONFIRMED (chain-head) state right
            # away. ``rediscover`` re-runs name discovery — a subdomain add (a
            # new name) or a renewal (a new expiry) shows up there, not in the
            # resolver records; a plain record write force-re-reads instead so
            # the new value shows now, marked "confirmed" until finality earns
            # it the ✓ (rather than waiting on finality to show it at all).
            if after_confirm is not None:
                after_confirm()
            if op.rediscover:
                # catchup=True: a transfer/reclaim changed ownership, so wait
                # for the verified head to reflect it before the proof overwrites
                # the fast read that already shows the new owner/manager.
                self._on_refresh(catchup=True)
            else:
                self._on_records_requested(nm, force=True)

        host.open_ens_composer(name, op, chain, to_checksum_address(addr),
                               on_confirmed=_on_confirmed)

    def _purge_name_from_caches(self, name: str) -> None:
        """Drop a just-deleted subdomain from EVERY account's disk cache, so the
        cross-account subnode surfacing (``_with_cross_account_subdomains``)
        can't re-float it from a sibling account's now-stale cache after
        removal. The current account's cache is rewritten by the rediscover that
        follows anyway; a sibling's isn't until that account is next loaded."""
        nl = name.lower()
        for acct in self._store.accounts:
            addr = acct.get("address", "")
            if not addr:
                continue
            cached = self._cache.load(ENS_CHAIN_ID, addr)
            if cached and any(n.name.lower() == nl for n in cached):
                self._cache.save(ENS_CHAIN_ID, addr,
                                 [n for n in cached if n.name.lower() != nl])

    # --- worker lifetime --------------------------------------------------

    def _start(self, worker: QThread) -> None:
        host = self.host
        if host is not None and hasattr(host, "start_worker"):
            host.start_worker(worker)
            return
        # No worker pump (a minimal/test host): keep a ref so the running
        # QThread isn't garbage-collected mid-run — Qt's QThread destructor
        # aborts the whole process if the thread is still running. Self-evict
        # on finished (the convention MainWindow.start_worker follows).
        self._workers.add(worker)
        worker.finished.connect(lambda w=worker: self._workers.discard(w))
        worker.start()
