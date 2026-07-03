"""WalletsPlugin — accounts tree + details panel + account actions.

Step 4 of the plugin refactor. Owns:
- The QTreeWidget listing Ledger / Hot wallet / Watch-only accounts,
  filling the whole panel.
- The account-action buttons on the bottom row: Add / Copy / Remove
  plus the per-account Sign / QR / Label / Connect icon buttons.
  Sign opens the compose/sign flow; QR opens ``AccountInfoDialog``
  (receive QR + address/path/source/scheme); Label opens an inline
  text popup; Connect is checkable and mirrors the dapp-facing
  default. (These replace the old bottom DetailsPanel.)

Wallets is the source of the selection broadcast — when the user
picks an address, the plugin emits ``selected_address_changed`` and
MainWindow forwards that to the right slot, which broadcasts to its
mounted plugins (Tokens, Transactions). ``default_account_changed``
fires when the user toggles which address is the dapp-facing default,
so the host can refresh the status bar.
"""

from __future__ import annotations

import logging

import io
from typing import Any, cast

import segno


log = logging.getLogger("qeth.plugin.wallets")

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction, QFont, QIcon, QKeySequence, QPalette, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QProgressBar, QPushButton,
    QSizePolicy, QSpinBox, QStyle, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QThread

from ..alerts import confirm, error, info, warn
from ..dialog import (
    Dialog, address_field_min_width, item_spacing, prompt_text,
)
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
        self._container: QWidget | None = None
        self._tree: QTreeWidget | None = None
        # Retired: the bottom details panel + its enclosing splitter.
        # Kept as a None attribute so test/host code that probes for
        # "is there a details panel" reads False rather than raising.
        self._details = None
        self._account_buttons: list[QPushButton] = []
        self.act_add: QAction | None = None
        self.act_copy: QAction | None = None
        self.act_remove: QAction | None = None
        # Per-account actions that used to live in the details panel,
        # now icon buttons on the bottom action row (right of Copy /
        # Remove). Connect is checkable — its checked state mirrors
        # "this address is the dapp-facing default".
        self.act_sign: QAction | None = None
        self.act_qr: QAction | None = None
        self.act_label: QAction | None = None
        self.act_connect: QAction | None = None
        # The checkable Connect button (synced to the default account).
        self._connect_btn: QPushButton | None = None
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
    def selected_address(self) -> str | None:
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

    def _find_item(self, address: str) -> QTreeWidgetItem | None:
        """The tree leaf carrying ``address`` (case-insensitive), or None."""
        if self._tree is None or not address:
            return None
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
                return hit
        return None

    def _apply_label_in_place(self, address: str, name: str,
                              verified: bool) -> bool:
        """Update one account row's label WITHOUT a full _rebuild_tree — used by
        the async ENS reverse-lookup path (5c). A rebuild there clears the tree,
        which fires itemSelectionChanged (account=None → account=addr) and so
        yanks the view off whatever account the user was reading and re-fetches
        the right-slot panels. Setting the label role repaints just the row.
        Returns True if the row was found."""
        it = self._find_item(address)
        if it is None:
            return False
        it.setData(0, ACCOUNT_LABEL_ROLE, name)
        if verified:
            it.setToolTip(0, f"{name} — cryptographically verified")
        return True

    def select_address(self, address: str) -> bool:
        """Programmatically focus the tree on the leaf carrying
        ``address`` (case-insensitive). Returns True if the address
        was found and selected, False otherwise. Used by MainWindow
        after a broadcast to make sure the user is looking at the
        ``from`` account when the pending row appears."""
        hit = self._find_item(address)
        if hit is None or self._tree is None:
            return False
        # Already the sole selection? Do nothing. clearSelection() would
        # broadcast account=None — which clears the transactions view and its
        # cached activities — then the re-select forces a full
        # show_transactions + re-fetch of every row's activity (the "redraws
        # every pic" on send). A no-op keeps a just-sent pending row a cheap
        # one-row prepend.
        sel = self._tree.selectedItems()
        if (self._tree.currentItem() is hit
                and len(sel) == 1 and sel[0] is hit):
            return True
        self._tree.clearSelection()
        self._tree.setCurrentItem(hit)
        hit.setSelected(True)
        return True

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
        # The per-account details (address / path / QR) and the
        # Sign / Connect actions used to live in a bottom details
        # panel inside a vertical splitter. They're now icon buttons
        # on the bottom action row + popups (Sign / QR-info / Label),
        # so the tree fills the whole panel.
        v.addWidget(self._tree, 1)

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

        # Sign / QR / Label / Connect — per-account actions that moved
        # off the old details panel onto this row. Sign and Connect
        # need a signing key (disabled for watch-only); QR and Label
        # work for any selected account.
        self.act_sign = QAction(
            _icon("document-edit", "document-sign", "edit-paste",
                  QStyle.StandardPixmap.SP_FileDialogDetailedView),
            "&Sign Message…",
        )
        self.act_sign.setToolTip("Sign a message")
        self.act_sign.setEnabled(False)
        self.act_sign.triggered.connect(self._sign_selected)

        self.act_qr = QAction(
            _icon("view-barcode-qr", "view-barcode", "qrcode",
                  QStyle.StandardPixmap.SP_FileDialogContentsView),
            "Show &QR / Info…",
        )
        self.act_qr.setToolTip("Show QR code and account details")
        self.act_qr.setEnabled(False)
        self.act_qr.triggered.connect(self._show_qr_info)

        # Distinct QStyle fallback (ListView) from Sign's DetailedView and
        # QR's ContentsView so the three stay tellable apart even on a
        # bare theme that lacks the named icons.
        self.act_label = QAction(
            _icon("tag", "bookmark-new", "edit-rename",
                  QStyle.StandardPixmap.SP_FileDialogListView),
            "Edit &Label…",
        )
        self.act_label.setToolTip("Edit this account's label")
        self.act_label.setEnabled(False)
        self.act_label.triggered.connect(self._edit_label)

        # Network link icon for "make this address visible to dapps via
        # the local JSON-RPC server" — reads as connecting a link, not
        # opening a web page. `network-transmit[-receive]` are freedesktop
        # status-context names (widely shipped). Checkable: pressed when
        # this account is the dapp-facing default.
        self.act_connect = QAction(
            _icon("network-transmit", "network-transmit-receive",
                  "network-wired",
                  QStyle.StandardPixmap.SP_DriveNetIcon),
            "Connect to &Browser",
        )
        self.act_connect.setToolTip("Make default for dapps")
        self.act_connect.setCheckable(True)
        self.act_connect.setEnabled(False)
        self.act_connect.triggered.connect(self._toggle_connect)

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
        # Connect is checkable (pressed = this account is connected to
        # the browser); the rest are momentary. We keep a ref to the
        # Connect button so _update_account_buttons can sync its checked
        # state to the store's default.
        for act in (self.act_copy, self.act_remove, self.act_sign,
                    self.act_qr, self.act_label, self.act_connect):
            btn = QPushButton()
            btn.setIcon(act.icon())
            btn.setToolTip(act.toolTip() or act.text().replace("&", ""))
            btn.setFlat(True)
            btn.setMaximumSize(28, 28)
            btn.setIconSize(QSize(16, 16))
            btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            btn.setEnabled(act.isEnabled())
            act.enabledChanged.connect(btn.setEnabled)
            if act is self.act_connect:
                btn.setCheckable(True)
                # The click toggles the button; the handler re-asserts
                # the checked state from the store afterward, so a click
                # on the already-connected account doesn't visually
                # un-press it.
                btn.clicked.connect(lambda _checked: self._toggle_connect())
                self._connect_btn = btn
            else:
                btn.clicked.connect(act.trigger)
            # Keep a Python ref so the C++ widgets survive function exit.
            self._account_buttons.append(btn)

    def _show_add_account_menu(self) -> None:
        """Triggered by act_add (e.g. from the tree's right-click
        menu). Pops the same Ledger/Watch-only picker as the
        toolbar dropdown, anchored at the cursor."""
        from PySide6.QtGui import QCursor
        # cast: ty mis-picks the QMenu.exec overload for a QPoint arg.
        cast(Any, self._add_menu).exec(QCursor.pos())

    # --- tree population ----------------------------------------------------

    def _capture_expansion(self) -> dict:
        """Snapshot which keyed group/root rows are expanded, so a rebuild
        can preserve the user's collapse state instead of resetting it."""
        out: dict = {}
        if self._tree is None:
            return out

        def walk(item: QTreeWidgetItem | None) -> None:
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

    def _make_account_item(self, a: dict) -> tuple[QTreeWidgetItem, bool]:
        """Build the address-leaf row for one account — shared by the Ledger /
        hot / watch-only sections (previously copy-pasted three times). Returns
        ``(item, is_default)`` so the caller can track the default row; the
        caller adds it under the right parent."""
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
                it.setToolTip(0, f"{label_text} — cryptographically verified")
        it.setFont(0, QFont("monospace"))
        # Address leaf: selectable + draggable, never a drop target (an address
        # can only land in the gap between siblings, resolved at the parent).
        it.setFlags(
            Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsDragEnabled
        )
        return it, is_default

    def _rebuild_tree(self) -> None:
        if self._tree is None:
            return
        # Preserve the user's current selection across the rebuild: an async
        # trigger (an ENS reverse-lookup, a background rediscover) must not yank
        # the view to the default account while they're reading another (5c).
        # Only fall back to the default when there was no single prior selection.
        prior = self.selected_address
        expanded = self._capture_expansion()
        self._tree.clear()
        # Each source's root row is shown only when it HAS accounts — an empty
        # "Watch only (0)" / "Hot wallet (0)" root is just noise (the Add button
        # below the tree is the discovery affordance, not these roots). With no
        # accounts at all the tree is simply empty.
        default_item: QTreeWidgetItem | None = None

        ledger_accts = [a for a in self._store.accounts if a.get("source") == "ledger"]
        if ledger_accts:
            ledger_root = QTreeWidgetItem([f"Ledger ({len(ledger_accts)})"])
            # Reuse the add-account menu icons so the tree groups and the
            # picker stay visually consistent (hardware device / key / eye).
            ledger_root.setIcon(0, self.act_add_ledger.icon())
            # Group containers: not draggable, not drop targets (we only
            # allow re-ordering inside scheme subgroups).
            ledger_root.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self._tree.addTopLevelItem(ledger_root)
            groups: dict[str, QTreeWidgetItem] = {}
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
                it, is_default = self._make_account_item(a)
                grp.addChild(it)
                if is_default:
                    default_item = it
            self._restore_expand(ledger_root, "ledger", expanded)
            for scheme, g in groups.items():
                self._restore_expand(g, f"ledger/{scheme}", expanded)

        hot_accts = [a for a in self._store.accounts
                      if a.get("source") == "hot"]
        if hot_accts:
            hot_root = QTreeWidgetItem([f"Hot wallet ({len(hot_accts)})"])
            hot_root.setIcon(0, self.act_add_hot.icon())
            hot_root.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)
            self._tree.addTopLevelItem(hot_root)
            for a in hot_accts:
                it, is_default = self._make_account_item(a)
                hot_root.addChild(it)
                if is_default:
                    default_item = it
            self._restore_expand(hot_root, "hot", expanded)

        watch_accts = [a for a in self._store.accounts
                        if a.get("source") == "watch_only"]
        if watch_accts:
            watch_root = QTreeWidgetItem([f"Watch only ({len(watch_accts)})"])
            watch_root.setIcon(0, self.act_add_watch.icon())
            # Top-level group: not draggable, not a drop target — same
            # treatment as the Ledger root.
            watch_root.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)
            self._tree.addTopLevelItem(watch_root)
            for a in watch_accts:
                it, is_default = self._make_account_item(a)
                watch_root.addChild(it)
                if is_default:
                    default_item = it
            self._restore_expand(watch_root, "watch", expanded)

        if not (prior and self.select_address(prior)):
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

        def walk(item: QTreeWidgetItem | None) -> None:
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
        addrs = self.selected_addresses()
        self._update_account_buttons(addrs)
        if len(addrs) == 1:
            acct = next(
                (a for a in self._store.accounts if a["address"] == addrs[0]),
                None,
            )
            if acct:
                self.selected_address_changed.emit(addrs[0])
                return
        self.selected_address_changed.emit(None)

    def _update_account_buttons(self, addrs: list[str] | None = None) -> None:
        """Sync the bottom-row action buttons to the current selection.

        Copy works for a single address; Remove for any non-empty
        selection. Sign / QR / Label / Connect are single-account
        actions. Sign and Connect additionally need a signing key, so
        they stay off for watch-only accounts. Connect is checkable —
        pressed when the selected account is the dapp-facing default."""
        assert (self.act_copy is not None and self.act_remove is not None
                and self.act_sign is not None and self.act_qr is not None
                and self.act_label is not None
                and self.act_connect is not None)
        if addrs is None:
            addrs = self.selected_addresses()
        single = len(addrs) == 1
        acct = None
        if single:
            acct = next(
                (a for a in self._store.accounts if a["address"] == addrs[0]),
                None,
            )
        is_watch = acct is not None and acct.get("source") == "watch_only"
        is_default = single and addrs[0] == self._store.default_account
        self.act_copy.setEnabled(single)
        self.act_remove.setEnabled(len(addrs) >= 1)
        self.act_qr.setEnabled(single)
        self.act_label.setEnabled(single)
        self.act_sign.setEnabled(single and not is_watch)
        self.act_connect.setEnabled(single and not is_watch and not is_default)
        self.act_connect.setChecked(bool(is_default))
        if self._connect_btn is not None:
            self._connect_btn.setChecked(bool(is_default))
            self._connect_btn.setToolTip(
                "Watch-only — can't connect" if is_watch
                else "Connected to browser" if is_default
                else "Connect to browser (make default for dapps)"
            )

    def _on_tree_context_menu(self, pos) -> None:
        """Tree right-click menu. Mirrors the Add / Copy / Remove
        button row and exposes Set-as-default — which the details
        pane below already offers, but having it on the row's right-
        click means the user doesn't have to navigate down."""
        assert (self.act_add is not None and self.act_copy is not None
                and self.act_remove is not None and self.act_connect is not None
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
                    self.act_connect.icon(), "Connect to Browser")
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
                # Update the row in place — a full rebuild here would yank the
                # user's selection to the default account (5c). Fall back only
                # if the row isn't built yet.
                if not self._apply_label_in_place(
                        addr_lower, name, addr_lower in self._ens_verified):
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

    def _sign_selected(self) -> None:
        """Sign button → open the compose/sign flow for the selected
        account (forwarded to the host)."""
        addr = self.selected_address
        if addr:
            self._on_sign_message(addr)

    def _show_qr_info(self) -> None:
        """QR button → modal popup with the receive QR plus the
        account's address / path / source / scheme."""
        addr = self.selected_address
        if not addr:
            return
        acct = next(
            (a for a in self._store.accounts if a["address"] == addr), None,
        )
        if acct is None:
            return
        dlg = AccountInfoDialog(acct, parent=self._container)
        dlg.exec()

    def _edit_label(self) -> None:
        """Label button → small text popup to edit the account's
        label; persists via the same path as the old inline field."""
        addr = self.selected_address
        if not addr:
            return
        acct = next(
            (a for a in self._store.accounts if a["address"] == addr), None,
        )
        current = (acct.get("label") if acct else "") or ""
        new, ok = prompt_text(
            self._container, "Edit Label", f"Label for {addr}:", current,
        )
        if ok and new.strip() != current:
            self._on_label_changed(addr, new.strip())

    def _toggle_connect(self) -> None:
        """Connect button → make the selected account the dapp-facing
        default. Clicking the already-connected account is a no-op (we
        just re-assert the pressed state)."""
        addr = self.selected_address
        if not addr:
            self._update_account_buttons()
            return
        default = self._store.default_account
        if default is not None and addr.lower() == default.lower():
            self._update_account_buttons()
            return
        self._set_default(addr)

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


