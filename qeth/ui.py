from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QSpinBox, QSplitter, QStatusBar, QToolBar, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from .ledger import DiscoveredAccount, LedgerWorker, PATH_SCHEMES


# --- Add Ledger dialog -------------------------------------------------------

class AddLedgerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Ledger accounts")
        self.setMinimumSize(560, 420)

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self.scheme_combo = QComboBox()
        self.scheme_combo.addItems(list(PATH_SCHEMES.keys()))
        form.addRow("Derivation scheme:", self.scheme_combo)
        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 50)
        self.count_spin.setValue(5)
        form.addRow("Accounts to scan:", self.count_spin)
        layout.addLayout(form)

        layout.addWidget(QLabel(
            "Connect your Ledger, unlock it, and open the Ethereum app, then click Scan."
        ))

        self.results = QListWidget()
        self.results.setSelectionMode(QListWidget.MultiSelection)
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

        self._worker: LedgerWorker | None = None

    def _scan(self) -> None:
        self.results.clear()
        self.add_btn.setEnabled(False)
        n = self.count_spin.value()
        self.progress.setRange(0, n)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.scan_btn.setEnabled(False)
        self._worker = LedgerWorker(self.scheme_combo.currentText(), n)
        self._worker.discovered.connect(self._on_found)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_found(self, acct: DiscoveredAccount) -> None:
        item = QListWidgetItem(f"#{acct.index:<3} {acct.address}   {acct.path}")
        item.setData(Qt.UserRole, acct)
        item.setSelected(True)
        self.results.addItem(item)
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

class DetailsPanel(QWidget):
    set_default_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        self.title = QLabel("Select an account on the left")
        f = self.title.font(); f.setPointSize(f.pointSize() + 2); f.setBold(True)
        self.title.setFont(f)
        v.addWidget(self.title)

        form = QFormLayout()
        mono = QFont("monospace")
        self.address_lbl = QLabel("—"); self.address_lbl.setFont(mono)
        self.address_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_lbl = QLabel("—"); self.path_lbl.setFont(mono)
        self.source_lbl = QLabel("—")
        self.scheme_lbl = QLabel("—")
        form.addRow("Address:", self.address_lbl)
        form.addRow("Path:", self.path_lbl)
        form.addRow("Source:", self.source_lbl)
        form.addRow("Scheme:", self.scheme_lbl)
        v.addLayout(form)

        self.set_default_btn = QPushButton("Set as default (exposed to dapps)")
        self.set_default_btn.setEnabled(False)
        self.set_default_btn.clicked.connect(
            lambda: self._current and self.set_default_requested.emit(self._current)
        )
        v.addWidget(self.set_default_btn)
        v.addStretch(1)
        self._current: str | None = None

    def show_account(self, account: dict, is_default: bool) -> None:
        self._current = account["address"]
        self.title.setText(account.get("label") or "Account")
        self.address_lbl.setText(account["address"])
        self.path_lbl.setText(account.get("path", "—"))
        self.source_lbl.setText(account.get("source", "—"))
        self.scheme_lbl.setText(account.get("scheme", "—"))
        self.set_default_btn.setEnabled(not is_default)
        self.set_default_btn.setText(
            "Default ✓" if is_default else "Set as default (exposed to dapps)"
        )

    def clear(self) -> None:
        self._current = None
        self.title.setText("Select an account on the left")
        for w in (self.address_lbl, self.path_lbl, self.source_lbl, self.scheme_lbl):
            w.setText("—")
        self.set_default_btn.setEnabled(False)
        self.set_default_btn.setText("Set as default (exposed to dapps)")


