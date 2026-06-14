"""WalletsPlugin — accounts tree + details panel + account actions.

Step 4 of the plugin refactor. Owns:
- The QTreeWidget listing Ledger / Hot wallet / Watch-only accounts.
- The DetailsPanel showing the selected account's address, path, QR,
  and the Set-as-default button.
- The three account actions (Add / Copy / Remove) and their button
  row, mounted at the top of the plugin's own widget rather than on
  the slot's bottom row — they're conceptually part of the Wallets
  view, not generic plugin actions, and the layout matches the
  pre-refactor look exactly.
- The internal vertical splitter between tree and details; its state
  is persisted via ``splitter_state``/``restore_splitter_state``.

Wallets is the source of the selection broadcast — when the user
picks an address, the plugin emits ``selected_address_changed`` and
MainWindow forwards that to the right slot, which broadcasts to its
mounted plugins (Tokens, Transactions). ``default_account_changed``
fires when the user toggles which address is the dapp-facing default,
so the host can refresh the status bar.
"""

from __future__ import annotations

import logging
from typing import Optional

import io

import segno


log = logging.getLogger("qeth.plugin.wallets")

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction, QFont, QIcon, QKeySequence, QPalette, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QProgressBar, QPushButton,
    QSizePolicy, QSpinBox, QSplitter, QStyle, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QThread

from ..alerts import confirm, error, info, warn
from ..ledger import DiscoveredAccount, LedgerWorker, PATH_SCHEMES
from ..plugin import Plugin

# Item data role carrying an account's user label. Present only on labeled
# account rows; the shared selection delegate (ui.py) reads it to paint
# those rows with a sticky-note background. UserRole holds the address.
ACCOUNT_LABEL_ROLE = Qt.ItemDataRole.UserRole + 1
# Stable key on collapsible group/root rows so _rebuild_tree can carry
# the user's expand/collapse state across a rebuild (e.g. when switching
# the default account) instead of force-expanding everything each time.
EXPAND_KEY_ROLE = Qt.ItemDataRole.UserRole + 2


def _palette_aware_error_color(palette) -> str:
    """Pick a "this is an error" red that contrasts against the
    active palette's window background. QPalette has no Error
    role — apps reach for ``BrightText`` (theme-dependent, often
    not actually red) or hardcoded values; we sample the Window
    luminance and pick a Material-inspired red that reads on
    that background.

    See also feedback_theme_safe_colors.md — same approach we
    take for link colours."""
    window = palette.color(QPalette.ColorRole.Window)
    lum = window.red() * 0.299 + window.green() * 0.587 + window.blue() * 0.114
    # Dark window → light red (Material red 300);
    # light window → deep red (Material red 800).
    return "#ff6b6b" if lum < 128 else "#c62828"


def _palette_aware_ok_color(palette) -> str:
    """Companion to ``_palette_aware_error_color`` — a green that
    reads as "ok / success" on the active theme."""
    window = palette.color(QPalette.ColorRole.Window)
    lum = window.red() * 0.299 + window.green() * 0.587 + window.blue() * 0.114
    # Light green on dark, deep green on light.
    return "#81c784" if lum < 128 else "#2e7d32"


class _ReorderTree(QTreeWidget):
    """QTreeWidget that allows drag-and-drop reorder of address
    leaves *within the same parent group*. Dropping into a different
    scheme group is rejected so a Ledger Default account can't end
    up under Legacy (or vice-versa) — the scheme is metadata of the
    address, not just a display nest. After a successful drop the
    widget emits ``reorder_committed`` so the plugin can rewrite
    the on-disk account list to match."""

    reorder_committed = Signal()
    # Fired when the user presses Return / Enter while a row is
    # selected. The plugin connects this to "connect to browser"
    # (same as double-clicking the address). Carries the address.
    enter_pressed = Signal(str)

    def drawRow(self, painter, option, index):  # noqa: N802 — Qt method
        # Suppress the row hover tint. The token/tx tables disable hover
        # via stylesheet; without this the left wallet tree would be the
        # only panel that highlights on hover — an inconsistency between
        # the left and right columns. Strip State_MouseOver at the row
        # level (the delegate strips it per-item); selection still paints.
        option.state &= ~QStyle.StateFlag.State_MouseOver
        super().drawRow(painter, option, index)

    def keyPressEvent(self, event):  # noqa: N802 — Qt method name
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            current = self.currentItem()
            if current is not None:
                addr = current.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(addr, str) and addr:
                    self.enter_pressed.emit(addr)
                    return
        super().keyPressEvent(event)

    def dropEvent(self, event):  # noqa: N802 — Qt method name
        source_items = self.selectedItems()
        if not source_items:
            return super().dropEvent(event)
        # All selected items must share a parent — otherwise we can't
        # honour "same parent only" cleanly.
        source_parent = source_items[0].parent()
        if any(it.parent() is not source_parent for it in source_items):
            event.ignore()
            return
        # Compute the destination parent based on Qt's drop indicator.
        target = self.itemAt(event.position().toPoint())
        indicator = self.dropIndicatorPosition()
        if indicator == QAbstractItemView.DropIndicatorPosition.OnItem:
            dest_parent = target
        elif indicator in (
            QAbstractItemView.DropIndicatorPosition.AboveItem,
            QAbstractItemView.DropIndicatorPosition.BelowItem,
        ):
            dest_parent = target.parent() if target is not None else None
        else:  # OnViewport — would drop at top level; refuse.
            event.ignore()
            return
        if dest_parent is not source_parent:
            event.ignore()
            return
        # Capture the dragged addresses BEFORE the drop. Qt's
        # InternalMove may destroy + recreate the source items at the
        # destination, so item pointers can dangle across the
        # super().dropEvent call — but the address-string data they
        # carry is reliable, and the new items will carry the same
        # value in UserRole. We use it to re-select after the drop so
        # the plugin slots see ``selected_address_changed(addr)`` and
        # repopulate, instead of being left with the mid-drop
        # ``None`` emission that empties the panels.
        dragged_addrs = [
            it.data(0, Qt.ItemDataRole.UserRole) for it in source_items
        ]
        dragged_addrs = [a for a in dragged_addrs if isinstance(a, str)]
        super().dropEvent(event)
        if dragged_addrs:
            self.clearSelection()
            first = None
            for addr in dragged_addrs:
                it = self._find_by_address(addr)
                if it is not None:
                    it.setSelected(True)
                    if first is None:
                        first = it
            if first is not None:
                self.setCurrentItem(first)
        self.reorder_committed.emit()

    def _find_by_address(self, addr: str):
        """Depth-first search for the leaf carrying ``addr`` in
        UserRole. Used after a drop to relocate items whose pointers
        were invalidated by Qt's row remove/insert."""

        def walk(item):
            if item.data(0, Qt.ItemDataRole.UserRole) == addr:
                return item
            for i in range(item.childCount()):
                r = walk(item.child(i))
                if r is not None:
                    return r
            return None

        for i in range(self.topLevelItemCount()):
            r = walk(self.topLevelItem(i))
            if r is not None:
                return r
        return None


