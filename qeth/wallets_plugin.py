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

from PySide6.QtCore import QByteArray, Qt, Signal
from PySide6.QtGui import QAction, QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QFrame, QHBoxLayout, QMessageBox,
    QSizePolicy, QSplitter, QStyle, QToolButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)
from PySide6.QtCore import QSize

from .plugin import Plugin


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
        # Local import to avoid a Qt UI import at module load.
        from .ui import DetailsPanel

        self._container = QWidget()
        v = QVBoxLayout(self._container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Top: account action row.
        v.addLayout(self._build_account_actions())

        # Middle: vertical splitter (tree on top, details on bottom).
        self._splitter = QSplitter(Qt.Vertical)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Accounts"])
        self._tree.setRootIsDecorated(True)
        self._tree.setTextElideMode(Qt.ElideMiddle)
        self._tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._tree.itemSelectionChanged.connect(self._on_tree_selection)
        self._splitter.addWidget(self._tree)

        self._details = DetailsPanel()
        self._details.set_default_requested.connect(self._set_default)
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
        self.act_add.setToolTip("Add Ledger accounts by scanning derivation paths")
        self.act_add.triggered.connect(self._add_ledger)

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
        for act in (self.act_add, self.act_copy, self.act_remove):
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

    # --- tree population ----------------------------------------------------

    def _rebuild_tree(self) -> None:
        if self._tree is None:
            return
        self._tree.clear()
        ledger_accts = [a for a in self._store.accounts if a.get("source") == "ledger"]
        ledger_root = QTreeWidgetItem([f"Ledger ({len(ledger_accts)})"])
        self._tree.addTopLevelItem(ledger_root)
        groups: dict[str, QTreeWidgetItem] = {}
        default_item: Optional[QTreeWidgetItem] = None
        for a in ledger_accts:
            scheme = a.get("scheme", "Custom")
            grp = groups.get(scheme)
            if grp is None:
                grp = QTreeWidgetItem([scheme])
                ledger_root.addChild(grp)
                groups[scheme] = grp
            addr = a["address"]
            is_default = (
                self._store.default_account is not None
                and addr.lower() == self._store.default_account.lower()
            )
            label = f"[{addr}]" if is_default else f" {addr} "
            it = QTreeWidgetItem([label])
            it.setData(0, Qt.UserRole, addr)
            it.setFont(0, QFont("monospace"))
            grp.addChild(it)
            if is_default:
                default_item = it
        ledger_root.setExpanded(True)
        for g in groups.values():
            g.setExpanded(True)

        # Stubs for the future
        self._tree.addTopLevelItem(QTreeWidgetItem(["Hot wallet (0)"]))
        self._tree.addTopLevelItem(QTreeWidgetItem(["Watch only (0)"]))

        if default_item is not None:
            self._tree.setCurrentItem(default_item)

    # --- selection / action handlers ---------------------------------------

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
        # Local import: AddLedgerDialog drags in DetailsPanel / Ledger
        # bits that we shouldn't load until the user actually clicks.
        from .ui import AddLedgerDialog

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

    def _set_default(self, address: str) -> None:
        self._store.set_default_account(address)
        self._rebuild_tree()
        self.default_account_changed.emit()
        # Re-run selection to refresh the details-panel button state.
        self._on_tree_selection()
