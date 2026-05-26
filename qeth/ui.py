"""qeth.ui — top-level MainWindow shell.

The three topic plugins (Wallets, Tokens, Transactions) and their
widgets / workers live in ``qeth.plugins.tokens``, ``.plugins.transactions``
and ``.plugins.wallets``. This module just orchestrates: instantiates
the plugins, mounts them in two slots, wires cross-plugin signals,
and handles geometry persistence."""

from PySide6.QtCore import QByteArray, QSize, Qt, QThread
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QComboBox, QLabel, QMainWindow, QSplitter, QStatusBar,
)

from .icons import bundled_chain_icon
from .plugin import Slot
from .plugins.tokens import TokensPlugin
from .plugins.transactions import (
    SignTransactionDialog, TransactionsPlugin,
)
from .plugins.wallets import WalletsPlugin
from .signing import SignAndBroadcastWorker, SignerBridge, SignerError


class MainWindow(QMainWindow):
    """Top-level shell. Owns the store + RPC server, instantiates the
    three plugins (Wallets / Tokens / Transactions) into two slots,
    and wires cross-plugin signals. Almost no domain logic lives here
    anymore — each topic owns its own state and workers."""

    def __init__(self, store, rpc):
        super().__init__()
        self.store = store
        self.rpc = rpc
        self.setWindowTitle("qeth — Ethereum wallet")
        self.resize(1060, 720)
        # Override QMainWindow's inflated minimumSizeHint (it reports
        # ~950x565 even when child widgets only need ~370x500).
        self.setMinimumSize(420, 360)

        # Topic plugins. Each owns its sources/caches/workers/widgets.
        self.wallets_plugin = WalletsPlugin(self.store)
        self.tokens_plugin = TokensPlugin(self.store)
        self.transactions_plugin = TransactionsPlugin()

        # Signing bridge: the RPC server hands incoming signing
        # requests through this object, the slot below opens the
        # confirmation dialog on the main thread, and the bridge's
        # future is resolved with the broadcast tx hash (or rejected
        # with a SignerError). Parent it to MainWindow so the QObject
        # lives on the main thread and cross-thread emit auto-queues.
        self.signer_bridge = SignerBridge(parent=self)
        self.signer_bridge.request_received.connect(
            self._on_signing_request,
            type=Qt.QueuedConnection,
        )
        if self.rpc is not None:
            self.rpc.signer_bridge = self.signer_bridge

        # Workers tracked here so Python doesn't GC them while running
        # (Qt's QThread destructor aborts the process if the thread is
        # alive). Plugins register workers via host.start_worker(...);
        # they self-evict via the ``finished`` signal.
        self._active_workers: set[QThread] = set()

        self._build_central()
        self._build_statusbar()

        # Wallets is the source of the selection broadcast: when the
        # user picks an account, the plugin emits this signal and we
        # forward it to the right slot's mounted plugins (Tokens,
        # Transactions). Default-account changes refresh the status bar.
        self.wallets_plugin.selected_address_changed.connect(
            self.right_slot.broadcast_account_changed
        )
        self.wallets_plugin.default_account_changed.connect(self._refresh_status)
        self._refresh_status()
        # Replay the current selection. _build_central() above mounted
        # the plugins, which built their widgets, which rebuilt the
        # wallet tree and auto-selected the default account — emitting
        # selected_address_changed before we connected to it just now.
        # Without this nudge the right-slot plugins would sit empty
        # until TokenListsLoader finishes and re-triggers a refresh,
        # producing a visible "gone for a sec" gap on startup.
        initial_addr = self.wallets_plugin.selected_address
        if initial_addr is not None:
            self.right_slot.broadcast_account_changed(initial_addr)

        # Restore prior window geometry + splitter states.
        if self.store.window_geometry:
            try:
                self.restoreGeometry(
                    QByteArray.fromHex(self.store.window_geometry.encode())
                )
            except Exception:
                pass
        if self.store.splitter_state_main:
            try:
                self._splitter_outer.restoreState(
                    QByteArray.fromHex(self.store.splitter_state_main.encode())
                )
            except Exception:
                pass
        if self.store.splitter_state_left:
            self.wallets_plugin.restore_splitter_state(
                self.store.splitter_state_left
            )
        # Restore each panel's column layout (widths, order, sort
        # indicator) from the per-plugin map in the store.
        for name, plugin in self._header_persisters().items():
            saved = self.store.get_header_state(name)
            if saved:
                plugin.restore_header_state(saved)

    def _header_persisters(self) -> dict:
        """Plugins whose panel layouts we persist across runs.
        Wallets uses a tree (not a QTableWidget); Transactions uses
        auto-sizing modes (no user-drag, nothing to remember). Only
        the Tokens panel has interactive widths worth persisting."""
        return {
            "tokens": self.tokens_plugin,
        }

    def closeEvent(self, event):
        self.store.set_window_geometry(bytes(self.saveGeometry().toHex()).decode())
        self.store.set_splitter_states(
            bytes(self._splitter_outer.saveState().toHex()).decode(),
            self.wallets_plugin.splitter_state(),
        )
        for name, plugin in self._header_persisters().items():
            state = plugin.header_state()
            if state:
                self.store.set_header_state(name, state)
        super().closeEvent(event)

    def _build_chain_combo(self) -> QComboBox:
        combo = QComboBox()
        combo.setIconSize(QSize(18, 18))
        for c in self.store.chains:
            label = f"{c.name} ({c.chain_id})"
            pix = bundled_chain_icon(c.chain_id)
            if pix is not None:
                combo.addItem(QIcon(pix), label, c.chain_id)
            else:
                combo.addItem(label, c.chain_id)
        idx = combo.findData(self.store.current_chain_id)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(self._on_chain_changed)
        return combo

    def _build_central(self) -> None:
        self._splitter_outer = outer = QSplitter(Qt.Horizontal)

        # Left slot: Wallets only. Single-plugin → no tab bar visible.
        self.left_slot = Slot()
        self.left_slot.add_plugin(self.wallets_plugin, self)
        outer.addWidget(self.left_slot)

        # Right slot: Tokens + Transactions. Tab bar visible.
        self.right_slot = Slot()
        self.right_slot.add_plugin(self.tokens_plugin, self)
        self.right_slot.add_plugin(self.transactions_plugin, self)
        self.chain_combo = self._build_chain_combo()
        self.right_slot.add_shared_widget(self.chain_combo)
        outer.addWidget(self.right_slot)

        outer.setStretchFactor(0, 1)
        outer.setStretchFactor(1, 1)
        outer.setSizes([480, 580])

        self.setCentralWidget(outer)

    def _build_statusbar(self) -> None:
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.rpc_label = QLabel()
        self.default_label = QLabel()
        sb.addWidget(self.rpc_label, 1)
        sb.addPermanentWidget(self.default_label)

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

    # --- Host protocol (consumed by plugins via plugin.attach) -----------

    @property
    def selected_address(self) -> "str | None":
        return self.wallets_plugin.selected_address

    def current_chain(self):
        return self.store.current_chain()

    def chain_by_id(self, chain_id: int):
        for c in self.store.chains:
            if c.chain_id == chain_id:
                return c
        return None

    def start_worker(self, worker: QThread) -> QThread:
        """Track a worker so Python doesn't GC it while running."""
        self._active_workers.add(worker)
        worker.finished.connect(lambda w=worker: self._active_workers.discard(w))
        worker.finished.connect(worker.deleteLater)
        worker.start()
        return worker

    def status_message(self, text: str, timeout_ms: int = 3000) -> None:
        self.statusBar().showMessage(text, timeout_ms)

    def token_info(self, chain_id: int, address: str):
        return self.tokens_plugin.token_lists.get(chain_id, address)

    def icon_cache(self):
        return self.tokens_plugin.icon_cache

    def native_price_usd(self, chain_id, address):
        if not address:
            return None
        try:
            cached = self.tokens_plugin._wallet_cache.load(chain_id, address)
        except Exception:
            return None
        if cached is None or not cached.native_price_usd:
            return None
        from decimal import Decimal
        return Decimal(cached.native_price_usd)

    # --- signing -------------------------------------------------------

    def _on_signing_request(self, req, fut) -> None:
        """Slot for ``SignerBridge.request_received`` — runs on the
        Qt main thread (queued connection). Looks up the right
        Chain from the RPC's session view, opens the dialog, and
        resolves / rejects the future. Phase 1: actual signing isn't
        wired in yet, so confirmed requests still reject with a
        placeholder error — the goal of this phase is to validate
        the round-trip plumbing."""
        chain = next(
            (c for c in self.store.chains if c.chain_id == req.chain_id),
            None,
        )
        if chain is None:
            self.signer_bridge.reject(
                fut, SignerError(f"Unknown chain {req.chain_id}"),
            )
            return
        # Native USD price (used by the dialog to annotate the
        # expected-fee line with a "(… USD)" suffix). Cache miss or
        # no entry yet → None and the dialog quietly omits the
        # parenthetical.
        native_price_usd = self.native_price_usd(
            chain.chain_id, req.from_addr,
        )
        dialog = SignTransactionDialog(
            req, chain,
            abi_source=self.transactions_plugin._abi_source,
            abi_cache=self.transactions_plugin._abi_cache,
            start_worker=self.start_worker,
            token_info=self.token_info,
            native_price_usd=native_price_usd,
            parent=self,
        )
        if dialog.exec() != SignTransactionDialog.Accepted:
            self.signer_bridge.reject(fut, SignerError("User cancelled"))
            return
        try:
            finalised = dialog.finalised_request()
        except SignerError as e:
            self.signer_bridge.reject(fut, e)
            return

        # Phase 2: actual sign + broadcast via the Ledger backend.
        # Future signers (hot wallet, watch-only) plug into the same
        # Signer ABC; pick the right backend based on the source on
        # the account record.
        from .ledger import LedgerSigner, prompt_until_ledger_ready
        signer = LedgerSigner(self.store)
        if not signer.can_sign(finalised.from_addr):
            self.signer_bridge.reject(
                fut,
                SignerError(
                    f"No known signer for {finalised.from_addr}"
                ),
            )
            return
        # Probe the device upfront so a disconnected / locked Ledger
        # surfaces as a "Try again" prompt instead of a silent
        # failure inside the worker (which would just bounce a
        # -32000 back to the dapp; the user would have to redo the
        # whole dapp interaction).
        if not prompt_until_ledger_ready(self):
            self.signer_bridge.reject(
                fut, SignerError("User cancelled — Ledger unavailable"),
            )
            return

        # Modal "waiting for device" — non-cancellable; the dapp on
        # the other end of the RPC will time out on its own if the
        # user walks away.
        from PySide6.QtWidgets import QProgressDialog
        progress = QProgressDialog(
            "Confirm the transaction on your Ledger device…",
            None,           # no cancel button
            0, 0,           # indeterminate / spinner
            self,
        )
        progress.setWindowTitle("Signing transaction")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        worker = SignAndBroadcastWorker(signer, finalised, chain)
        worker.broadcast.connect(
            lambda h, p=progress, f=fut, r=finalised, c=chain:
                self._on_tx_broadcast(h, p, f, r, c)
        )
        worker.failed.connect(
            lambda msg, p=progress, f=fut: self._on_tx_sign_failed(msg, p, f)
        )
        self.start_worker(worker)

    def _on_tx_broadcast(self, tx_hash, progress, fut, req, chain) -> None:
        progress.close()
        self.signer_bridge.resolve(fut, tx_hash)
        # Snapshot the just-sent tx into the transactions list as a
        # pending row so the user sees it immediately — without
        # waiting for Blockscout indexing (it lags mempool by tens of
        # seconds). The plugin's PendingTxWatcher polls the receipt
        # and flips the row to confirmed when the tx mines.
        try:
            self.transactions_plugin.add_pending(tx_hash, req, chain)
        except Exception:
            import logging
            logging.getLogger("qeth.ui").exception("add_pending failed")
        # Make the pending row visible: select the ``from`` account
        # in the wallets tree (pending lives in that account's bucket)
        # and flip the right slot to the Transactions tab. Both are
        # idempotent if we're already in the right place.
        self.wallets_plugin.select_address(req.from_addr)
        self.right_slot.set_active(self.transactions_plugin)
        self.status_message(f"Broadcast {tx_hash}", 6000)

    def _on_tx_sign_failed(self, msg: str, progress, fut) -> None:
        progress.close()
        self.signer_bridge.reject(fut, SignerError(msg))
        self.status_message(f"Signing failed: {msg}", 6000)

    # --- transitional aliases (kept so existing tests / external code
    # that pokes at the panels directly keeps working).

    @property
    def token_panel(self):
        return self.tokens_plugin.widget()

    @property
    def tx_panel(self):
        return self.transactions_plugin.widget()

    @property
    def tree(self):
        return self.wallets_plugin._tree

    @property
    def details(self):
        return self.wallets_plugin._details

    @property
    def act_add(self):
        return self.wallets_plugin.act_add

    @property
    def act_copy(self):
        return self.wallets_plugin.act_copy

    @property
    def act_remove(self):
        return self.wallets_plugin.act_remove

    def _on_chain_changed(self, idx: int) -> None:
        cid = self.chain_combo.itemData(idx)
        if cid is not None:
            self.store.set_current_chain(int(cid))
            self._refresh_status()
            self.right_slot.broadcast_chain_changed()