# --- AccountInfoDialog + AddLedgerDialog (moved from qeth.ui) --------------

class AccountInfoDialog(Dialog):
    """Modal popup for a single account: the receive QR plus the
    address / path / source / scheme. Opened from the QR button on
    the accounts panel's action row (the info used to sit in a
    permanent details panel below the tree)."""

    def __init__(self, account: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Account")
        v = QVBoxLayout(self)

        form = QFormLayout()
        mono = QFont("monospace")
        self.address_lbl = QLabel(account["address"]); self.address_lbl.setFont(mono)
        self.address_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_lbl = QLabel(account.get("path", "—")); self.path_lbl.setFont(mono)
        self.source_lbl = QLabel(account.get("source", "—"))
        self.scheme_lbl = QLabel(account.get("scheme", "—"))
        label_text = account.get("label") or ""
        if label_text:
            form.addRow("Label:", QLabel(label_text))
        form.addRow("Address:", self.address_lbl)
        form.addRow("Path:", self.path_lbl)
        form.addRow("Source:", self.source_lbl)
        form.addRow("Scheme:", self.scheme_lbl)
        v.addLayout(form)

        # form / QR / buttons are three paragraphs — the Dialog base spaces them.
        self.qr_lbl = QLabel()
        self.qr_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_lbl.setFixedSize(220, 220)
        v.addWidget(self.qr_lbl, 0, Qt.AlignmentFlag.AlignCenter)
        self._render_qr(account["address"])

        btns = QDialogButtonBox()
        copy_btn = btns.addButton("&Copy Address",
                                  QDialogButtonBox.ButtonRole.ActionRole)
        copy_btn.setIcon(QIcon.fromTheme("edit-copy"))
        close_btn = btns.addButton(QDialogButtonBox.StandardButton.Close)
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(account["address"]))
        close_btn.clicked.connect(self.accept)
        v.addWidget(btns)

    def _render_qr(self, address: str) -> None:
        buf = io.BytesIO()
        # ethereum: URI per EIP-681 so wallets recognize it as a send intent
        segno.make(f"ethereum:{address}", error="m").save(
            buf, kind="png", scale=6, border=2)
        pix = QPixmap()
        pix.loadFromData(buf.getvalue())  # format auto-detected from the PNG header
        self.qr_lbl.setPixmap(pix.scaled(
            self.qr_lbl.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        ))


