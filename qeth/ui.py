"""qeth.ui — top-level MainWindow shell.

The three topic plugins (Wallets, Tokens, Transactions) and their
widgets / workers live in ``qeth.plugins.tokens``, ``.plugins.transactions``
and ``.plugins.wallets``. This module just orchestrates: instantiates
the plugins, mounts them in two slots, wires cross-plugin signals,
and handles geometry persistence."""

from PySide6.QtCore import (
    QByteArray, QEvent, QObject, QSize, Qt, QThread,
)
from PySide6.QtGui import QIcon, QKeyEvent
from PySide6.QtWidgets import (
    QComboBox, QLabel, QMainWindow, QSplitter, QStatusBar,
    QTableWidget, QTreeWidget,
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
        self.wallets_plugin.default_account_changed.connect(
            self._push_accounts_changed
        )
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
        self._restrict_tab_focus_to_lists()

    def _restrict_tab_focus_to_lists(self) -> None:
        """Tab-stops in the main window are exactly the three
        primary lists — wallet tree, tokens table, transactions
        table. Everything else (toolbar buttons, combos, label
        edit, tab bar) keeps mouse focus but is skipped by the
        Tab key.

        Tokens and Transactions live in the same right-slot tab
        widget, so only ONE of their tables is visible at any
        moment. Qt's native Tab walks the visible-widget chain,
        which collapses to two stops (wallet + whichever right
        table is active). We want three logical stops, so we
        install an event filter on each list to handle Tab /
        Shift+Tab manually: jumping into Transactions activates
        its tab in the right slot first, then focuses its table
        (and vice versa).

        Dialogs are unaffected — they're independent top-level
        windows with their own tab orders. Idempotent: we call
        this once at the end of ``_build_central``."""
        from PySide6.QtWidgets import QWidget
        tab_stops = self._collect_tab_stops()
        if not tab_stops:
            return
        central = self.centralWidget()
        if central is not None:
            for w in central.findChildren(QWidget):
                if w in tab_stops:
                    w.setFocusPolicy(Qt.StrongFocus)
                    continue
                # Don't touch widgets that live INSIDE one of the
                # tab-stop lists. QTreeWidget / QTableWidget use
                # an internal viewport / header / scrollbar chain
                # as focus proxies; calling
                # ``setFocusPolicy(ClickFocus)`` on those silently
                # demotes the tree itself back to ClickFocus,
                # undoing the StrongFocus we just set.
                if any(w is t or _is_descendant(w, t) for t in tab_stops):
                    continue
                w.setFocusPolicy(Qt.ClickFocus)
        # Native tab-order chain (useful if focus arrives via
        # Qt-internal navigation, not just our filter).
        for prev, nxt in zip(tab_stops, tab_stops[1:]):
            QWidget.setTabOrder(prev, nxt)
        # Event filter intercepts Tab / Shift+Tab on each list to
        # cycle between panels — including activating the right-
        # slot tab so a hidden table becomes visible before
        # receiving focus.
        self._tab_cycle_filter = _TabCycleFilter(self, tab_stops)
        for w in tab_stops:
            w.installEventFilter(self._tab_cycle_filter)
        # Norton-Commander cursor style: focused list paints
        # solid selection (default), unfocused list paints
        # outline-only. Delegate handles the cell-by-cell paint;
        # the table stylesheets no longer carry a ``:selected``
        # rule so they don't beat us.
        for w in tab_stops:
            _apply_focus_aware_selection(w)
        # Initial focus on the wallet tree so arrow keys work
        # immediately.
        tab_stops[0].setFocus(Qt.OtherFocusReason)

    def _collect_tab_stops(self) -> list:
        """Resolve the three list widgets safely (each may be None
        in tests where the plugins were stubbed). Returns the list
        in tab-cycle order."""
        out = []
        tree = getattr(self.wallets_plugin, "_tree", None)
        if tree is not None:
            out.append(tree)
        tpanel = getattr(self.tokens_plugin, "_panel", None)
        if tpanel is not None and hasattr(tpanel, "table"):
            out.append(tpanel.table)
        xpanel = getattr(self.transactions_plugin, "_panel", None)
        if xpanel is not None and hasattr(xpanel, "table"):
            out.append(xpanel.table)
        return out

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
        Qt main thread (queued connection). Builds the dialog and
        wires its lifecycle async (no ``exec``), so failures keep
        the dialog open and surface a popup parented to it. The
        bridge future is resolved / rejected from the worker
        callbacks below.

        ``req`` is one of: ``SigningRequest`` (transactions),
        ``MessageSigningRequest`` (personal_sign), or
        ``TypedDataSigningRequest`` (EIP-712). Dispatch by type."""
        from .signing import (
            MessageSigningRequest as _MR, TypedDataSigningRequest as _TR,
        )
        if isinstance(req, (_MR, _TR)):
            self._launch_message_sign(req, fut)
            return
        chain = next(
            (c for c in self.store.chains if c.chain_id == req.chain_id),
            None,
        )
        if chain is None:
            self.signer_bridge.reject(
                fut, SignerError(f"Unknown chain {req.chain_id}"),
            )
            return
        # Native USD price for the expected-fee line. Cache miss →
        # None and the dialog quietly omits the parenthetical.
        native_price_usd = self.native_price_usd(
            chain.chain_id, req.from_addr,
        )
        dialog = SignTransactionDialog(
            req, chain,
            abi_source=self.transactions_plugin._abi_source,
            abi_cache=self.transactions_plugin._abi_cache,
            start_worker=self.start_worker,
            token_info=self.token_info,
            icon_cache=self.icon_cache(),
            native_price_usd=native_price_usd,
            parent=self,
        )
        self._launch_sign_flow(
            dialog, chain,
            on_broadcast=lambda h: self.signer_bridge.resolve(fut, h),
            on_cancel=lambda: self.signer_bridge.reject(
                fut, SignerError("User cancelled"),
            ),
            on_fail=lambda msg: self.signer_bridge.reject(
                fut, SignerError(msg),
            ),
        )

    def _launch_sign_flow(self, dialog, chain, *,
                          on_broadcast, on_cancel, on_fail) -> None:
        """Wire a sign-style dialog (SignTransactionDialog or
        SendTokenDialog — both expose ``sign_requested`` /
        ``finalised_request`` / ``set_signing_in_progress`` /
        ``accept``) to the worker pipeline. Callbacks fire on
        broadcast success / dialog cancel / signing failure so the
        same code path serves both the RPC-driven and the locally
        UI-driven signing flows."""
        dialog.setWindowModality(Qt.WindowModal)
        dialog.sign_requested.connect(
            lambda d=dialog, c=chain, ob=on_broadcast, of=on_fail:
                self._begin_sign(d, c, ob, of)
        )
        dialog.rejected.connect(on_cancel)
        dialog.show()

    def open_send_dialog(self, asset: dict, chain, from_addr: str) -> None:
        """Host-facing entry point used by TokensPlugin's Send
        button. Opens SendTokenDialog and runs the same worker
        pipeline as the RPC flow; success / cancel / failure
        produce status-bar messages (no bridge future)."""
        from .plugins.transactions import SendTokenDialog
        dialog = SendTokenDialog(
            asset, chain, from_addr,
            abi_source=self.transactions_plugin._abi_source,
            abi_cache=self.transactions_plugin._abi_cache,
            start_worker=self.start_worker,
            token_info=self.token_info,
            icon_cache=self.icon_cache(),
            native_price_usd=self.native_price_usd(
                chain.chain_id, from_addr,
            ),
            parent=self,
        )
        self._launch_sign_flow(
            dialog, chain,
            on_broadcast=lambda h: self.status_message(
                f"Broadcast {h}", 6000,
            ),
            on_cancel=lambda: None,
            on_fail=lambda msg: self.status_message(
                f"Send failed: {msg}", 6000,
            ),
        )

    def _begin_sign(self, dialog, chain, on_broadcast, on_fail) -> None:
        """Start one sign-and-broadcast attempt. Called every time
        the user clicks Confirm — including retries after a
        previous attempt failed. ``on_broadcast(tx_hash)`` /
        ``on_fail(msg)`` let the caller hook in (the RPC path
        resolves / rejects the bridge future; the local Send path
        emits status messages)."""
        try:
            finalised = dialog.finalised_request()
        except SignerError as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(dialog, "Cannot sign", str(e))
            return

        # Pick the right Signer based on the stored account record.
        # Hot wallets need a passphrase prompt up-front (on the
        # main thread) so the worker has the decrypted key by the
        # time it calls signer.sign().
        addr_lower = finalised.from_addr.lower()
        acct = next(
            (a for a in self.store.accounts
             if a["address"].lower() == addr_lower),
            None,
        )
        source = acct.get("source") if acct else None
        from PySide6.QtWidgets import QMessageBox
        if source == "ledger":
            from .ledger import LedgerSigner
            signer = LedgerSigner(self.store)
            progress_text = "Confirm the transaction on your Ledger device…"
        elif source == "hot":
            from PySide6.QtWidgets import QInputDialog, QLineEdit
            passphrase, ok = QInputDialog.getText(
                dialog, "Hot wallet",
                f"Passphrase for {finalised.from_addr}:",
                QLineEdit.Password, "",
            )
            if not ok:
                return
            from .hot_wallet import HotWalletSigner
            signer = HotWalletSigner(self.store, passphrase)
            # Scrypt-derived key decrypt typically takes ~1 second.
            progress_text = "Decrypting keystore and signing…"
        else:
            QMessageBox.warning(
                dialog, "Cannot sign",
                f"No known signer for {finalised.from_addr}",
            )
            return
        if not signer.can_sign(finalised.from_addr):
            QMessageBox.warning(
                dialog, "Cannot sign",
                f"No known signer for {finalised.from_addr}",
            )
            return

        dialog.set_signing_in_progress(True)
        from PySide6.QtWidgets import QProgressDialog
        progress = QProgressDialog(
            progress_text,
            None,           # no cancel button
            0, 0,           # indeterminate spinner
            dialog,         # parent on the sign dialog so the
                            # progress sits on top of it.
        )
        progress.setWindowTitle("Signing transaction")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        worker = SignAndBroadcastWorker(signer, finalised, chain)
        worker.broadcast.connect(
            lambda h, d=dialog, p=progress, r=finalised, c=chain,
                   ob=on_broadcast:
                self._on_tx_broadcast(h, d, p, r, c, ob)
        )
        worker.failed.connect(
            lambda msg, d=dialog, p=progress, of=on_fail:
                self._on_tx_sign_failed(msg, d, p, of)
        )
        self.start_worker(worker)

    def _pick_signer_for(self, dialog, address: str):
        """Pick a Signer instance based on the account's source.
        For hot wallets, prompts for the passphrase on the main
        thread BEFORE returning (so the worker's slow scrypt
        decrypt can run off-thread). Returns
        ``(signer, progress_text)`` or ``(None, None)`` if the
        user cancelled the passphrase prompt / no signer exists.
        Shows its own QMessageBox for the "no signer" case."""
        from PySide6.QtWidgets import QMessageBox
        addr_lower = address.lower()
        acct = next(
            (a for a in self.store.accounts
             if a["address"].lower() == addr_lower),
            None,
        )
        source = acct.get("source") if acct else None
        if source == "ledger":
            from .ledger import LedgerSigner
            return (
                LedgerSigner(self.store),
                "Confirm on your Ledger device…",
            )
        if source == "hot":
            from PySide6.QtWidgets import QInputDialog, QLineEdit
            passphrase, ok = QInputDialog.getText(
                dialog, "Hot wallet",
                f"Passphrase for {address}:",
                QLineEdit.Password, "",
            )
            if not ok:
                return None, None
            from .hot_wallet import HotWalletSigner
            return (
                HotWalletSigner(self.store, passphrase),
                "Decrypting keystore and signing…",
            )
        QMessageBox.warning(
            dialog, "Cannot sign",
            f"No known signer for {address}",
        )
        return None, None

    def _launch_message_sign(self, req, fut) -> None:
        """Dapp-initiated personal_sign / eth_signTypedData_v4 flow.
        Mirror of ``_on_signing_request`` for transactions, but
        the worker is ``SignMessageWorker`` and the result is a
        signature hex string rather than a tx hash."""
        from .plugins.sign_message import SignMessageDialog
        dialog = SignMessageDialog(req, parent=self)

        def on_confirm():
            self._run_message_sign(
                req, dialog,
                on_signed=lambda sig: self.signer_bridge.resolve(fut, sig),
                on_fail=lambda msg: self.signer_bridge.reject(
                    fut, SignerError(msg),
                ),
            )

        dialog.sign_requested.connect(on_confirm)
        dialog.rejected.connect(
            lambda: self.signer_bridge.reject(
                fut, SignerError("User cancelled"),
            )
        )
        dialog.show()

    def _run_message_sign(self, req, dialog, *, on_signed, on_fail) -> None:
        """One signing attempt — picks the signer, prompts for
        passphrase if it's a hot wallet, kicks SignMessageWorker
        off the main thread. Errors keep the dialog open so the
        user can retry."""
        from .signing import SignMessageWorker
        from PySide6.QtWidgets import QMessageBox, QProgressDialog
        signer, progress_text = self._pick_signer_for(
            dialog, req.from_addr,
        )
        if signer is None:
            return
        if not signer.can_sign(req.from_addr):
            QMessageBox.warning(
                dialog, "Cannot sign",
                f"No known signer for {req.from_addr}",
            )
            return

        dialog.set_signing_in_progress(True)
        progress = QProgressDialog(
            progress_text, None, 0, 0, dialog,
        )
        progress.setWindowTitle("Signing message")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        worker = SignMessageWorker(signer, req)
        worker.signed.connect(
            lambda sig, d=dialog, p=progress, ok=on_signed:
                self._on_message_signed(sig, d, p, ok)
        )
        worker.failed.connect(
            lambda msg, d=dialog, p=progress, of=on_fail:
                self._on_message_sign_failed(msg, d, p, of)
        )
        self.start_worker(worker)

    def _on_message_signed(self, sig_hex, dialog, progress, on_signed):
        progress.close()
        dialog.accept()
        on_signed(sig_hex)

    def _on_message_sign_failed(self, msg, dialog, progress, on_fail):
        progress.close()
        dialog.set_signing_in_progress(False)
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(dialog, "Signing failed", msg)
        on_fail(msg)

    def open_sign_message_dialog(self, address: str) -> None:
        """User-initiated 'sign anything you paste' flow. Triggered
        from the details panel button. Opens
        ``ComposeMessageDialog`` to collect the payload; the
        compose dialog IS the review — the user just typed the
        text, no separate confirmation step needed. After signing,
        ``SignatureResultDialog`` shows the 0x-hex with a copy-
        to-clipboard button (no dapp to receive it
        automatically)."""
        from .plugins.sign_message import ComposeMessageDialog

        compose = ComposeMessageDialog(address, parent=self)
        compose.request_built.connect(self._sign_local_message)
        compose.show()

    def _sign_local_message(self, req) -> None:
        """Sign a locally-composed message (from
        ComposeMessageDialog). No review step — the user typed it
        themselves. Picks signer, prompts for passphrase if hot,
        runs SignMessageWorker, then shows the resulting
        signature."""
        from PySide6.QtWidgets import QMessageBox, QProgressDialog
        signer, progress_text = self._pick_signer_for(self, req.from_addr)
        if signer is None:
            return
        if not signer.can_sign(req.from_addr):
            QMessageBox.warning(
                self, "Cannot sign",
                f"No known signer for {req.from_addr}",
            )
            return

        progress = QProgressDialog(progress_text, None, 0, 0, self)
        progress.setWindowTitle("Signing message")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        from .signing import SignMessageWorker
        worker = SignMessageWorker(signer, req)
        worker.signed.connect(
            lambda sig, p=progress: self._on_local_message_signed(sig, p)
        )
        worker.failed.connect(
            lambda msg, p=progress: self._on_local_message_sign_failed(msg, p)
        )
        self.start_worker(worker)

    def _on_local_message_signed(self, signature_hex, progress) -> None:
        from .plugins.sign_message import SignatureResultDialog
        progress.close()
        dlg = SignatureResultDialog(signature_hex, parent=self)
        dlg.show()

    def _on_local_message_sign_failed(self, msg, progress) -> None:
        from PySide6.QtWidgets import QMessageBox
        progress.close()
        QMessageBox.warning(self, "Signing failed", msg)

    def _on_tx_broadcast(self, tx_hash, dialog, progress, req, chain,
                          on_broadcast) -> None:
        progress.close()
        dialog.accept()
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
        # Make the pending row visible: select the from account in
        # the wallets tree (pending lives in that account's bucket)
        # and flip the right slot to the Transactions tab. Both are
        # idempotent if we're already in the right place.
        self.wallets_plugin.select_address(req.from_addr)
        self.right_slot.set_active(self.transactions_plugin)
        on_broadcast(tx_hash)

    def _on_tx_sign_failed(self, msg: str, dialog, progress,
                            on_fail) -> None:
        """Signing failed (Ledger unavailable, user cancelled on
        device, broadcast rejected, …). Don't close the sign
        dialog — show the message on top of it and re-enable
        Confirm so the user can fix the device and retry without
        losing the dialog state."""
        progress.close()
        dialog.set_signing_in_progress(False)
        from PySide6.QtWidgets import QMessageBox
        # "Signing failed" is signer-neutral — Ledger AND hot
        # wallets flow through this handler, and broadcast failures
        # (e.g. insufficient funds) bubble up here too.
        QMessageBox.warning(dialog, "Signing failed", msg)
        on_fail(msg)

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
            # The UI chain is the user's preferred chain — also
            # update the dapp-facing RPC chain so connected dapps
            # see it (eth_chainId, eth_signTypedData_v4 domain
            # checks). Dapp-initiated wallet_switchEthereumChain
            # is the only thing that can override this asymmetric
            # link; UI ⇒ dapp, but dapp-switches stay session-only
            # and don't pull the UI back. So a user can browse on
            # Gnosis in the wallet, open Gnosis Pay, and have it
            # see chainId 100 immediately instead of getting
            # "provided 1" complaints.
            if self.rpc is not None:
                self.rpc.set_rpc_chain(int(cid))

    def _push_accounts_changed(self) -> None:
        """Slot for default_account_changed — push the EIP-1193
        accountsChanged notification to connected dapps. We send
        the default account (or empty) because that's what
        ``eth_accounts`` returns; matching the request/response
        shape keeps dapps consistent."""
        if self.rpc is None:
            return
        accounts = [self.store.default_account] if self.store.default_account else []
        self.rpc.broadcast_accounts_changed(accounts)


class _TabCycleFilter(QObject):
    """Keyboard navigation across the main window.

    Two responsibilities:

    1. **Tab / Shift+Tab** cycle between the wallet tree and the
       currently-visible right-slot table — two stops. The user
       sees one logical "other panel" at a time, so Tab swapping
       to a hidden table would be jarring; tab-switching is a
       separate gesture (see #2).

    2. **Left / Right** when focus is on the right-slot table
       switch the right slot's active tab (Tokens ↔ Transactions)
       and move focus to the newly-active table. Up/down still
       does its native selection-navigation thing.

    Behaves like a 2D grid: Tab is the left/right axis between
    panels; arrow keys are the up/down axis within a panel; left/
    right is the lateral switch between right-slot tabs."""

    def __init__(self, main_window, lists):
        super().__init__(main_window)
        self._mw = main_window
        self._lists = lists

    def eventFilter(self, obj, event):
        if event.type() != QEvent.KeyPress:
            return False
        if not isinstance(event, QKeyEvent):
            return False
        key = event.key()
        if key in (Qt.Key_Tab, Qt.Key_Backtab):
            return self._handle_tab(obj)
        if key in (Qt.Key_Left, Qt.Key_Right):
            return self._handle_left_right(obj, key == Qt.Key_Right)
        return False

    # --- Tab between wallet ↔ right-slot table ---------------------------

    def _handle_tab(self, obj) -> bool:
        wallet_tree = self._wallet_tree()
        right_table = self._active_right_table()
        if wallet_tree is None or right_table is None:
            return False
        if obj is wallet_tree:
            right_table.setFocus(Qt.TabFocusReason)
        else:
            wallet_tree.setFocus(Qt.TabFocusReason)
        return True

    # --- Left / Right switch the right-slot tab --------------------------

    def _handle_left_right(self, obj, go_right: bool) -> bool:
        if obj not in self._right_tables():
            return False
        plugins = [
            self._mw.tokens_plugin, self._mw.transactions_plugin,
        ]
        current = self._mw.right_slot.active()
        try:
            idx = plugins.index(current)
        except ValueError:
            return False
        nxt = plugins[(idx + (1 if go_right else -1)) % len(plugins)]
        if nxt is current:
            return True   # only one tab; swallow event
        self._mw.right_slot.set_active(nxt)
        new_table = self._table_for_plugin(nxt)
        if new_table is not None:
            new_table.setFocus(Qt.OtherFocusReason)
        return True

    # --- helpers ---------------------------------------------------------

    def _wallet_tree(self):
        return getattr(self._mw.wallets_plugin, "_tree", None)

    def _right_tables(self) -> list:
        out = []
        for plugin in (self._mw.tokens_plugin,
                        self._mw.transactions_plugin):
            t = self._table_for_plugin(plugin)
            if t is not None:
                out.append(t)
        return out

    def _table_for_plugin(self, plugin):
        panel = getattr(plugin, "_panel", None)
        return getattr(panel, "table", None) if panel is not None else None

    def _active_right_table(self):
        active = self._mw.right_slot.active()
        return self._table_for_plugin(active) if active is not None else None


def _is_descendant(child, ancestor) -> bool:
    """True if ``child`` is the ``ancestor`` or any of its
    descendants in the QObject tree. Used to skip widgets nested
    inside list views — their internal focus-proxy chain can
    feed a demote-to-ClickFocus call back up to the list
    itself."""
    p = child
    while p is not None:
        if p is ancestor:
            return True
        p = p.parent()
    return False


def _apply_focus_aware_selection(widget) -> None:
    """Norton-Commander-style cursor: swaps the widget's
    stylesheet between a "focused" variant (solid filled
    selection) and an "unfocused" variant (outlined selection)
    on every FocusIn / FocusOut event. The base widget classes
    have to be the actual concrete classes (QTreeView,
    QTableView, etc.) — QSS doesn't match ``QAbstractItemView``
    in practice."""
    selectors = {
        QTreeWidget: "QTreeView",
        QTableWidget: "QTableView",
    }
    # Pick the most specific selector among the widget's MRO.
    selector = "QAbstractItemView"
    for cls, sel in selectors.items():
        if isinstance(widget, cls):
            selector = sel
            break
    filt = _SelectionStyleFilter(widget, selector)
    widget.installEventFilter(filt)
    widget._selection_style_filter = filt
    # Apply the initial state.
    filt.apply(widget.hasFocus())


class _SelectionStyleFilter(QObject):
    """Per-widget event filter that rewrites the widget's
    stylesheet on FocusIn / FocusOut, swapping between a solid-
    fill selection (focused) and an outlined selection
    (unfocused). The widget's existing stylesheet is preserved
    as a prefix; only the selection rules are added/replaced."""

    def __init__(self, widget, selector):
        super().__init__(widget)
        self._widget = widget
        self._selector = selector
        # Snapshot any stylesheet the panel set up at
        # construction (padding, hover, headers, …) — we
        # concatenate the selection rules onto this base.
        self._base_qss = widget.styleSheet() or ""

    def apply(self, focused: bool) -> None:
        if focused:
            sel_qss = (
                f"{self._selector}::item:selected,"
                f"{self._selector}::item:selected:hover {{"
                f"  background: palette(highlight);"
                f"  color: palette(highlighted-text);"
                f"}}"
            )
        else:
            sel_qss = (
                f"{self._selector}::item:selected,"
                f"{self._selector}::item:selected:hover {{"
                f"  background: transparent;"
                f"  color: palette(text);"
                f"  border-top: 1px solid palette(highlight);"
                f"  border-bottom: 1px solid palette(highlight);"
                f"}}"
            )
        self._widget.setStyleSheet(self._base_qss + sel_qss)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn:
            self.apply(True)
        elif event.type() == QEvent.FocusOut:
            self.apply(False)
        return False