class WalletsPlugin(Plugin):
    name = "Accounts"

    # Emitted when the user's tree selection narrows to a single
    # account (or clears). MainWindow forwards this to the right
    # slot's account-broadcast.
    selected_address_changed = Signal(object)   # str or None
    # Fired when the user changes which address is the dapp-facing
    # default. Host listens so it can refresh the status bar label.
    default_account_changed = Signal()

    def __init__(self, store):
        super().__init__()
        self._store = store
        # Built lazily in widget()/_build so importing this module
        # doesn't require a running Qt event loop.
        self._container: Optional[QWidget] = None
        self._tree: Optional[QTreeWidget] = None
        self._details = None
        self._splitter: Optional[QSplitter] = None
        self._account_buttons: list[QPushButton] = []
        self.act_add: Optional[QAction] = None
        self.act_copy: Optional[QAction] = None
        self.act_remove: Optional[QAction] = None
        # ENS workers kicked from post-import label-fill; tracked
        # here so Python's GC doesn't drop them mid-run (Qt's
        # QThread destructor aborts on a still-running thread).
        self._ens_workers: list[QThread] = []
        # Lower-case addresses whose ENS label was resolved through Helios
        # (proof-verified) — drives a "verified" tooltip on the tree row.
        self._ens_verified: set[str] = set()

    # --- Plugin contract ----------------------------------------------------

    def widget(self) -> QWidget:
        if self._container is None:
            self._build()
            self._rebuild_tree()
        assert self._container is not None  # _build() sets it
        return self._container

    def action_widgets(self):
        # Add / Copy / Remove mount on the slot's bottom row (like the
        # Tokens panel's +/-/star/eye), so this panel is structurally
        # symmetric with the tabbed slot — [tab][list][actions] — and the
        # account list lines up with the token list. _build() populates
        # _account_buttons before the slot first asks for them.
        return list(self._account_buttons)

    # --- public surface (read by MainWindow + Host implementations) --------

    @property
    def selected_address(self) -> Optional[str]:
        """The single currently-selected address, or None when zero or
        multiple are selected."""
        addrs = self.selected_addresses()
        return addrs[0] if len(addrs) == 1 else None

    def selected_addresses(self) -> list[str]:
        if self._tree is None:
            return []
        out = []
        for it in self._tree.selectedItems():
            addr = it.data(0, Qt.ItemDataRole.UserRole)
            if addr:
                out.append(addr)
        return out

    def select_address(self, address: str) -> bool:
        """Programmatically focus the tree on the leaf carrying
        ``address`` (case-insensitive). Returns True if the address
        was found and selected, False otherwise. Used by MainWindow
        after a broadcast to make sure the user is looking at the
        ``from`` account when the pending row appears."""
        if self._tree is None or not address:
            return False
        wanted = address.lower()

        def walk(item):
            addr = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(addr, str) and addr.lower() == wanted:
                return item
            for i in range(item.childCount()):
                hit = walk(item.child(i))
                if hit is not None:
                    return hit
            return None

        for i in range(self._tree.topLevelItemCount()):
            hit = walk(self._tree.topLevelItem(i))
            if hit is not None:
                # Already the sole selection? Do nothing. clearSelection()
                # would broadcast account=None — which clears the
                # transactions view and its cached activities — then the
                # re-select forces a full show_transactions + re-fetch of
                # every row's activity (the "redraws every pic" on send).
                # A no-op keeps a just-sent pending row a cheap one-row
                # prepend.
                sel = self._tree.selectedItems()
                if (self._tree.currentItem() is hit
                        and len(sel) == 1 and sel[0] is hit):
                    return True
                self._tree.clearSelection()
                self._tree.setCurrentItem(hit)
                hit.setSelected(True)
                return True
        return False

    def splitter_state(self) -> str:
        if self._splitter is None:
            return ""
        return bytes(self._splitter.saveState().toHex().data()).decode()

    def restore_splitter_state(self, state_hex: str) -> None:
        if self._splitter is None or not state_hex:
            return
        try:
            self._splitter.restoreState(QByteArray.fromHex(state_hex.encode()))
        except Exception:
            pass

    def rebuild_tree(self) -> None:
        """Public re-entry point for MainWindow / host code that
        modifies accounts and needs the tree to reflect that."""
        self._rebuild_tree()

    # --- widget building ----------------------------------------------------

    def _build(self) -> None:
        self._container = QWidget()
        v = QVBoxLayout(self._container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Build the account-action buttons (Add / Copy / Remove). They are
        # NOT added here: the slot mounts them on its bottom row via
        # action_widgets(), so this panel mirrors the tabbed Tokens slot
        # ([tab][list][actions]) and the two lists' tops align.
        self._build_account_actions()

        # Middle: vertical splitter (tree on top, details on bottom).
        self._splitter = QSplitter(Qt.Orientation.Vertical)

        self._tree = _ReorderTree()
        self._tree.setHeaderLabels(["Accounts"])
        self._tree.setRootIsDecorated(True)
        # Group roots carry themed icons; keep them small + aligned with
        # the toolbar/menu icons rather than the style's larger default.
        self._tree.setIconSize(QSize(16, 16))
        # The slot now shows an "Accounts" tab (Slot show_single_tab) that
        # labels this list, so the tree's own one-column header would just
        # repeat it — hide it. (This also retires the old header-vs-rows
        # alignment tweak: a hidden header can't be misaligned.)
        self._tree.setHeaderHidden(True)
        self._tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        # Never show a horizontal scrollbar in the wallet tree —
        # addresses are 42 chars + label and the left pane gets
        # tight on smaller windows. Middle-elide handles the
        # overflow gracefully (an address truncated like
        # ``0xbabe…79f67`` still gives the user enough to
        # recognise it). Scrollbars in this position are a UX
        # papercut: the user just wants to see addresses, not
        # work a scrollbar.
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Drag now reorders the address rows instead of accumulating a
        # selection — multi-select is still available via Ctrl/Shift +
        # click for the Remove button's bulk-remove path. InternalMove
        # restricts dragging to within this widget; the subclass's
        # dropEvent further restricts to within the same parent group.
        self._tree.setDragEnabled(True)
        self._tree.setAcceptDrops(True)
        self._tree.setDropIndicatorShown(True)
        self._tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._tree.itemSelectionChanged.connect(self._on_tree_selection)
        self._tree.reorder_committed.connect(self._on_tree_reordered)
        # Double-click an address leaf = "Connect to browser". The
        # button + right-click menu offer the same action; this is
        # just the no-friction path for the user's primary
        # action-on-account.
        self._tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        # Enter on the focused tree = same thing.
        self._tree.enter_pressed.connect(self._on_tree_enter_pressed)
        # Right-click menu mirrors the top action row (Add / Copy /
        # Remove) plus Set-as-default — so every button has a menu
        # equivalent and every menu item has a button equivalent.
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(
            self._on_tree_context_menu
        )
        # Make Ctrl+C / Del work while the accounts tree has focus
        # (the actions carry the shortcuts; the tree is their context).
        # _build_account_actions() above created these.
        assert self.act_copy is not None and self.act_remove is not None
        self._tree.addAction(self.act_copy)
        self._tree.addAction(self.act_remove)
        self._splitter.addWidget(self._tree)

        self._details = DetailsPanel()
        self._details.set_default_requested.connect(self._set_default)
        self._details.label_changed.connect(self._on_label_changed)
        self._details.sign_message_requested.connect(self._on_sign_message)
        details_wrap = QFrame()
        details_wrap.setFrameShape(QFrame.Shape.StyledPanel)
        dlay = QVBoxLayout(details_wrap)
        # Symmetric bottom margin: without it the last button (Connect to
        # Browser) sits flush against the framed panel's bottom border.
        dlay.setContentsMargins(12, 12, 12, 12)
        dlay.addWidget(self._details)
        self._splitter.addWidget(details_wrap)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([290, 365])
        v.addWidget(self._splitter, 1)

    def _build_account_actions(self) -> None:
        """Build the account-level action buttons (Add / Copy / Remove)
        into ``self._account_buttons``. The slot mounts them on its bottom
        row via ``action_widgets()`` (they lived in a top toolbar, then a
        QMainWindow toolbar, before the plugin refactor)."""
        style_proxy = QApplication.style()
        self.act_add = QAction(
            QIcon.fromTheme("document-new",
                            style_proxy.standardIcon(QStyle.StandardPixmap.SP_FileIcon)),
            "&Add Account",
        )
        self.act_add.setToolTip("Add account")
        # Sub-actions used by both the toolbar dropdown and the
        # tree's right-click menu so the two entry points agree on
        # which dialogs they open.
        # Per-source icons so the picker menu isn't a flat list of text:
        # a hardware device for Ledger, a key for the local hot wallet,
        # an eye for watch-only, and an import glyph for the two
        # external-wallet importers. Each falls back to a QStyle
        # standard icon when the theme lacks the named one.
        def _icon(*names_then_fallback):
            *names, fb = names_then_fallback
            ic = QIcon()
            for n in names:
                ic = QIcon.fromTheme(n)
                if not ic.isNull():
                    return ic
            return style_proxy.standardIcon(fb)

        self.act_add_ledger = QAction("&Ledger Account…", self)
        self.act_add_ledger.setIcon(_icon(
            "drive-removable-media-usb", "media-flash", "drive-harddisk",
            QStyle.StandardPixmap.SP_DriveFDIcon,
        ))
        self.act_add_ledger.triggered.connect(self._add_ledger)
        self.act_add_hot = QAction("&Hot Wallet…", self)
        self.act_add_hot.setIcon(_icon(
            "dialog-password", "security-high", QStyle.StandardPixmap.SP_FileIcon,
        ))
        self.act_add_hot.triggered.connect(self._add_hot_wallet)
        self.act_add_watch = QAction("&Watch-only Address…", self)
        # An eye for "watch only". Theme names vary: Breeze ships
        # view-visible, Adwaita (and SE98's fallback) ships the show-
        # password eye as view-reveal-symbolic — try both before the
        # generic QStyle fallback.
        self.act_add_watch.setIcon(_icon(
            "view-visible", "view-reveal-symbolic", "eye", "eye-symbolic",
            QStyle.StandardPixmap.SP_FileDialogContentsView,
        ))
        self.act_add_watch.triggered.connect(self._add_watch_only)
        # Import-from-other-wallet actions live below a separator
        # so the primary "add a new account" actions stay grouped
        # at the top. Each external source is its own entry +
        # dialog so the per-source UX (passphrase fields, paths)
        # isn't muddled with tab switching.
        _import_icon = _icon(
            "document-import", "document-open", QStyle.StandardPixmap.SP_DialogOpenButton,
        )
        self.act_import_brownie = QAction("Import from &Brownie…", self)
        self.act_import_brownie.setIcon(_import_icon)
        self.act_import_brownie.triggered.connect(self._import_from_brownie)
        self.act_import_frame = QAction("Import from &Frame…", self)
        self.act_import_frame.setIcon(_import_icon)
        self.act_import_frame.triggered.connect(self._import_from_frame)
        # Triggering act_add itself shows the picker menu — invoked
        # via the right-click "Add account" item in the tree.
        self.act_add.triggered.connect(self._show_add_account_menu)

        self.act_copy = QAction(
            QIcon.fromTheme("edit-copy",
                            style_proxy.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton)),
            "&Copy Address",
        )
        self.act_copy.setEnabled(False)
        self.act_copy.setShortcut(QKeySequence.StandardKey.Copy)
        self.act_copy.triggered.connect(self._copy_selected_address)

        self.act_remove = QAction(
            QIcon.fromTheme("list-remove",
                            style_proxy.standardIcon(QStyle.StandardPixmap.SP_TrashIcon)),
            "&Remove Account",
        )
        self.act_remove.setEnabled(False)
        self.act_remove.setShortcut(QKeySequence.StandardKey.Delete)
        self.act_remove.triggered.connect(self._remove_selected_account)

        # Scope the shortcuts to the accounts tree: Ctrl+C / Del act on
        # the selected address only when that panel has focus, so they
        # don't shadow copy/delete in the token or transaction tables.
        # (The tree is added to these actions in _build, once it exists.)
        for act in (self.act_copy, self.act_remove):
            act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)

        # Add / Copy / Remove are QPushButtons styled like the Tokens
        # panel's "Send" button (raised, icon + label) so the two slots'
        # bottom action rows match. Add carries the source-picker menu
        # (Ledger / hot wallet / watch-only / import); a QPushButton with
        # setMenu() pops it on click, with a dropdown indicator.
        add_btn = QPushButton(self.act_add.text())
        add_btn.setIcon(self.act_add.icon())
        add_btn.setToolTip(self.act_add.toolTip())
        self._add_menu = QMenu(add_btn)
        self._add_menu.addAction(self.act_add_ledger)
        self._add_menu.addAction(self.act_add_hot)
        self._add_menu.addAction(self.act_add_watch)
        self._add_menu.addSeparator()
        self._add_menu.addAction(self.act_import_brownie)
        self._add_menu.addAction(self.act_import_frame)
        add_btn.setMenu(self._add_menu)
        self._account_buttons.append(add_btn)

        # Copy / Remove become icon-only flat buttons matching the Tokens /
        # Transactions panels' utility buttons, so the two slots' bottom
        # rows read the same: one labelled primary action (Add, like Send)
        # followed by small icon-only ones. They still mirror their QActions
        # (which carry the tree's Ctrl+C / Del shortcuts and the enabled
        # state); the label moves to the tooltip since there's no text.
        for act in (self.act_copy, self.act_remove):
            btn = QPushButton()
            btn.setIcon(act.icon())
            btn.setToolTip(act.toolTip() or act.text().replace("&", ""))
            btn.setFlat(True)
            btn.setMaximumSize(28, 28)
            btn.setIconSize(QSize(16, 16))
            btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            btn.setEnabled(act.isEnabled())
            act.enabledChanged.connect(btn.setEnabled)
            btn.clicked.connect(act.trigger)
            # Keep a Python ref so the C++ widgets survive function exit.
            self._account_buttons.append(btn)

    def _show_add_account_menu(self) -> None:
        """Triggered by act_add (e.g. from the tree's right-click
        menu). Pops the same Ledger/Watch-only picker as the
        toolbar dropdown, anchored at the cursor."""
        from PySide6.QtGui import QCursor
        self._add_menu.exec(QCursor.pos())

    # --- tree population ----------------------------------------------------

    def _capture_expansion(self) -> dict:
        """Snapshot which keyed group/root rows are expanded, so a rebuild
        can preserve the user's collapse state instead of resetting it."""
        out: dict = {}
        if self._tree is None:
            return out

        def walk(item: Optional[QTreeWidgetItem]) -> None:
            if item is None:
                return
            key = item.data(0, EXPAND_KEY_ROLE)
            if key:
                out[key] = item.isExpanded()
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self._tree.topLevelItemCount()):
            walk(self._tree.topLevelItem(i))
        return out

    def _restore_expand(self, item: QTreeWidgetItem, key: str,
                        snapshot: dict) -> None:
        """Tag a collapsible row with its stable key and restore its
        expanded state (rows not seen before default to expanded)."""
        item.setData(0, EXPAND_KEY_ROLE, key)
        item.setExpanded(snapshot.get(key, True))

    def _rebuild_tree(self) -> None:
        if self._tree is None:
            return
        expanded = self._capture_expansion()
        self._tree.clear()
        ledger_accts = [a for a in self._store.accounts if a.get("source") == "ledger"]
        ledger_root = QTreeWidgetItem([f"Ledger ({len(ledger_accts)})"])
        # Reuse the add-account menu icons so the tree groups and the
        # picker stay visually consistent (hardware device / key / eye).
        ledger_root.setIcon(0, self.act_add_ledger.icon())
        # Group containers: not draggable, not drop targets (we only
        # allow re-ordering inside scheme subgroups).
        ledger_root.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self._tree.addTopLevelItem(ledger_root)
        groups: dict[str, QTreeWidgetItem] = {}
        default_item: Optional[QTreeWidgetItem] = None
        for a in ledger_accts:
            scheme = a.get("scheme", "Custom")
            grp = groups.get(scheme)
            if grp is None:
                grp = QTreeWidgetItem([scheme])
                # Scheme group: drop-enabled so children can be
                # reordered between siblings via the parent, but not
                # draggable itself.
                grp.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)
                ledger_root.addChild(grp)
                groups[scheme] = grp
            addr = a["address"]
            is_default = (
                self._store.default_account is not None
                and addr.lower() == self._store.default_account.lower()
            )
            display = f"[{addr}]" if is_default else f" {addr} "
            label_text = a.get("label") or ""
            it = QTreeWidgetItem([display])
            it.setData(0, Qt.ItemDataRole.UserRole, addr)
            if label_text:
                it.setData(0, ACCOUNT_LABEL_ROLE, label_text)
                if addr.lower() in self._ens_verified:
                    it.setToolTip(
                        0,
                        f"{label_text} — cryptographically verified")
            it.setFont(0, QFont("monospace"))
            # Address leaf: selectable + draggable, NOT a drop target
            # (so an address can't be dropped onto another address —
            # only into the gap between siblings, which Qt resolves at
            # the parent group level).
            it.setFlags(
                Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDragEnabled
            )
            grp.addChild(it)
            if is_default:
                default_item = it
        self._restore_expand(ledger_root, "ledger", expanded)
        for scheme, g in groups.items():
            self._restore_expand(g, f"ledger/{scheme}", expanded)

        hot_accts = [a for a in self._store.accounts
                      if a.get("source") == "hot"]
        hot_root = QTreeWidgetItem([f"Hot wallet ({len(hot_accts)})"])
        hot_root.setIcon(0, self.act_add_hot.icon())
        hot_root.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)
        self._tree.addTopLevelItem(hot_root)
        for a in hot_accts:
            addr = a["address"]
            is_default = (
                self._store.default_account is not None
                and addr.lower() == self._store.default_account.lower()
            )
            label_text = a.get("label") or ""
            display = f"[{addr}]" if is_default else f" {addr} "
            it = QTreeWidgetItem([display])
            it.setData(0, Qt.ItemDataRole.UserRole, addr)
            if label_text:
                it.setData(0, ACCOUNT_LABEL_ROLE, label_text)
                if addr.lower() in self._ens_verified:
                    it.setToolTip(
                        0,
                        f"{label_text} — cryptographically verified")
            it.setFont(0, QFont("monospace"))
            it.setFlags(
                Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDragEnabled
            )
            hot_root.addChild(it)
            if is_default:
                default_item = it
        self._restore_expand(hot_root, "hot", expanded)

        watch_accts = [a for a in self._store.accounts
                        if a.get("source") == "watch_only"]
        watch_root = QTreeWidgetItem([f"Watch only ({len(watch_accts)})"])
        watch_root.setIcon(0, self.act_add_watch.icon())
        # Top-level group: not draggable, not a drop target — same
        # treatment as the Ledger root.
        watch_root.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)
        self._tree.addTopLevelItem(watch_root)
        for a in watch_accts:
            addr = a["address"]
            is_default = (
                self._store.default_account is not None
                and addr.lower() == self._store.default_account.lower()
            )
            label_text = a.get("label") or ""
            display = f"[{addr}]" if is_default else f" {addr} "
            it = QTreeWidgetItem([display])
            it.setData(0, Qt.ItemDataRole.UserRole, addr)
            if label_text:
                it.setData(0, ACCOUNT_LABEL_ROLE, label_text)
                if addr.lower() in self._ens_verified:
                    it.setToolTip(
                        0,
                        f"{label_text} — cryptographically verified")
            it.setFont(0, QFont("monospace"))
            it.setFlags(
                Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDragEnabled
            )
            watch_root.addChild(it)
            if is_default:
                default_item = it
        self._restore_expand(watch_root, "watch", expanded)

        if default_item is not None:
            self._tree.setCurrentItem(default_item)

    # --- selection / action handlers ---------------------------------------

    def _on_tree_reordered(self) -> None:
        """Walk the tree top-to-bottom collecting addresses in their
        current display order, then persist that order via the Store.
        Triggered after _ReorderTree commits an internal move."""
        ordered: list[str] = []
        if self._tree is None:
            return

        def walk(item: Optional[QTreeWidgetItem]) -> None:
            if item is None:
                return
            addr = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(addr, str) and addr:
                ordered.append(addr)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self._tree.topLevelItemCount()):
            walk(self._tree.topLevelItem(i))
        self._store.reorder_accounts(ordered)

    def _on_tree_double_clicked(self, item, _column: int) -> None:
        """Double-click on an address leaf connects that account to
        the browser (sets it as the default for eth_accounts).
        No-op for group rows (they have no UserRole address) and
        for an account that's already connected."""
        addr = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(addr, str) or not addr:
            return
        current = self._store.default_account
        if current is not None and addr.lower() == current.lower():
            return
        self._set_default(addr)

    def _on_tree_enter_pressed(self, address: str) -> None:
        """Enter / Return on a focused account leaf: same as
        double-click → connect to browser."""
        current = self._store.default_account
        if current is not None and address.lower() == current.lower():
            return
        self._set_default(address)

    def _on_tree_selection(self) -> None:
        assert (self.act_copy is not None and self.act_remove is not None
                and self._details is not None)  # built before signals connect
        addrs = self.selected_addresses()
        # Copy only makes sense for a single address; Remove handles many.
        self.act_copy.setEnabled(len(addrs) == 1)
        self.act_remove.setEnabled(len(addrs) >= 1)
        if len(addrs) == 1:
            acct = next(
                (a for a in self._store.accounts if a["address"] == addrs[0]),
                None,
            )
            if acct:
                self._details.show_account(
                    acct,
                    is_default=(addrs[0] == self._store.default_account),
                )
                self.selected_address_changed.emit(addrs[0])
                return
        self._details.clear()
        self.selected_address_changed.emit(None)

    def _on_tree_context_menu(self, pos) -> None:
        """Tree right-click menu. Mirrors the Add / Copy / Remove
        button row and exposes Set-as-default — which the details
        pane below already offers, but having it on the row's right-
        click means the user doesn't have to navigate down."""
        assert (self.act_add is not None and self.act_copy is not None
                and self.act_remove is not None and self._details is not None
                and self._tree is not None)  # built before signals connect
        addrs = self.selected_addresses()
        menu = QMenu(self._tree)
        menu.addAction(self.act_add)
        if len(addrs) == 1:
            menu.addAction(self.act_copy)
            addr = addrs[0]
            default = self._store.default_account
            already_default = (
                default is not None and addr.lower() == default.lower()
            )
            if not already_default:
                act_default = menu.addAction(
                    self._details.set_default_btn.icon(), "Connect to Browser")
                act_default.triggered.connect(
                    lambda _checked=False, a=addr: self._set_default(a)
                )
        if addrs:
            menu.addSeparator()
            menu.addAction(self.act_remove)
        menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _copy_selected_address(self) -> None:
        addrs = self.selected_addresses()
        if len(addrs) != 1:
            return
        QApplication.clipboard().setText(addrs[0])
        if self.host is not None:
            self.host.status_message(f"Copied {addrs[0]} to clipboard", 3000)

    def _remove_selected_account(self) -> None:
        addrs = self.selected_addresses()
        if not addrs:
            return
        # Bucket the selection by source so the confirmation
        # message tells the user what's actually happening — a
        # Ledger removal is reversible (re-scan the device), a
        # hot-wallet removal DELETES the on-disk keystore (real
        # data loss unless backed up), watch-only just forgets an
        # address.
        addrs_lower = {a.lower() for a in addrs}
        sources = {a.get("source") for a in self._store.accounts
                    if a["address"].lower() in addrs_lower}
        if len(addrs) == 1:
            prompt = "Remove this account from the wallet?"
            detail_head = addrs[0]
        else:
            preview = "\n".join(f"  • {a}" for a in addrs[:5])
            extra = f"\n  … and {len(addrs) - 5} more" if len(addrs) > 5 else ""
            prompt = f"Remove these {len(addrs)} accounts from the wallet?"
            detail_head = f"{preview}{extra}"
        if sources == {"ledger"}:
            consequence = (
                "Keys on your Ledger are untouched; this only forgets "
                "the addresses locally. You can re-add them via Scan "
                "at any time."
            )
        elif sources == {"hot"}:
            consequence = (
                "This will DELETE the on-disk keystore. If you have "
                "no other backup of the keystore file AND your "
                "passphrase, funds at these addresses will be "
                "permanently inaccessible."
            )
        elif sources == {"watch_only"}:
            consequence = (
                "Watch-only accounts have no key — this just stops "
                "tracking the addresses. You can re-add them at any "
                "time."
            )
        else:
            consequence = (
                "Ledger and watch-only entries are reversible (re-scan "
                "or re-add), but hot-wallet entries DELETE their on-"
                "disk keystore — those addresses are unrecoverable "
                "without an external backup of the keystore + "
                "passphrase."
            )
        if not confirm(
            self._container, prompt, f"{detail_head}\n\n{consequence}",
            action="&Remove", destructive=True,
        ):
            return
        # Hot wallets carry an on-disk keystore alongside the
        # config-level account record; remove both. Ledger / watch-
        # only accounts have nothing on disk to clean up, only the
        # config record.
        from ..hot_wallet import delete_keystore
        hot_addrs = {
            a["address"].lower() for a in self._store.accounts
            if a.get("source") == "hot" and a["address"].lower() in
                {x.lower() for x in addrs}
        }
        removed = sum(1 for a in addrs if self._store.remove_account(a))
        for a in addrs:
            if a.lower() in hot_addrs:
                try:
                    delete_keystore(a)
                except Exception:
                    log.exception("hot wallet keystore cleanup failed")
        if removed:
            self._rebuild_tree()
            self.default_account_changed.emit()
            if self.host is not None:
                self.host.status_message(f"Removed {removed} account(s)", 3000)

    def _add_ledger(self) -> None:
        if self.host is None:
            return
        dlg = AddLedgerDialog(self.host.current_chain(), self._container)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        scheme = dlg.scheme_combo.currentText()
        added_addrs: list[str] = []
        for d in dlg.selected_accounts():
            if self._store.add_account({
                "address": d.address,
                "path": d.path,
                "source": "ledger",
                "scheme": scheme,
                "label": "",
            }):
                added_addrs.append(d.address)
        self._rebuild_tree()
        self.default_account_changed.emit()
        if added_addrs and self.host is not None:
            self.host.status_message(
                f"Added {len(added_addrs)} account(s)", 3000,
            )
            # Async ENS reverse-lookup for each new address —
            # mirrors the Frame-import path. The wallet ships
            # blank labels by default; verified names fill in
            # whenever ENS has one. User-edited labels are never
            # clobbered (the guard is on ``looks_autogenerated``
            # in ``_on_ens_label_resolved``).
            self._kick_ens_label_lookups(added_addrs)

    def _add_hot_wallet(self) -> None:
        """Generate a new hot-wallet private key and persist it
        as a passphrase-encrypted keystore. The passphrase is
        collected by AddHotWalletDialog; the key is generated via
        ``os.urandom(32)`` inside hot_wallet.generate_new_keystore
        so this method never has the raw bytes in memory longer
        than the encrypt() call."""
        dlg = AddHotWalletDialog(self._container)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        from ..hot_wallet import encrypt_keystore, save_keystore
        try:
            address, keystore = encrypt_keystore(
                dlg.private_key, dlg.passphrase,
            )
        except Exception as e:
            error(
                self._container, "Hot wallet error",
                f"Failed to encrypt private key:\n\n{e}",
            )
            return
        existing_addrs = {a["address"].lower()
                          for a in self._store.accounts}
        if address.lower() in existing_addrs:
            warn(
                self._container, "Hot wallet error",
                f"An account with address {address} already exists.",
            )
            return
        try:
            save_keystore(address, keystore)
        except Exception as e:
            error(
                self._container, "Hot wallet error",
                f"Failed to write keystore:\n\n{e}",
            )
            return
        if self._store.add_account({
            "address": address,
            "source": "hot",
            "label": dlg.label,
        }):
            self._rebuild_tree()
            self.default_account_changed.emit()
            if self.host is not None:
                self.host.status_message(
                    f"Hot wallet {address} created", 3000,
                )
            # The keystore + passphrase are BOTH required to recover
            # the funds. Surface that explicitly post-creation —
            # users tend to overlook the "keep backups" hint in a
            # generation dialog and only think about it later.
            from ..hot_wallet import keystore_path
            info(
                self._container, "Hot wallet created",
                f"Address: {address}\n\n"
                f"Keystore: {keystore_path(address)}\n\n"
                "Both the keystore file AND your passphrase are "
                "required to recover funds. Back up the file and "
                "remember the passphrase — qeth never sends either "
                "anywhere."
            )

    def _import_from_brownie(self) -> None:
        self._run_import(_brownie_source())

    def _import_from_frame(self) -> None:
        self._run_import(_frame_source())

    def _run_import(self, source) -> None:
        """Open a single-source import dialog, persist the results.
        Shared between the Brownie and Frame menu entries — only
        the ImportSource implementation differs."""
        from ..hot_wallet import save_keystore
        dlg = ImportHotWalletsDialog(
            source,
            existing_addresses={a["address"].lower()
                                 for a in self._store.accounts},
            parent=self._container,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        imported, failed = [], []
        for result in dlg.imported:
            addr = result["address"]
            try:
                save_keystore(addr, result["keystore"])
            except Exception as e:
                failed.append((addr, str(e)))
                continue
            added = self._store.add_account({
                "address": addr,
                "source": "hot",
                "label": result["label"],
            })
            if added:
                imported.append(addr)
        if imported:
            self._rebuild_tree()
            self.default_account_changed.emit()
            if self.host is not None:
                self.host.status_message(
                    f"Imported {len(imported)} hot wallet"
                    f"{'s' if len(imported) != 1 else ''}"
                    f" from {source.name}",
                    4000,
                )
            # Sources whose label isn't a real name (Frame's hex
            # IDs) get an ENS reverse lookup. The worker fires
            # async; ``_on_ens_label_resolved`` overwrites the
            # account's label when a verified name comes back.
            if not source.label_is_user_meaningful:
                self._kick_ens_label_lookups(imported)
        if failed:
            lines = "\n".join(f"  • {a}: {m}" for a, m in failed)
            warn(
                self._container, "Import partly failed",
                f"Imported {len(imported)} account(s). "
                f"{len(failed)} failed:\n\n{lines}",
            )

    def _kick_ens_label_lookups(self, addresses: list[str]) -> None:
        """One ENS reverse-lookup worker per just-added address.
        Mainnet only — that's where ENS lives even when the user
        is browsing other chains. Quietly drops anything that
        doesn't resolve or fails to verify (forward-lookup
        mismatch). Used after both Ledger scans and Frame imports
        — same flow either way; the labels we'd otherwise leave
        blank or set to a useless hex blob get filled with a
        verified name when one exists."""
        from ..chains import DEFAULT_CHAINS
        from ..ens import EnsReverseWorker
        mainnet = next(
            (c for c in DEFAULT_CHAINS if c.chain_id == 1), None,
        )
        if mainnet is None:
            return
        for addr in addresses:
            # wait_s=0.0: verify only when a mainnet Helios sidecar is already
            # synced; never block a label on a cold sync (it's informational).
            w = EnsReverseWorker(mainnet, addr, wait_s=0.0)
            w.resolved.connect(self._on_ens_label_resolved)
            self._ens_workers.append(w)
            w.finished.connect(
                lambda x=w: self._ens_workers.remove(x)
                if x in self._ens_workers else None
            )
            w.finished.connect(w.deleteLater)
            w.start()

    def _on_ens_label_resolved(
        self, address: str, name: str, verified: bool = False,
    ) -> None:
        """ENS worker came back. Overwrite the account's label IFF
        a real (forward-verified) name resolved AND the current
        label still looks auto-generated. We never clobber a
        user-typed label — only fill in empties and Frame's
        ``deadbeef…`` / ``deadbeef… (key N/M)`` placeholders.

        ``verified`` (resolved through Helios) is recorded so the tree row
        gets a proof-verified tooltip."""
        if not name:
            return
        addr_lower = address.lower()
        if verified:
            self._ens_verified.add(addr_lower)
        for acct in self._store.accounts:
            if acct["address"].lower() != addr_lower:
                continue
            current = acct.get("label") or ""
            looks_autogenerated = (
                "…" in current   # Frame label has an ellipsis
                or not current
            )
            if looks_autogenerated:
                self._store.set_label(acct["address"], name)
                self._rebuild_tree()
            return

    def _add_watch_only(self) -> None:
        """Open the small "Add watch-only address" modal and
        persist the result. Watch-only accounts have no key — they
        appear in the wallet tree (under "Watch only") so balances
        and tx history are visible, but Send / Connect-to-browser
        fail with "No known signer" for them (and the actions are
        disabled when one is selected)."""
        existing = {a["address"] for a in self._store.accounts}
        dlg = AddWatchOnlyDialog(existing, self._container)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if self._store.add_account(dlg.result_account()):
            self._rebuild_tree()
            self.default_account_changed.emit()
            if self.host is not None:
                self.host.status_message("Watch-only address added", 3000)

    def _on_label_changed(self, address: str, label: str) -> None:
        """The details panel reported a label edit. Persist via
        the store, then rebuild the tree so the new label appears
        next to the address everywhere."""
        if self._store.set_label(address, label):
            self._rebuild_tree()
            if self.host is not None:
                self.host.status_message(
                    f"Updated label for {address}", 2500,
                )

    def _set_default(self, address: str) -> None:
        self._store.set_default_account(address)
        self._rebuild_tree()
        self.default_account_changed.emit()
        # Re-run selection to refresh the details-panel button state.
        self._on_tree_selection()

    def _on_sign_message(self, address: str) -> None:
        """Forward to host. ``MainWindow.open_sign_message_dialog``
        runs the compose + review + signing flow."""
        if self.host is None:
            return
        opener = getattr(self.host, "open_sign_message_dialog", None)
        if callable(opener):
            opener(address)


# --- DetailsPanel + AddLedgerDialog (moved from qeth.ui) -------------------

class DetailsPanel(QWidget):
    """The right-hand details for the selected account. Title is
    editable inline — typing into it and committing (Enter or
    focus-out) emits ``label_changed(address, label)`` so the
    plugin can persist it via the store.
    """

    # User edited the title field. Carries (address, new_label).
    # The plugin pipes this to Store.set_label + tree rebuild.
    label_changed = Signal(str, str)

    set_default_requested = Signal(str)
    # Emitted when the user clicks "Sign message…" on this account.
    # The plugin forwards it to the host (MainWindow) which opens
    # the ComposeMessageDialog → SignMessageDialog flow.
    sign_message_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        # Inner bottom margin stays 0: the framed wrapper (details_wrap)
        # owns the gap between the buttons and its bottom border, so the
        # two margins don't stack into an oversized void below the buttons.
        v.setContentsMargins(9, 9, 9, 0)
        # Header placeholder shown when no account is selected.
        # We hide it (and show the form) once show_account runs.
        self.placeholder_lbl = QLabel("Select an account on the left")
        v.addWidget(self.placeholder_lbl)

        form = QFormLayout()
        # Label field — same form treatment as the rest of the
        # rows. Frameless until focus so the read state looks like
        # a value rather than an empty input box, but the user can
        # still click into it. editingFinished fires on Enter or
        # focus-out.
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("(no label)")
        self.label_edit.setFrame(False)
        self.label_edit.setEnabled(False)
        self.label_edit.editingFinished.connect(self._on_label_committed)
        form.addRow("Label:", self.label_edit)
        mono = QFont("monospace")
        # Ignored size policy on the long monospace labels: their sizeHint
        # (full 42-char address etc.) shouldn't pin the panel's minimum
        # width, otherwise the whole window can't be shrunk down.
        self.address_lbl = QLabel("—"); self.address_lbl.setFont(mono)
        self.address_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.address_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.path_lbl = QLabel("—"); self.path_lbl.setFont(mono)
        self.path_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.source_lbl = QLabel("—")
        self.scheme_lbl = QLabel("—")
        form.addRow("Address:", self.address_lbl)
        form.addRow("Path:", self.path_lbl)
        form.addRow("Source:", self.source_lbl)
        form.addRow("Scheme:", self.scheme_lbl)
        v.addLayout(form)

        # Vertical breathing room above + below the QR — without these,
        # the form rows / button crowd right up against it and the panel
        # looks squeezed.
        v.addSpacing(12)
        self.qr_lbl = QLabel()
        self.qr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_lbl.setFixedSize(220, 220)
        v.addWidget(self.qr_lbl, 0, Qt.AlignmentFlag.AlignCenter)
        v.addSpacing(12)

        # Short label + tooltip rather than a wide button — keeps the
        # panel narrow-shrinkable. Same policy trick on the button itself.
        # "Connect to browser" makes the action concrete: this is the
        # address dapps will see via the local JSON-RPC server (Frame
        # interface), nothing about persistence or "default".
        self.set_default_btn = QPushButton("Connect to &Browser")
        # Globe / browser icon for "make this address visible to the
        # web". Same icon the Transactions list uses for "Open in
        # block explorer" — reuse keeps the "exposed to the web"
        # association consistent across the app.
        _conn_icon = QIcon.fromTheme(
            "applications-internet",
            QIcon.fromTheme(
                "internet-web-browser",
                QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DesktopIcon),
            ),
        )
        if not _conn_icon.isNull() and _conn_icon.availableSizes():
            self.set_default_btn.setIcon(_conn_icon)
        self.set_default_btn.setToolTip("Make default for dapps")
        self.set_default_btn.setEnabled(False)
        # Pin the height. With QSizePolicy.Policy.Fixed Qt re-queries sizeHint()
        # every time the text changes — and "Connected ✓" can come out
        # a touch shorter than "Connect to browser" depending on the
        # theme. The snapshot here is taken while the (longer) text is
        # set, so the disabled state never shrinks.
        self.set_default_btn.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_default_btn.setMinimumHeight(self.set_default_btn.sizeHint().height())
        self.set_default_btn.clicked.connect(
            lambda: (self.set_default_requested.emit(self._current)
                     if self._current else None)
        )

        # "Sign message…" — opens the compose dialog for the
        # currently-shown account. Disabled when nothing's
        # selected or when the account has no signer (watch-only).
        self.sign_message_btn = QPushButton("Sign &Message…")
        _sig_icon = QIcon.fromTheme(
            "document-edit",
            QIcon.fromTheme(
                "edit-paste",
                QApplication.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView),
            ),
        )
        if not _sig_icon.isNull() and _sig_icon.availableSizes():
            self.sign_message_btn.setIcon(_sig_icon)
        self.sign_message_btn.setToolTip("Sign a message")
        self.sign_message_btn.setEnabled(False)
        self.sign_message_btn.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred,
        )
        self.sign_message_btn.setMinimumHeight(
            self.sign_message_btn.sizeHint().height()
        )
        self.sign_message_btn.clicked.connect(
            lambda: (self.sign_message_requested.emit(self._current)
                     if self._current else None)
        )

        # Stretch pushes the buttons to the very bottom of the panel.
        v.addStretch(1)
        v.addWidget(self.sign_message_btn)
        v.addWidget(self.set_default_btn)
        self._current: str | None = None
        # The label we last loaded into the title field; used by
        # _on_title_committed to detect actual user edits vs the
        # user focusing in/out without typing.
        self._loaded_label: str = ""

    def show_account(self, account: dict, is_default: bool) -> None:
        self._current = account["address"]
        # Suppress the editingFinished signal we'd otherwise emit
        # from setText — only programmatic loads, not user edits.
        self.label_edit.blockSignals(True)
        self.label_edit.setText(account.get("label") or "")
        self.label_edit.setEnabled(True)
        self.label_edit.blockSignals(False)
        # Track the value we just loaded so _on_label_committed can
        # tell whether the user actually changed anything.
        self._loaded_label = account.get("label") or ""
        self.placeholder_lbl.setVisible(False)
        self.address_lbl.setText(account["address"])
        self.path_lbl.setText(account.get("path", "—"))
        self.source_lbl.setText(account.get("source", "—"))
        self.scheme_lbl.setText(account.get("scheme", "—"))
        # Watch-only accounts have no key behind them — connecting
        # one to the browser would just lead to "No known signer"
        # popups the moment the dapp tries to sign. Disable the
        # button + flip the tooltip to explain.
        is_watch_only = account.get("source") == "watch_only"
        if is_watch_only:
            self.set_default_btn.setEnabled(False)
            self.set_default_btn.setText("Watch-only — Read-only")
            self.set_default_btn.setToolTip("Watch-only — can't sign")
        else:
            self.set_default_btn.setEnabled(not is_default)
            self.set_default_btn.setText(
                "Connected to Browser ✓" if is_default
                else "Connect to &Browser"
            )
            self.set_default_btn.setToolTip("Make default for dapps")
        # Same key-bearing-account check as Connect-to-browser:
        # watch-only can't sign messages.
        self.sign_message_btn.setEnabled(not is_watch_only)
        self._render_qr(account["address"])

    def _render_qr(self, address: str) -> None:
        buf = io.BytesIO()
        # ethereum: URI per EIP-681 so wallets recognize it as a send intent
        segno.make(f"ethereum:{address}", error="m").save(buf, kind="png", scale=6, border=2)
        pix = QPixmap()
        pix.loadFromData(buf.getvalue())  # format auto-detected from the PNG header
        self.qr_lbl.setPixmap(pix.scaled(
            self.qr_lbl.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation
        ))

    def clear(self) -> None:
        self._current = None
        self._loaded_label = ""
        self.label_edit.blockSignals(True)
        self.label_edit.setText("")
        self.label_edit.setEnabled(False)
        self.label_edit.blockSignals(False)
        self.placeholder_lbl.setVisible(True)
        for w in (self.address_lbl, self.path_lbl, self.source_lbl, self.scheme_lbl):
            w.setText("—")
        self.qr_lbl.clear()
        self.set_default_btn.setEnabled(False)
        self.set_default_btn.setText("Connect to &Browser")
        self.sign_message_btn.setEnabled(False)

    def _on_label_committed(self) -> None:
        """Label editingFinished: emit ``label_changed`` so the
        plugin persists the new label. Guards against firing for
        no-op edits (the user clicked into the field and back out
        without typing) and against firing when no account is
        currently shown."""
        if self._current is None:
            return
        new = self.label_edit.text().strip()
        if new == self._loaded_label:
            return
        # Update the cached value so subsequent focus-out events
        # in the same session don't re-fire.
        self._loaded_label = new
        self.label_changed.emit(self._current, new)