# --- Token list panel -------------------------------------------------------
#
# The six token-related QThread workers (TokenListsLoader, TokenListWorker,
# RiskWorker, MetadataWorker, BalanceWorker, PricesWorker) used to live
# here. They moved into qeth.plugins.tokens as part of the plugin refactor
# (step 3) — they're token-domain code, owned by TokensPlugin now.




class AddLedgerDialog(Dialog):
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


class AddWatchOnlyDialog(Dialog):
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
        # Margins and paragraph spacing (between the form and the button row)
        # come from the Dialog base — font-derived, uniform across all dialogs.

        form = QFormLayout()
        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText(
            "0x… address or ENS name (e.g. vitalik.eth)")
        self.address_edit.setFont(QFont("monospace"))
        # Wide enough that a full 0x address shows without scrolling.
        self.address_edit.setMinimumWidth(address_field_min_width(self))
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
        self._ens_forward_addr: str | None = None

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

    def _set_resolved(self, text: str | None) -> None:
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


class AddHotWalletDialog(Dialog):
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
        # Margins + paragraph spacing come from the Dialog base (font-derived).

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

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("e.g. Daily driver")
        self.label_edit.setMinimumHeight(_input_min_h)
        form.addRow("&Label (optional):", self.label_edit)

        # Theme-aware error / ok colours used by both status labels below.
        self._err_color = _palette_aware_error_color(self.palette())
        self._ok_color = _palette_aware_ok_color(self.palette())

        # Two validation lines — passphrase length/match, and private-key
        # rejection. They sit just below the form (not between two fields,
        # where an always-reserved blank row read as a big gap), grouped WITH
        # the form as one paragraph so they don't open a second paragraph gap
        # above the buttons. Empty → they collapse to nothing; in a plain VBox
        # (not a form row) a QLabel grows cleanly when its text is set, so no
        # height needs reserving.
        self.pass_status_lbl = QLabel("")
        self.pass_status_lbl.setVisible(False)   # hidden → zero height when empty
        self.match_lbl = QLabel("")
        self.match_lbl.setWordWrap(True)
        self.match_lbl.setVisible(False)
        self.match_lbl.setStyleSheet(
            f"color: {self._err_color}; font-weight: bold;"
        )
        creds = QVBoxLayout()
        creds.setSpacing(item_spacing(self))
        creds.addLayout(form)
        creds.addWidget(self.pass_status_lbl)
        creds.addWidget(self.match_lbl)
        layout.addLayout(creds)

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

    def _parsed_private_key(self) -> bytes | None:
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

    def _set_status(self, label: QLabel, html: str) -> None:
        """Set a status line's text and hide it when empty, so an empty line
        reserves no height (an empty QLabel still claims a font line)."""
        label.setText(html)
        label.setVisible(bool(html))

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
            self._set_status(self.pass_status_lbl, "")
        elif len(p1) < 8:
            self._set_status(self.pass_status_lbl, 
                f"<span style='color: {self._err_color};"
                f" font-weight: bold;'>"
                f"{len(p1)}/8 characters</span>"
            )
        elif p1 != p2:
            self._set_status(self.pass_status_lbl, 
                f"<span style='color: {self._err_color};"
                f" font-weight: bold;'>"
                f"passphrases don't match</span>"
            )
        else:
            self._set_status(self.pass_status_lbl, 
                f"<span style='color: {self._ok_color};"
                f" font-weight: bold;'>✓ ok</span>"
            )

        if not pk_text.strip():
            self._set_status(self.match_lbl, "")
            self.gen_btn.setEnabled(False)
            return
        if not pk_valid:
            self._set_status(self.match_lbl, 
                "Private key must be 64 hex characters (with or "
                "without 0x prefix)."
            )
            self.gen_btn.setEnabled(False)
            return
        if not p1:
            self._set_status(self.match_lbl, "")
            self.gen_btn.setEnabled(False)
            return
        if p1 != p2:
            self._set_status(self.match_lbl, "")
            self.gen_btn.setEnabled(False)
            return
        if len(p1) < 8:
            self._set_status(self.match_lbl, "")
            self.gen_btn.setEnabled(False)
            return
        self._set_status(self.match_lbl, "")
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


class ImportHotWalletsDialog(Dialog):
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
        self._source = source
        self._existing = existing_lower
        self._candidates: list = []

        layout = QVBoxLayout(self)
        # Outer margins come from the Dialog base (font-derived, uniform).
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

