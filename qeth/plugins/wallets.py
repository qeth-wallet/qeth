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

from typing import Optional

import io

import segno

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton,
    QSizePolicy, QSpinBox, QSplitter, QStyle, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QThread

from ..ledger import DiscoveredAccount, LedgerWorker, PATH_SCHEMES
from ..plugin import Plugin


class _ReorderTree(QTreeWidget):
    """QTreeWidget that allows drag-and-drop reorder of address
    leaves *within the same parent group*. Dropping into a different
    scheme group is rejected so a Ledger Default account can't end
    up under Legacy (or vice-versa) — the scheme is metadata of the
    address, not just a display nest. After a successful drop the
    widget emits ``reorder_committed`` so the plugin can rewrite
    the on-disk account list to match."""

    reorder_committed = Signal()

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
        if indicator == QAbstractItemView.OnItem:
            dest_parent = target
        elif indicator in (
            QAbstractItemView.AboveItem,
            QAbstractItemView.BelowItem,
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
            it.data(0, Qt.UserRole) for it in source_items
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
            if item.data(0, Qt.UserRole) == addr:
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
    name = "Wallets"

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
        self._account_buttons: list[QToolButton] = []
        self.act_add: Optional[QAction] = None
        self.act_copy: Optional[QAction] = None
        self.act_remove: Optional[QAction] = None

    # --- Plugin contract ----------------------------------------------------

    def widget(self) -> QWidget:
        if self._container is None:
            self._build()
            self._rebuild_tree()
        return self._container

    def action_widgets(self):
        # Wallets' action row lives at the top of its own widget (it's
        # part of the Wallets view, not a generic plugin-action set).
        # The shared bottom row of a single-plugin left slot stays
        # empty as a result — by design.
        return []

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
            addr = it.data(0, Qt.UserRole)
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
            addr = item.data(0, Qt.UserRole)
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
                self._tree.clearSelection()
                self._tree.setCurrentItem(hit)
                hit.setSelected(True)
                return True
        return False

    def splitter_state(self) -> str:
        if self._splitter is None:
            return ""
        return bytes(self._splitter.saveState().toHex()).decode()

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

        # Top: account action row.
        v.addLayout(self._build_account_actions())

        # Middle: vertical splitter (tree on top, details on bottom).
        self._splitter = QSplitter(Qt.Vertical)

        self._tree = _ReorderTree()
        self._tree.setHeaderLabels(["Accounts"])
        self._tree.setRootIsDecorated(True)
        self._tree.setTextElideMode(Qt.ElideMiddle)
        self._tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # Drag now reorders the address rows instead of accumulating a
        # selection — multi-select is still available via Ctrl/Shift +
        # click for the Remove button's bulk-remove path. InternalMove
        # restricts dragging to within this widget; the subclass's
        # dropEvent further restricts to within the same parent group.
        self._tree.setDragEnabled(True)
        self._tree.setAcceptDrops(True)
        self._tree.setDropIndicatorShown(True)
        self._tree.setDragDropMode(QAbstractItemView.InternalMove)
        self._tree.setDefaultDropAction(Qt.MoveAction)
        self._tree.itemSelectionChanged.connect(self._on_tree_selection)
        self._tree.reorder_committed.connect(self._on_tree_reordered)
        # Double-click an address leaf = "Connect to browser". The
        # button + right-click menu offer the same action; this is
        # just the no-friction path for the user's primary
        # action-on-account.
        self._tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        # Right-click menu mirrors the top action row (Add / Copy /
        # Remove) plus Set-as-default — so every button has a menu
        # equivalent and every menu item has a button equivalent.
        self._tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(
            self._on_tree_context_menu
        )
        self._splitter.addWidget(self._tree)

        self._details = DetailsPanel()
        self._details.set_default_requested.connect(self._set_default)
        self._details.label_changed.connect(self._on_label_changed)
        details_wrap = QFrame()
        details_wrap.setFrameShape(QFrame.StyledPanel)
        dlay = QVBoxLayout(details_wrap)
        dlay.setContentsMargins(12, 12, 12, 0)
        dlay.addWidget(self._details)
        self._splitter.addWidget(details_wrap)
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([290, 365])
        v.addWidget(self._splitter, 1)

    def _build_account_actions(self) -> QHBoxLayout:
        """Account-level actions (Add / Copy / Remove) rendered as a
        compact icon+text button row at the top of the Wallets widget.
        Lived in a QMainWindow toolbar before the plugin refactor."""
        style_proxy = QApplication.style()
        self.act_add = QAction(
            QIcon.fromTheme("document-new",
                            style_proxy.standardIcon(QStyle.SP_FileIcon)),
            "Add account",
        )
        self.act_add.setToolTip("Add a Ledger or watch-only account")
        # Sub-actions used by both the toolbar dropdown and the
        # tree's right-click menu so the two entry points agree on
        # which dialogs they open.
        self.act_add_ledger = QAction("Ledger account…", self)
        self.act_add_ledger.triggered.connect(self._add_ledger)
        self.act_add_watch = QAction("Watch-only address…", self)
        self.act_add_watch.triggered.connect(self._add_watch_only)
        # Triggering act_add itself shows the picker menu — invoked
        # via the right-click "Add account" item in the tree.
        self.act_add.triggered.connect(self._show_add_account_menu)

        self.act_copy = QAction(
            QIcon.fromTheme("edit-copy",
                            style_proxy.standardIcon(QStyle.SP_DialogSaveButton)),
            "Copy address",
        )
        self.act_copy.setEnabled(False)
        self.act_copy.triggered.connect(self._copy_selected_address)

        self.act_remove = QAction(
            QIcon.fromTheme("list-remove",
                            style_proxy.standardIcon(QStyle.SP_TrashIcon)),
            "Remove account",
        )
        self.act_remove.setEnabled(False)
        self.act_remove.triggered.connect(self._remove_selected_account)

        row = QHBoxLayout()
        row.setContentsMargins(4, 2, 4, 4)

        # Add button: QMenu of "Ledger account…" / "Watch-only
        # address…". InstantPopup means a click anywhere on the
        # button opens the menu (no separate arrow split).
        add_btn = QToolButton()
        add_btn.setIcon(self.act_add.icon())
        add_btn.setText(self.act_add.text())
        add_btn.setToolTip(self.act_add.toolTip())
        add_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        add_btn.setAutoRaise(True)
        add_btn.setIconSize(QSize(16, 16))
        self._add_menu = QMenu(add_btn)
        self._add_menu.addAction(self.act_add_ledger)
        self._add_menu.addAction(self.act_add_watch)
        add_btn.setMenu(self._add_menu)
        add_btn.setPopupMode(QToolButton.InstantPopup)
        row.addWidget(add_btn)
        self._account_buttons.append(add_btn)

        for act in (self.act_copy, self.act_remove):
            btn = QToolButton()
            btn.setDefaultAction(act)
            btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            btn.setAutoRaise(True)
            btn.setIconSize(QSize(16, 16))
            row.addWidget(btn)
            # Keep a Python ref so the C++ widgets survive function exit.
            self._account_buttons.append(btn)
        row.addStretch(1)
        return row

    def _show_add_account_menu(self) -> None:
        """Triggered by act_add (e.g. from the tree's right-click
        menu). Pops the same Ledger/Watch-only picker as the
        toolbar dropdown, anchored at the cursor."""
        from PySide6.QtGui import QCursor
        self._add_menu.exec(QCursor.pos())

    # --- tree population ----------------------------------------------------

    def _rebuild_tree(self) -> None:
        if self._tree is None:
            return
        self._tree.clear()
        ledger_accts = [a for a in self._store.accounts if a.get("source") == "ledger"]
        ledger_root = QTreeWidgetItem([f"Ledger ({len(ledger_accts)})"])
        # Group containers: not draggable, not drop targets (we only
        # allow re-ordering inside scheme subgroups).
        ledger_root.setFlags(Qt.ItemIsEnabled)
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
                grp.setFlags(Qt.ItemIsEnabled | Qt.ItemIsDropEnabled)
                ledger_root.addChild(grp)
                groups[scheme] = grp
            addr = a["address"]
            is_default = (
                self._store.default_account is not None
                and addr.lower() == self._store.default_account.lower()
            )
            display = f"[{addr}]" if is_default else f" {addr} "
            label_text = a.get("label") or ""
            if label_text:
                display = f"{display}   {label_text}"
            it = QTreeWidgetItem([display])
            it.setData(0, Qt.UserRole, addr)
            it.setFont(0, QFont("monospace"))
            # Address leaf: selectable + draggable, NOT a drop target
            # (so an address can't be dropped onto another address —
            # only into the gap between siblings, which Qt resolves at
            # the parent group level).
            it.setFlags(
                Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsDragEnabled
            )
            grp.addChild(it)
            if is_default:
                default_item = it
        ledger_root.setExpanded(True)
        for g in groups.values():
            g.setExpanded(True)

        # Hot wallet stub — not yet implemented.
        hot = QTreeWidgetItem(["Hot wallet (0)"])
        hot.setFlags(Qt.ItemIsEnabled)
        self._tree.addTopLevelItem(hot)

        watch_accts = [a for a in self._store.accounts
                        if a.get("source") == "watch_only"]
        watch_root = QTreeWidgetItem([f"Watch only ({len(watch_accts)})"])
        # Top-level group: not draggable, not a drop target — same
        # treatment as the Ledger root.
        watch_root.setFlags(Qt.ItemIsEnabled | Qt.ItemIsDropEnabled)
        self._tree.addTopLevelItem(watch_root)
        for a in watch_accts:
            addr = a["address"]
            is_default = (
                self._store.default_account is not None
                and addr.lower() == self._store.default_account.lower()
            )
            label_text = a.get("label") or ""
            display = f"[{addr}]" if is_default else f" {addr} "
            if label_text:
                display = f"{display}   {label_text}"
            it = QTreeWidgetItem([display])
            it.setData(0, Qt.UserRole, addr)
            it.setFont(0, QFont("monospace"))
            it.setFlags(
                Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsDragEnabled
            )
            watch_root.addChild(it)
            if is_default:
                default_item = it
        if watch_accts:
            watch_root.setExpanded(True)

        if default_item is not None:
            self._tree.setCurrentItem(default_item)

    # --- selection / action handlers ---------------------------------------

    def _on_tree_reordered(self) -> None:
        """Walk the tree top-to-bottom collecting addresses in their
        current display order, then persist that order via the Store.
        Triggered after _ReorderTree commits an internal move."""
        ordered: list[str] = []

        def walk(item: QTreeWidgetItem) -> None:
            addr = item.data(0, Qt.UserRole)
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
        addr = item.data(0, Qt.UserRole)
        if not isinstance(addr, str) or not addr:
            return
        current = self._store.default_account
        if current is not None and addr.lower() == current.lower():
            return
        self._set_default(addr)

    def _on_tree_selection(self) -> None:
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
                act_default = menu.addAction("Connect to browser")
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
        if len(addrs) == 1:
            prompt = f"Remove {addrs[0]} from this wallet?"
        else:
            preview = "\n".join(f"  • {a}" for a in addrs[:5])
            extra = f"\n  … and {len(addrs) - 5} more" if len(addrs) > 5 else ""
            prompt = (
                f"Remove {len(addrs)} accounts from this wallet?\n\n"
                f"{preview}{extra}"
            )
        reply = QMessageBox.question(
            self._container,
            "Remove account" if len(addrs) == 1 else "Remove accounts",
            f"{prompt}\n\n"
            "Keys on your Ledger are untouched; this only forgets the "
            "addresses locally. You can re-add them via Scan at any time.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        removed = sum(1 for a in addrs if self._store.remove_account(a))
        if removed:
            self._rebuild_tree()
            self.default_account_changed.emit()
            if self.host is not None:
                self.host.status_message(f"Removed {removed} account(s)", 3000)

    def _add_ledger(self) -> None:
        if self.host is None:
            return
        dlg = AddLedgerDialog(self.host.current_chain(), self._container)
        if dlg.exec() != QDialog.Accepted:
            return
        scheme = dlg.scheme_combo.currentText()
        added = 0
        for d in dlg.selected_accounts():
            if self._store.add_account({
                "address": d.address,
                "path": d.path,
                "source": "ledger",
                "scheme": scheme,
                "label": "",
            }):
                added += 1
        self._rebuild_tree()
        self.default_account_changed.emit()
        if added and self.host is not None:
            self.host.status_message(f"Added {added} account(s)", 3000)

    def _add_watch_only(self) -> None:
        """Open the small "Add watch-only address" modal and
        persist the result. Watch-only accounts have no key — they
        appear in the wallet tree (under "Watch only") so balances
        and tx history are visible, but Send / Connect-to-browser
        fail with "No known signer" for them (and the actions are
        disabled when one is selected)."""
        existing = {a["address"] for a in self._store.accounts}
        dlg = AddWatchOnlyDialog(existing, self._container)
        if dlg.exec() != QDialog.Accepted:
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

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        # No bottom margin so the Set-as-default button can sit flush with
        # the bottom of the splitter (matches the right panel's bottom edge).
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
        self.address_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.address_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.path_lbl = QLabel("—"); self.path_lbl.setFont(mono)
        self.path_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
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
        self.qr_lbl.setAlignment(Qt.AlignCenter)
        self.qr_lbl.setFixedSize(220, 220)
        v.addWidget(self.qr_lbl, 0, Qt.AlignCenter)
        v.addSpacing(12)

        # Short label + tooltip rather than a wide button — keeps the
        # panel narrow-shrinkable. Same policy trick on the button itself.
        # "Connect to browser" makes the action concrete: this is the
        # address dapps will see via the local JSON-RPC server (Frame
        # interface), nothing about persistence or "default".
        self.set_default_btn = QPushButton("Connect to browser")
        # Globe / browser icon for "make this address visible to the
        # web". Same icon the Transactions list uses for "Open in
        # block explorer" — reuse keeps the "exposed to the web"
        # association consistent across the app.
        _conn_icon = QIcon.fromTheme(
            "applications-internet",
            QIcon.fromTheme(
                "internet-web-browser",
                QApplication.style().standardIcon(QStyle.SP_DesktopIcon),
            ),
        )
        if not _conn_icon.isNull() and _conn_icon.availableSizes():
            self.set_default_btn.setIcon(_conn_icon)
        self.set_default_btn.setToolTip(
            "Make this the address dapps see (returned by eth_accounts "
            "over the local JSON-RPC server)"
        )
        self.set_default_btn.setEnabled(False)
        # Pin the height. With QSizePolicy.Fixed Qt re-queries sizeHint()
        # every time the text changes — and "Connected ✓" can come out
        # a touch shorter than "Connect to browser" depending on the
        # theme. The snapshot here is taken while the (longer) text is
        # set, so the disabled state never shrinks.
        self.set_default_btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.set_default_btn.setMinimumHeight(self.set_default_btn.sizeHint().height())
        self.set_default_btn.clicked.connect(
            lambda: self._current and self.set_default_requested.emit(self._current)
        )
        # Stretch pushes the button to the very bottom of the panel.
        v.addStretch(1)
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
            self.set_default_btn.setText("Watch-only — read-only")
            self.set_default_btn.setToolTip(
                "Watch-only accounts have no signing key — they "
                "can't be the address dapps see."
            )
        else:
            self.set_default_btn.setEnabled(not is_default)
            self.set_default_btn.setText(
                "Connected to browser ✓" if is_default
                else "Connect to browser"
            )
            self.set_default_btn.setToolTip(
                "Make this the address dapps see (returned by "
                "eth_accounts over the local JSON-RPC server)"
            )
        self._render_qr(account["address"])

    def _render_qr(self, address: str) -> None:
        buf = io.BytesIO()
        # ethereum: URI per EIP-681 so wallets recognize it as a send intent
        segno.make(f"ethereum:{address}", error="m").save(buf, kind="png", scale=6, border=2)
        pix = QPixmap()
        pix.loadFromData(buf.getvalue(), "PNG")
        self.qr_lbl.setPixmap(pix.scaled(
            self.qr_lbl.size(), Qt.KeepAspectRatio, Qt.FastTransformation
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
        self.set_default_btn.setText("Connect to browser")

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
        self.setWindowTitle("Add Ledger accounts")
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
        self.results.setSelectionMode(QAbstractItemView.ExtendedSelection)
        font = QFont("monospace")
        self.results.setFont(font)
        layout.addWidget(self.results, 1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        btns = QHBoxLayout()
        style_proxy = QApplication.style()
        self.scan_btn = QPushButton("Scan")
        # Magnifier icon reads as "look for accounts on the device".
        _scan_icon = QIcon.fromTheme(
            "system-search",
            QIcon.fromTheme(
                "edit-find",
                style_proxy.standardIcon(QStyle.SP_FileDialogContentsView),
            ),
        )
        if not _scan_icon.isNull() and _scan_icon.availableSizes():
            self.scan_btn.setIcon(_scan_icon)
        self.add_btn = QPushButton("Add selected")
        # Same "+" icon the toolbar Add buttons use across the app —
        # keeps the meaning stable wherever the user sees it.
        _add_icon = QIcon.fromTheme(
            "list-add",
            style_proxy.standardIcon(QStyle.SP_FileDialogNewFolder),
        )
        if not _add_icon.isNull() and _add_icon.availableSizes():
            self.add_btn.setIcon(_add_icon)
        self.add_btn.setEnabled(False)
        # Close stays text-only — Qt's QDialogButtonBox-derived
        # dialogs across the app render Cancel/Close without
        # icons, and adding one here would be inconsistent.
        self.close_btn = QPushButton("Close")
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
        item.setData(Qt.UserRole, acct)
        item.setSelected(acct.nonce > 0)
        self.results.addItem(item)
        if self.progress.maximum() > 0:
            self.progress.setValue(self.progress.value() + 1)

    def _on_done(self) -> None:
        self.progress.setVisible(False)
        self.scan_btn.setEnabled(True)
        self.add_btn.setEnabled(bool(self.results.selectedItems()))

    def _on_failed(self, msg: str) -> None:
        self.progress.setVisible(False)
        self.scan_btn.setEnabled(True)
        QMessageBox.critical(self, "Ledger error", msg)

    def selected_accounts(self) -> list[DiscoveredAccount]:
        return [it.data(Qt.UserRole) for it in self.results.selectedItems()]


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
        self.setWindowTitle("Add watch-only address")
        # Lower-case set of addresses already in the store, used for
        # the duplicate check on accept.
        self._existing = {a.lower() for a in existing_addresses}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)

        form = QFormLayout()
        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText("0x… address to watch")
        self.address_edit.setFont(QFont("monospace"))
        form.addRow("Address:", self.address_edit)

        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("e.g. Cold storage, Treasury")
        form.addRow("Label (optional):", self.label_edit)
        layout.addLayout(form)

        self.error_lbl = QLabel("")
        self.error_lbl.setStyleSheet("color: palette(highlighted-text);")
        self.error_lbl.setVisible(False)
        layout.addWidget(self.error_lbl)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Cancel,
        )
        self.add_btn = buttons.addButton(
            "Add", QDialogButtonBox.AcceptRole,
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

    def _on_address_changed(self) -> None:
        """Light validation as the user types: enable Add only when
        the address looks like a 42-char 0x-prefixed hex string.
        Full checksum normalisation happens on accept so a
        lower-case paste is accepted."""
        text = self.address_edit.text().strip()
        ok = text.startswith("0x") and len(text) == 42
        try:
            int(text, 16)
        except ValueError:
            ok = False
        self.add_btn.setEnabled(ok)
        if not text:
            self.error_lbl.setVisible(False)
        if ok:
            # Restart the debounce timer so we only query ENS once
            # the user stops typing for 300 ms.
            self._ens_timer.start()
        else:
            self._ens_timer.stop()

    def _kick_ens_lookup(self) -> None:
        """Start a reverse-resolve against Ethereum mainnet. ENS
        lives on chain 1 regardless of which chain the user is
        currently viewing on; the resulting name is informational
        and used only to pre-populate the empty Label field."""
        text = self.address_edit.text().strip()
        if not (text.startswith("0x") and len(text) == 42):
            return
        from ..chains import DEFAULT_CHAINS
        from ..ens import EnsReverseWorker
        mainnet = next(
            (c for c in DEFAULT_CHAINS if c.chain_id == 1), None,
        )
        if mainnet is None:
            return
        worker = EnsReverseWorker(mainnet.rpc_url, text)
        worker.resolved.connect(self._on_ens_resolved)
        self._ens_workers.append(worker)
        worker.finished.connect(
            lambda w=worker: self._ens_workers.remove(w)
            if w in self._ens_workers else None
        )
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _on_ens_resolved(self, address: str, name: str) -> None:
        """Drop the verified ENS name into the Label field — only
        when the user is still looking at the same address (they
        may have edited further while the lookup was in flight)
        AND they haven't typed their own label, which we never
        overwrite."""
        if self.address_edit.text().strip().lower() != address.lower():
            return
        if self.label_edit.text().strip():
            return
        if name:
            self.label_edit.setText(name)

    def _on_accept(self) -> None:
        from eth_utils import to_checksum_address
        text = self.address_edit.text().strip()
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


# --- Right-hand details panel ------------------------------------------------