# --- Token list panel -------------------------------------------------------
#
# The six token-related QThread workers (TokenListsLoader, TokenListWorker,
# RiskWorker, MetadataWorker, BalanceWorker, PricesWorker) used to live
# here. They moved into qeth.plugins.tokens as part of the plugin refactor
# (step 3) — they're token-domain code, owned by TokensPlugin now.




class AddLedgerDialog(QDialog):
    def __init__(self, chain, parent=None):
        super().__init__(parent)
        self._chain = chain
        self.setWindowTitle("Add Ledger Accounts")
        self.setMinimumSize(640, 460)

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.scheme_combo = QComboBox()
        self.scheme_combo.addItems(list(PATH_SCHEMES.keys()))
        form.addRow("Derivation scheme:", self.scheme_combo)
        self.count_spin = QSpinBox()
        self.count_spin.setRange(0, 100)
        self.count_spin.setValue(0)
        self.count_spin.setSpecialValueText("Auto-detect (stop after 3 empty)")
        form.addRow("Accounts to scan:", self.count_spin)
        layout.addLayout(form)

        self.results = QListWidget()
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        font = QFont("monospace")
        self.results.setFont(font)
        # ENS names appended after the address can push rows past
        # the dialog width — middle-elide + no horizontal bar,
        # same treatment as the wallet tree and import dialog.
        self.results.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.results, 1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        btns = QHBoxLayout()
        style_proxy = QApplication.style()
        self.scan_btn = QPushButton("&Scan")
        # Magnifier icon reads as "look for accounts on the device".
        _scan_icon = QIcon.fromTheme(
            "system-search",
            QIcon.fromTheme(
                "edit-find",
                style_proxy.standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView),
            ),
        )
        if not _scan_icon.isNull() and _scan_icon.availableSizes():
            self.scan_btn.setIcon(_scan_icon)
        self.add_btn = QPushButton("&Add Selected")
        # Same "+" icon the toolbar Add buttons use across the app —
        # keeps the meaning stable wherever the user sees it.
        _add_icon = QIcon.fromTheme(
            "list-add",
            style_proxy.standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder),
        )
        if not _add_icon.isNull() and _add_icon.availableSizes():
            self.add_btn.setIcon(_add_icon)
        self.add_btn.setEnabled(False)
        # Close stays text-only — Qt's QDialogButtonBox-derived
        # dialogs across the app render Cancel/Close without
        # icons, and adding one here would be inconsistent.
        self.close_btn = QPushButton("&Close")
        btns.addWidget(self.scan_btn)
        btns.addStretch(1)
        btns.addWidget(self.add_btn)
        btns.addWidget(self.close_btn)
        layout.addLayout(btns)

        self.scan_btn.clicked.connect(self._scan)
        self.add_btn.clicked.connect(self.accept)
        self.close_btn.clicked.connect(self.reject)
        self.results.itemSelectionChanged.connect(
            lambda: self.add_btn.setEnabled(bool(self.results.selectedItems()))
        )

        # Workers tracked here so they aren't garbage-collected while still
        # running (Qt's QThread destructor aborts the process if it is).
        self._workers: set[QThread] = set()
        # Separate bucket for ENS lookups (one per scanned address);
        # they're independent of the discovery worker lifecycle, so
        # ``_scan`` clearing ``_workers`` shouldn't touch them.
        self._ens_workers: set[QThread] = set()

    def _scan(self) -> None:
        self.results.clear()
        self.add_btn.setEnabled(False)
        n = self.count_spin.value()
        if n == 0:
            self.progress.setRange(0, 0)  # indeterminate spinner
        else:
            self.progress.setRange(0, n)
            self.progress.setValue(0)
        self.progress.setVisible(True)
        self.scan_btn.setEnabled(False)
        worker = LedgerWorker(
            self.scheme_combo.currentText(), n, chain=self._chain
        )
        worker.discovered.connect(self._on_found)
        worker.finished_ok.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        self._workers.add(worker)
        worker.finished.connect(lambda w=worker: self._workers.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_found(self, acct: DiscoveredAccount) -> None:
        # Annotate each row with its sent-tx count: 0 = never used
        # from this address, >0 = active wallet. Pre-select the
        # active ones so they're auto-added on Confirm; the user can
        # still tick / untick to override.
        if acct.nonce == 0:
            usage = "unused"
        elif acct.nonce == 1:
            usage = "1 tx"
        else:
            usage = f"{acct.nonce} txs"
        label = f"#{acct.index:<3} {acct.address}   {usage}"
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, acct)
        item.setSelected(acct.nonce > 0)
        self.results.addItem(item)
        if self.progress.maximum() > 0:
            self.progress.setValue(self.progress.value() + 1)
        # Async ENS reverse-lookup so we can show ``ens.eth``
        # alongside the address in the scan list — helps the user
        # tell their "swiss-stake.eth" account from a fresh one
        # before they tick which to add. Quietly drops if nothing
        # resolves.
        self._kick_ens_for_row(item, acct.address)

    def _kick_ens_for_row(self, item, address: str) -> None:
        from ..chains import DEFAULT_CHAINS
        from ..ens import EnsReverseWorker
        mainnet = next(
            (c for c in DEFAULT_CHAINS if c.chain_id == 1), None,
        )
        if mainnet is None:
            return
        w = EnsReverseWorker(mainnet, address, wait_s=0.0)
        w.resolved.connect(
            lambda addr, name, verified, _it=item: self._annotate_row_with_ens(
                _it, addr, name, verified,
            )
        )
        self._ens_workers.add(w)
        w.finished.connect(lambda x=w: self._ens_workers.discard(x))
        w.finished.connect(w.deleteLater)
        w.start()

    def _annotate_row_with_ens(
        self, item, address: str, name: str, verified: bool = False,
    ) -> None:
        """Append the verified ENS name to the row text. The
        DiscoveredAccount in UserRole is unchanged — selection +
        the eventual add_account call still see the bare address;
        the label gets re-resolved through the same ENS path
        after Add (see ``WalletsPlugin._kick_ens_label_lookups``).

        ``verified`` (resolved through Helios) shows a ✓ and a tooltip."""
        if not name:
            return
        # Defensive: confirm the item still belongs to this row's
        # address (the user could have re-scanned mid-resolve).
        acct = item.data(Qt.ItemDataRole.UserRole)
        if acct is None or acct.address.lower() != address.lower():
            return
        mark = " ✓" if verified else ""
        item.setText(f"{item.text()}   ({name}{mark})")
        if verified:
            item.setToolTip(
                f"{name} — cryptographically verified")

    def _on_done(self) -> None:
        self.progress.setVisible(False)
        self.scan_btn.setEnabled(True)
        self.add_btn.setEnabled(bool(self.results.selectedItems()))

    def _on_failed(self, msg: str) -> None:
        self.progress.setVisible(False)
        self.scan_btn.setEnabled(True)
        error(self, "Ledger error", msg)

    def selected_accounts(self) -> list[DiscoveredAccount]:
        return [it.data(Qt.ItemDataRole.UserRole) for it in self.results.selectedItems()]