# --- Main window -------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, store, rpc):
        super().__init__()
        self.store = store
        self.rpc = rpc
        self.setWindowTitle("qeth — Ethereum wallet")
        self.resize(1024, 600)

        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self._rebuild_tree()
        self._refresh_status()

    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
        act_add = QAction("Add Ledger account", self)
        act_add.triggered.connect(self._add_ledger)
        tb.addAction(act_add)
        tb.addSeparator()
        tb.addWidget(QLabel("  Chain: "))
        self.chain_combo = QComboBox()
        for c in self.store.chains:
            self.chain_combo.addItem(f"{c.name} ({c.chain_id})", c.chain_id)
        idx = self.chain_combo.findData(self.store.current_chain_id)
        if idx >= 0:
            self.chain_combo.setCurrentIndex(idx)
        self.chain_combo.currentIndexChanged.connect(self._on_chain_changed)
        tb.addWidget(self.chain_combo)

    def _build_central(self) -> None:
        splitter = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Accounts"])
        self.tree.setRootIsDecorated(True)
        self.tree.itemSelectionChanged.connect(self._on_tree_selection)
        splitter.addWidget(self.tree)

        self.details = DetailsPanel()
        self.details.set_default_requested.connect(self._set_default)
        wrap = QFrame()
        wrap.setFrameShape(QFrame.StyledPanel)
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.addWidget(self.details)
        splitter.addWidget(wrap)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([460, 560])
        self.setCentralWidget(splitter)

    def _build_statusbar(self) -> None:
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.rpc_label = QLabel()
        self.default_label = QLabel()
        sb.addWidget(self.rpc_label, 1)
        sb.addPermanentWidget(self.default_label)

    def _rebuild_tree(self) -> None:
        self.tree.clear()
        ledger_accts = [a for a in self.store.accounts if a.get("source") == "ledger"]
        ledger_root = QTreeWidgetItem([f"Ledger ({len(ledger_accts)})"])
        self.tree.addTopLevelItem(ledger_root)
        groups: dict[str, QTreeWidgetItem] = {}
        default_item: QTreeWidgetItem | None = None
        for a in ledger_accts:
            scheme = a.get("scheme", "Custom")
            grp = groups.get(scheme)
            if grp is None:
                grp = QTreeWidgetItem([scheme])
                ledger_root.addChild(grp)
                groups[scheme] = grp
            addr = a["address"]
            it = QTreeWidgetItem([addr])
            it.setData(0, Qt.UserRole, addr)
            it.setFont(0, QFont("monospace"))
            grp.addChild(it)
            if addr == self.store.default_account:
                default_item = it
        ledger_root.setExpanded(True)
        for g in groups.values():
            g.setExpanded(True)

        # Stubs for the future
        self.tree.addTopLevelItem(QTreeWidgetItem(["Hot wallet (0)"]))
        self.tree.addTopLevelItem(QTreeWidgetItem(["Watch only (0)"]))

        if default_item is not None:
            self.tree.setCurrentItem(default_item)

    def _refresh_status(self) -> None:
        err = self.rpc.error
        if err:
            self.rpc_label.setText(f"JSON-RPC: error — {err}")
        else:
            self.rpc_label.setText(
                f"JSON-RPC: http://{self.rpc.host}:{self.rpc.port}   (ws on same port)"
            )
        addr = self.store.default_account or "none"
        self.default_label.setText(
            f"Default: {addr}   •   {self.store.current_chain().name}"
        )

    def _on_tree_selection(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            self.details.clear()
            return
        addr = items[0].data(0, Qt.UserRole)
        if not addr:
            self.details.clear()
            return
        acct = next((a for a in self.store.accounts if a["address"] == addr), None)
        if acct:
            self.details.show_account(acct, is_default=(addr == self.store.default_account))
        else:
            self.details.clear()

    def _add_ledger(self) -> None:
        dlg = AddLedgerDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        scheme = dlg.scheme_combo.currentText()
        added = 0
        for d in dlg.selected_accounts():
            if self.store.add_account({
                "address": d.address,
                "path": d.path,
                "source": "ledger",
                "scheme": scheme,
                "label": "",
            }):
                added += 1
        self._rebuild_tree()
        self._refresh_status()
        if added:
            self.statusBar().showMessage(f"Added {added} account(s)", 3000)

    def _on_chain_changed(self, idx: int) -> None:
        cid = self.chain_combo.itemData(idx)
        if cid is not None:
            self.store.set_current_chain(int(cid))
            self._refresh_status()

    def _set_default(self, address: str) -> None:
        self.store.set_default_account(address)
        self._rebuild_tree()
        self._refresh_status()
        self._on_tree_selection()
