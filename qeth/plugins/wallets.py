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

from PySide6.QtCore import QByteArray, QSize, Qt, Signal
from PySide6.QtGui import QAction, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton,
    QSizePolicy, QSpinBox, QSplitter, QStyle, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtCore import QThread

from ..chain import wei_to_ether
from ..ledger import DiscoveredAccount, LedgerWorker, PATH_SCHEMES
from ..plugin import Plugin


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


# --- DetailsPanel + AddLedgerDialog (moved from qeth.ui) -------------------

class DetailsPanel(QWidget):
    set_default_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        # No bottom margin so the Set-as-default button can sit flush with
        # the bottom of the splitter (matches the right panel's bottom edge).
        v.setContentsMargins(9, 9, 9, 0)
        self.title = QLabel("Select an account on the left")
        f = self.title.font(); f.setPointSize(f.pointSize() + 2); f.setBold(True)
        self.title.setFont(f)
        v.addWidget(self.title)

        form = QFormLayout()
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
        self.set_default_btn = QPushButton("Set as default")
        self.set_default_btn.setToolTip(
            "Make this the address dapps see (returned by eth_accounts)"
        )
        self.set_default_btn.setEnabled(False)
        # Pin the height. With QSizePolicy.Fixed Qt re-queries sizeHint()
        # every time the text changes — and "Default ✓" can come out a
        # touch shorter than "Set as default" depending on the theme. The
        # snapshot here is taken while the (longer) text is set, so the
        # disabled state never shrinks.
        self.set_default_btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.set_default_btn.setMinimumHeight(self.set_default_btn.sizeHint().height())
        self.set_default_btn.clicked.connect(
            lambda: self._current and self.set_default_requested.emit(self._current)
        )
        # Stretch pushes the button to the very bottom of the panel.
        v.addStretch(1)
        v.addWidget(self.set_default_btn)
        self._current: str | None = None

    def show_account(self, account: dict, is_default: bool) -> None:
        self._current = account["address"]
        self.title.setText(account.get("label") or "Account")
        self.address_lbl.setText(account["address"])
        self.path_lbl.setText(account.get("path", "—"))
        self.source_lbl.setText(account.get("source", "—"))
        self.scheme_lbl.setText(account.get("scheme", "—"))
        self.set_default_btn.setEnabled(not is_default)
        self.set_default_btn.setText("Default ✓" if is_default else "Set as default")
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
        self.title.setText("Select an account on the left")
        for w in (self.address_lbl, self.path_lbl, self.source_lbl, self.scheme_lbl):
            w.setText("—")
        self.qr_lbl.clear()
        self.set_default_btn.setEnabled(False)
        self.set_default_btn.setText("Set as default")


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

        layout.addWidget(QLabel(
            "Connect your Ledger, unlock it, and open the Ethereum app, then click Scan.\n"
            f"Balances are queried on {chain.name}; non-empty accounts are pre-selected."
        ))

        self.results = QListWidget()
        self.results.setSelectionMode(QAbstractItemView.ExtendedSelection)
        font = QFont("monospace")
        self.results.setFont(font)
        layout.addWidget(self.results, 1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        btns = QHBoxLayout()
        self.scan_btn = QPushButton("Scan")
        self.add_btn = QPushButton("Add selected")
        self.add_btn.setEnabled(False)
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
        balance = wei_to_ether(acct.balance_wei)
        label = (
            f"#{acct.index:<3} {acct.address}   "
            f"{balance:.6f} {self._chain.symbol}"
        )
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, acct)
        item.setSelected(acct.balance_wei > 0)
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


# --- Right-hand details panel ------------------------------------------------