class AddWatchOnlyDialog(QDialog):
    """Small modal for adding a watch-only address. No signing
    capability — these accounts can be selected, view balances and
    transaction history, but Send / Connect-to-browser stay disabled
    because there's no key behind them.

    Used for tracking other people's wallets (treasury, vesting
    contracts, friends' addresses) or your own cold-storage addresses
    without exposing the device for read-only views."""

    def __init__(self, existing_addresses: set[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Watch-only Address")
        # Lower-case set of addresses already in the store, used for
        # the duplicate check on accept.
        self._existing = {a.lower() for a in existing_addresses}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)

        form = QFormLayout()
        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText(
            "0x… address or ENS name (e.g. vitalik.eth)")
        self.address_edit.setFont(QFont("monospace"))
        form.addRow("&Address:", self.address_edit)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("e.g. Cold storage, Treasury")
        form.addRow("&Label (optional):", self.label_edit)
        layout.addLayout(form)

        # ENS forward-resolution status: "resolving…" → "→ 0x…" / "not found".
        self.resolved_lbl = QLabel("")
        self.resolved_lbl.setVisible(False)
        layout.addWidget(self.resolved_lbl)

        self.error_lbl = QLabel("")
        self.error_lbl.setStyleSheet("color: palette(highlighted-text);")
        self.error_lbl.setVisible(False)
        layout.addWidget(self.error_lbl)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel,
        )
        self.add_btn = buttons.addButton(
            "Add", QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.add_btn.setEnabled(False)
        buttons.rejected.connect(self.reject)
        self.add_btn.clicked.connect(self._on_accept)
        layout.addWidget(buttons)

        self.address_edit.textChanged.connect(self._on_address_changed)
        # Debounce ENS lookups until 300 ms after the last keystroke
        # so we don't fire one per typed character.
        self._ens_timer = QTimer(self)
        self._ens_timer.setSingleShot(True)
        self._ens_timer.setInterval(300)
        self._ens_timer.timeout.connect(self._kick_ens_lookup)
        # Track in-flight workers so they survive Python GC.
        self._ens_workers: list[QThread] = []
        # Set when the entered ENS name forward-resolves to an address; used
        # in place of the raw text on accept.
        self._ens_forward_addr: Optional[str] = None

    def _on_address_changed(self) -> None:
        """Accept either a 0x address or an ENS name as the user types.
        A 42-char 0x hex enables Add immediately (and kicks a *reverse*
        lookup to pre-fill the Label); a name like ``vitalik.eth`` is
        *forward*-resolved to an address, with Add disabled until it
        resolves. Both run debounced against Ethereum mainnet. Full
        checksum normalisation happens on accept (lower-case paste OK)."""
        text = self.address_edit.text().strip()
        self._ens_forward_addr = None
        is_addr = text.startswith("0x") and len(text) == 42
        if is_addr:
            try:
                int(text, 16)
            except ValueError:
                is_addr = False
        is_name = "." in text and not text.startswith("0x") and len(text) >= 5
        if is_addr:
            self.add_btn.setEnabled(True)
            self._set_resolved(None)
            self._ens_timer.start()           # reverse → Label
        elif is_name:
            self.add_btn.setEnabled(False)    # until it resolves
            self._set_resolved("resolving ENS…")
            self._ens_timer.start()           # forward → address
        else:
            self.add_btn.setEnabled(False)
            self._set_resolved(None)
            self._ens_timer.stop()
        if not text:
            self.error_lbl.setVisible(False)

    def _set_resolved(self, text: Optional[str]) -> None:
        self.resolved_lbl.setText(text or "")
        self.resolved_lbl.setVisible(bool(text))
        # Reset to neutral; _on_ens_forward re-applies the verified pill. Stops
        # a prior "✓ verified" green from lingering over a "resolving…" message.
        self.resolved_lbl.setStyleSheet("")
        self.resolved_lbl.setToolTip("")

    def _kick_ens_lookup(self) -> None:
        """Debounced ENS query against Ethereum mainnet (ENS lives on
        chain 1 regardless of the viewing chain). A 0x address →
        reverse-resolve for the Label; a name → forward-resolve to an
        address."""
        text = self.address_edit.text().strip()
        from ..chains import DEFAULT_CHAINS
        from ..ens import EnsResolveWorker, EnsReverseWorker
        mainnet = next(
            (c for c in DEFAULT_CHAINS if c.chain_id == 1), None,
        )
        if mainnet is None:
            return
        if text.startswith("0x") and len(text) == 42:
            rev = EnsReverseWorker(mainnet, text)
            rev.resolved.connect(self._on_ens_reverse)
            worker: QThread = rev
        elif "." in text:
            fwd = EnsResolveWorker(mainnet, text)
            fwd.resolved.connect(self._on_ens_forward)
            worker = fwd
        else:
            return
        self._ens_workers.append(worker)
        worker.finished.connect(
            lambda w=worker: self._ens_workers.remove(w)
            if w in self._ens_workers else None
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_ens_reverse(self, address: str, name: str) -> None:
        """Reverse result: drop the verified ENS name into an empty
        Label, only when the user is still on the same address (they
        may have edited further while the lookup was in flight)."""
        if self.address_edit.text().strip().lower() != address.lower():
            return
        if self.label_edit.text().strip():
            return
        if name:
            self.label_edit.setText(name)

    def _on_ens_forward(
        self, name: str, address: str, verified: bool = False,
    ) -> None:
        """Forward result: a name → address. Show the resolved address,
        enable Add, and offer the name as the default Label. Ignored if
        the user has since edited the field. ``verified`` (resolved through
        Helios) badges the address green ✓, same meaning as the Send dialog."""
        if self.address_edit.text().strip().lower() != name.lower():
            return
        if address:
            self._ens_forward_addr = address
            if verified:
                self._set_resolved(f"→ {address}  ✓ verified")
                self.resolved_lbl.setStyleSheet(
                    "background:#d1e7dd; color:#0f5132; padding:1px 6px;"
                    " border-radius:4px;")
                self.resolved_lbl.setToolTip("Cryptographically verified")
            else:
                self._set_resolved(f"→ {address}")
                self.resolved_lbl.setStyleSheet("")
                self.resolved_lbl.setToolTip("Unverified (no Helios)")
            self.add_btn.setEnabled(True)
            if not self.label_edit.text().strip():
                self.label_edit.setText(name)
        else:
            self._ens_forward_addr = None
            self._set_resolved("ENS name not found")
            self.add_btn.setEnabled(False)

    def _on_accept(self) -> None:
        from eth_utils import to_checksum_address
        # An ENS name that forward-resolved → use the resolved address.
        text = self._ens_forward_addr or self.address_edit.text().strip()
        try:
            checksum = to_checksum_address(text)
        except Exception as e:
            self.error_lbl.setText(f"Invalid address: {e}")
            self.error_lbl.setVisible(True)
            return
        if checksum.lower() in self._existing:
            self.error_lbl.setText(
                "That address is already in the wallet."
            )
            self.error_lbl.setVisible(True)
            return
        self._checksum = checksum
        self._label = self.label_edit.text().strip()
        self.accept()

    def result_account(self) -> dict:
        """The new account dict, ready to hand to Store.add_account.
        Call only after the dialog returned Accepted."""
        return {
            "address": self._checksum,
            "source": "watch_only",
            "label": self._label,
        }


class AddHotWalletDialog(QDialog):
    """Generate a new hot wallet: pick a passphrase, get a fresh
    random key encrypted with it. The keystore file lands in
    ``~/.qeth/keystores/`` and the passphrase IS the only key —
    nobody (including qeth) can recover the funds without both."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Hot Wallet")
        # Width is what matters; let Qt auto-size the height to
        # whatever the form needs once each field has a generous
        # min-height — no trailing stretch means no empty space
        # below the form.
        self.resize(560, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(14)

        warn = QLabel(
            "qeth will encrypt the private key under your passphrase "
            "and write the keystore to disk. Both the keystore file "
            "AND your passphrase are required to recover funds — "
            "back up the file and remember the passphrase. Lose "
            "either and the funds are gone."
        )
        warn.setWordWrap(True)
        layout.addWidget(warn)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        # Roomy vertical rhythm between rows; combines with the
        # per-field minimum height below to keep each input from
        # collapsing into a thin strip.
        form.setVerticalSpacing(14)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        # Stretch the fields to fill the dialog width rather than
        # the default narrow column.
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        # Common minimum height for all the input widgets — Qt's
        # default QLineEdit height (~24 px on some themes) reads
        # as "squeezed" next to a sensibly-padded button.
        _input_min_h = 30

        # Private-key row: text field + dice button that generates
        # a random 32-byte key. User can also paste their own.
        # The QWidget wrapper's minimumSizeHint doesn't always
        # propagate the inner QLineEdit's minimumHeight on every
        # Qt style; pin it explicitly so this row gets the same
        # vertical breathing room as the QLineEdits below.
        pk_row = QWidget()
        pk_row.setMinimumHeight(_input_min_h)
        pk_layout = QHBoxLayout(pk_row)
        pk_layout.setContentsMargins(0, 0, 0, 0)
        pk_layout.setSpacing(6)
        self.pk_edit = QLineEdit()
        self.pk_edit.setPlaceholderText(
            "64 hex chars (with or without 0x prefix)"
        )
        self.pk_edit.setFont(QFont("monospace"))
        self.pk_edit.setMinimumHeight(_input_min_h)
        pk_layout.addWidget(self.pk_edit, 1)
        self.dice_btn = QToolButton()
        self.dice_btn.setText("🎲")
        self.dice_btn.setMinimumHeight(_input_min_h)
        self.dice_btn.setToolTip("New random key")
        self.dice_btn.clicked.connect(self._on_dice_clicked)
        pk_layout.addWidget(self.dice_btn)
        form.addRow("Pri&vate key:", pk_row)

        self.pass1_edit = QLineEdit()
        self.pass1_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass1_edit.setPlaceholderText("Passphrase")
        self.pass1_edit.setMinimumHeight(_input_min_h)
        form.addRow("&Passphrase:", self.pass1_edit)

        self.pass2_edit = QLineEdit()
        self.pass2_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass2_edit.setPlaceholderText("Repeat passphrase")
        self.pass2_edit.setMinimumHeight(_input_min_h)
        form.addRow("&Confirm:", self.pass2_edit)

        # Live length / match indicator inline with the form so the
        # user can see at a glance whether their passphrase is past
        # the 8-char minimum, and whether the two fields agree —
        # the global match_lbl below was easy to overlook on the
        # way from the Confirm field to the Add button.
        # Reserve room for descenders ('p', 'q' on "passphrases…")
        # — without an explicit minimum height the form-row cell
        # sized to the empty label's near-zero hint, and the bold
        # text ended up clipped at the bottom on first display.
        self.pass_status_lbl = QLabel("")
        # Use the bold font's height (plus a couple of px) to reserve
        # the row — the rich-text spans we'll set are bold.
        from PySide6.QtGui import QFontMetrics
        bold_font = self.pass_status_lbl.font()
        bold_font.setBold(True)
        self.pass_status_lbl.setMinimumHeight(
            QFontMetrics(bold_font).height() + 4
        )
        form.addRow("", self.pass_status_lbl)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("e.g. Daily driver")
        self.label_edit.setMinimumHeight(_input_min_h)
        form.addRow("&Label (optional):", self.label_edit)
        layout.addLayout(form)

        # Theme-aware error / ok colours used by both the inline
        # passphrase progress and the bottom match_lbl.
        self._err_color = _palette_aware_error_color(self.palette())
        self._ok_color = _palette_aware_ok_color(self.palette())

        # Inline status / rejection hint for the private-key field.
        # Styled with the palette-aware error red so it actually
        # reads as a warning on both light and dark themes — the
        # previous ``palette(highlight)`` came out as light cyan on
        # some palettes and was effectively invisible.
        self.match_lbl = QLabel("")
        self.match_lbl.setWordWrap(True)
        self.match_lbl.setStyleSheet(
            f"color: {self._err_color}; font-weight: bold;"
        )
        layout.addWidget(self.match_lbl)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.gen_btn = buttons.addButton(
            "Add", QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.gen_btn.setEnabled(False)
        buttons.rejected.connect(self.reject)
        self.gen_btn.clicked.connect(self._on_accept)
        layout.addWidget(buttons)

        self.pk_edit.textChanged.connect(self._update_state)
        self.pass1_edit.textChanged.connect(self._update_state)
        self.pass2_edit.textChanged.connect(self._update_state)

    def _on_dice_clicked(self) -> None:
        """Fill the private-key field with a fresh 32-byte
        cryptographically-random value. The user can still edit
        afterwards — we use whatever's in the field on accept."""
        from ..hot_wallet import generate_random_private_key
        self.pk_edit.setText(generate_random_private_key().hex())

    def _parsed_private_key(self) -> Optional[bytes]:
        """Returns the parsed 32 bytes if the field holds a valid
        hex private key, else None. Used by _update_state to gate
        the Add button; ``_on_accept`` re-parses (with the same
        helper) so a single source of truth catches all the edge
        cases."""
        from ..hot_wallet import parse_private_key_hex
        try:
            return parse_private_key_hex(self.pk_edit.text())
        except Exception:
            return None

    def _update_state(self) -> None:
        pk_text = self.pk_edit.text()
        pk_valid = self._parsed_private_key() is not None
        p1 = self.pass1_edit.text()
        p2 = self.pass2_edit.text()

        # Inline passphrase progress always shows while the user is
        # typing, so it's visible right next to the field they just
        # touched. Green when ≥ 8 chars AND fields match; otherwise
        # red with a count.
        if not p1 and not p2:
            self.pass_status_lbl.setText("")
        elif len(p1) < 8:
            self.pass_status_lbl.setText(
                f"<span style='color: {self._err_color};"
                f" font-weight: bold;'>"
                f"{len(p1)}/8 characters</span>"
            )
        elif p1 != p2:
            self.pass_status_lbl.setText(
                f"<span style='color: {self._err_color};"
                f" font-weight: bold;'>"
                f"passphrases don't match</span>"
            )
        else:
            self.pass_status_lbl.setText(
                f"<span style='color: {self._ok_color};"
                f" font-weight: bold;'>✓ ok</span>"
            )

        if not pk_text.strip():
            self.match_lbl.setText("")
            self.gen_btn.setEnabled(False)
            return
        if not pk_valid:
            self.match_lbl.setText(
                "Private key must be 64 hex characters (with or "
                "without 0x prefix)."
            )
            self.gen_btn.setEnabled(False)
            return
        if not p1:
            self.match_lbl.setText("")
            self.gen_btn.setEnabled(False)
            return
        if p1 != p2:
            self.match_lbl.setText("")
            self.gen_btn.setEnabled(False)
            return
        if len(p1) < 8:
            self.match_lbl.setText("")
            self.gen_btn.setEnabled(False)
            return
        self.match_lbl.setText("")
        self.gen_btn.setEnabled(True)

    def _on_accept(self) -> None:
        priv = self._parsed_private_key()
        if priv is None:
            return  # belt + braces; Add button is gated already
        self.private_key = priv
        self.passphrase = self.pass1_edit.text()
        self.label = self.label_edit.text().strip()
        self.accept()


def _brownie_source():
    """Lazy factory — keeps the import-sources module out of the
    eager-load path for users who never open the importer."""
    from ..import_sources import BrownieSource
    return BrownieSource()


def _frame_source():
    from ..import_sources import FrameSource
    return FrameSource()


class ImportHotWalletsDialog(QDialog):
    """Single-source hot-wallet importer.

    One dialog per source (Brownie or Frame), opened from its own
    menu entry. The dialog wraps a single ``_ImportSourcePanel``
    — kept as its own widget so we could reuse it elsewhere if we
    later add more sources, but the user-facing flow is one
    dialog == one source.

    Sets ``self.imported`` to ``[{"address", "label", "keystore"},
    …]`` before accepting; the host calls ``save_keystore`` and
    registers the account records."""

    def __init__(self, source, existing_addresses: set[str], parent=None):
        super().__init__(parent)
        self._source = source
        self.setWindowTitle(f"Import from {source.name}")
        self.setMinimumSize(720, 480)
        self._existing = {a.lower() for a in existing_addresses}
        self.imported: list[dict] = []

        layout = QVBoxLayout(self)
        if source.needs_source_passphrase:
            intro_text = (
                f"Import accounts from {source.name}. Each selected "
                f"key is decrypted with your {source.name} passphrase "
                "and re-encrypted under a new qeth passphrase. The "
                "original wallet stays untouched."
            )
        else:
            intro_text = (
                f"Import accounts from {source.name}. Keystores are "
                "copied as-is — your existing passphrase is preserved "
                "and you'll use it to sign in qeth too. The source "
                "directory stays untouched."
            )
        intro = QLabel(intro_text)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.panel = _ImportSourcePanel(source, self._existing, parent=self)
        layout.addWidget(self.panel, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self.import_btn = QPushButton("&Import Selected")
        self.import_btn.setEnabled(False)
        self.import_btn.clicked.connect(self._do_import)
        self.close_btn = QPushButton("&Close")
        self.close_btn.clicked.connect(self.reject)
        btns.addWidget(self.import_btn)
        btns.addWidget(self.close_btn)
        layout.addLayout(btns)

        self.panel.state_changed.connect(self._refresh_import_btn)
        self.panel.refresh()

    def _refresh_import_btn(self) -> None:
        self.import_btn.setEnabled(self.panel.ready_to_import())

    def _do_import(self) -> None:
        try:
            results = self.panel.do_import()
        except Exception as e:
            error(
                self, "Import failed",
                f"Could not import:\n\n{e}",
            )
            return
        self.imported = results
        self.accept()


class _ImportSourcePanel(QWidget):
    """Single-source import widget hosted inside
    ImportHotWalletsDialog — the per-source UI.

    Owns: directory picker, scan button, candidate list with
    checkboxes, optional source/target passphrase fields. Knows
    nothing about persisting the result — exposes ``do_import``
    to hand the dialog a list of (address, label, keystore) dicts."""

    state_changed = Signal()

    def __init__(self, source, existing_lower: set[str], parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QFileDialog, QPlainTextEdit
        self._source = source
        self._existing = existing_lower
        self._candidates: list = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Directory row.
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("Directory:"))
        self.dir_edit = QLineEdit(str(source.default_dir()))
        self.dir_edit.setFont(QFont("monospace"))
        dir_row.addWidget(self.dir_edit, 1)
        browse = QPushButton("&Browse…")
        browse.clicked.connect(self._browse)
        dir_row.addWidget(browse)
        self.refresh_btn = QPushButton("&Scan")
        self.refresh_btn.clicked.connect(self.refresh)
        dir_row.addWidget(self.refresh_btn)
        layout.addLayout(dir_row)

        # Candidate list — uses extended SELECTION (click /
        # Ctrl-click / Shift-click), same UX as the Ledger scan
        # dialog. We switched from checkboxes-per-row because the
        # extended-selection feel matches what users already
        # know from the Ledger flow, and the per-row checkboxes
        # were visually noisier than a simple highlight.
        self.list = QListWidget()
        self.list.setFont(QFont("monospace"))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        # Address + label rows can exceed the dialog width; elide
        # in the middle and skip the horizontal scrollbar (same
        # reasoning as the wallet tree).
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.itemSelectionChanged.connect(self.state_changed.emit)
        layout.addWidget(self.list, 1)

        # Status line — "N found, K already imported" / errors.
        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        layout.addWidget(self.status_lbl)

        # Passphrase fields — only shown when the source needs them.
        form = QFormLayout()
        form.setVerticalSpacing(8)
        self.src_pass_edit = None
        self.dst_pass1_edit = None
        self.dst_pass2_edit = None
        if source.needs_source_passphrase:
            self.src_pass_edit = QLineEdit()
            self.src_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.src_pass_edit.setMinimumHeight(30)
            self.src_pass_edit.textChanged.connect(
                lambda _: self.state_changed.emit()
            )
            form.addRow(f"{source.name} passphrase:", self.src_pass_edit)
        if source.needs_target_passphrase:
            self.dst_pass1_edit = QLineEdit()
            self.dst_pass1_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.dst_pass1_edit.setMinimumHeight(30)
            self.dst_pass2_edit = QLineEdit()
            self.dst_pass2_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.dst_pass2_edit.setMinimumHeight(30)
            self.dst_pass1_edit.textChanged.connect(
                lambda _: self.state_changed.emit()
            )
            self.dst_pass2_edit.textChanged.connect(
                lambda _: self.state_changed.emit()
            )
            form.addRow("&New qeth passphrase:", self.dst_pass1_edit)
            form.addRow("Confirm &passphrase:", self.dst_pass2_edit)
        if form.rowCount() > 0:
            layout.addLayout(form)

    def _browse(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        chosen = QFileDialog.getExistingDirectory(
            self, f"{self._source.name} directory",
            self.dir_edit.text(),
        )
        if chosen:
            self.dir_edit.setText(chosen)
            self.refresh()

    def refresh(self) -> None:
        from pathlib import Path
        self.list.clear()
        self._candidates = []
        try:
            cands = self._source.discover(Path(self.dir_edit.text()))
        except Exception as e:
            self.status_lbl.setText(f"Scan failed: {e}")
            self.state_changed.emit()
            return
        already = 0
        for c in cands:
            item = QListWidgetItem(f"{c.address}   {c.label}")
            already_have = c.address.lower() in self._existing
            if already_have:
                item.setText(f"{c.address}   {c.label}   (already imported)")
                # Already-imported rows can't be selected — strip
                # ItemIsSelectable + ItemIsEnabled so the row
                # renders muted and won't enter the selection.
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                already += 1
            self.list.addItem(item)
            self._candidates.append(c)
            # Pre-select new rows AFTER addItem (selection state
            # lives on the model, not the item; setting it before
            # the item joins the model is a no-op).
            if not already_have:
                item.setSelected(True)
        if not cands:
            self.status_lbl.setText(
                f"No {self._source.name} accounts found in this directory."
            )
        elif already:
            self.status_lbl.setText(
                f"{len(cands)} found ({already} already imported)"
            )
        else:
            self.status_lbl.setText(f"{len(cands)} found")
        self.state_changed.emit()

    def _selected_candidates(self) -> list:
        out = []
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.isSelected() and (item.flags() & Qt.ItemFlag.ItemIsEnabled):
                out.append(self._candidates[i])
        return out

    def ready_to_import(self) -> bool:
        if not self._selected_candidates():
            return False
        if self.src_pass_edit is not None:
            if not self.src_pass_edit.text():
                return False
        if self.dst_pass1_edit is not None and self.dst_pass2_edit is not None:
            p1 = self.dst_pass1_edit.text()
            p2 = self.dst_pass2_edit.text()
            if not p1 or p1 != p2 or len(p1) < 8:
                return False
        return True

    def do_import(self) -> list[dict]:
        """Run the source's import_one for every checked candidate
        and return ``[{'address', 'label', 'keystore'}, ...]``. A
        bad-passphrase error on the first candidate aborts the
        whole batch — we don't want to import half the ring under
        the wrong key."""
        src_pass = self.src_pass_edit.text() if self.src_pass_edit else None
        dst_pass = (
            self.dst_pass1_edit.text() if self.dst_pass1_edit else None
        )
        results: list[dict] = []
        for c in self._selected_candidates():
            addr, ks = self._source.import_one(
                c,
                source_passphrase=src_pass,
                target_passphrase=dst_pass,
            )
            results.append({
                "address": addr,
                "label": c.label,
                "keystore": ks,
            })
        return results


# --- Right-hand details panel ------------------------------------------------

