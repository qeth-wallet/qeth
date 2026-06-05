"""qeth.ui — top-level MainWindow shell.

The three topic plugins (Wallets, Tokens, Transactions) and their
widgets / workers live in ``qeth.plugins.tokens``, ``.plugins.transactions``
and ``.plugins.wallets``. This module just orchestrates: instantiates
the plugins, mounts them in two slots, wires cross-plugin signals,
and handles geometry persistence."""

from PySide6.QtCore import (
    QByteArray, QEvent, QObject, QRect, QSize, Qt, QThread,
)
from PySide6.QtGui import QColor, QIcon, QKeyEvent, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QComboBox, QDialog, QLabel, QMainWindow, QSplitter, QStatusBar,
    QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTableWidget, QTreeWidget, QWidget,
)

from .icons import ChainIconCache, bundled_chain_icon
from .plugin import Slot
from .plugins.tokens import TokensPlugin
from .plugins.transactions import (
    SignTransactionDialog, TransactionsPlugin,
)
from .plugins.wallets import ACCOUNT_LABEL_ROLE, WalletsPlugin

# Sticky-note colours for the wallet-label pill: a self-consistent (bg, fg)
# pair (pale yellow / dark brown) that reads on any palette because the pill
# carries its own background — same self-consistent-pair approach as the
# Send-dialog recipient tints (feedback_theme_safe_colors).
_STICKY_BG = "#fcefa1"
_STICKY_FG = "#43360a"
# Pill geometry: horizontal/vertical text padding, gap from the address,
# and right margin inside the row.
_STICKY_PAD_H = 6
_STICKY_PAD_V = 1
_STICKY_GAP = 8
_STICKY_MARGIN = 4
from .alerts import warn
from .signing import SignAndBroadcastWorker, Signer, SignerBridge, SignerError


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
        self.transactions_plugin = TransactionsPlugin(store=self.store)

        # Signing bridge: the RPC server hands incoming signing
        # requests through this object, the slot below opens the
        # confirmation dialog on the main thread, and the bridge's
        # future is resolved with the broadcast tx hash (or rejected
        # with a SignerError). Parent it to MainWindow so the QObject
        # lives on the main thread and cross-thread emit auto-queues.
        self.signer_bridge = SignerBridge(parent=self)
        self.signer_bridge.request_received.connect(
            self._on_signing_request,
            type=Qt.ConnectionType.QueuedConnection,
        )
        self.signer_bridge.chain_added.connect(
            self._on_chain_added,
            type=Qt.ConnectionType.QueuedConnection,
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
        self.store.set_window_geometry(bytes(self.saveGeometry().toHex().data()).decode())
        self.store.set_splitter_states(
            bytes(self._splitter_outer.saveState().toHex().data()).decode(),
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
        # ChainIconCache resolves bundled → disk → upstream
        # (Curve curve-assets, then TrustWallet). Misses kick a
        # background download; we swap the icon in when it lands.
        self._chain_icon_cache = ChainIconCache(self)
        self._chain_icon_cache.icon_ready.connect(self._on_chain_icon_ready)
        for c in self.store.chains:
            label = f"{c.name} ({c.chain_id})"
            pix = self._chain_icon_cache.get(c.chain_id)
            if pix is not None:
                combo.addItem(QIcon(pix), label, c.chain_id)
            else:
                combo.addItem(label, c.chain_id)
                self._chain_icon_cache.request(c.chain_id)
        idx = combo.findData(self.store.current_chain_id)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(self._on_chain_changed)
        return combo

    def chain_icon(self, chain_id: int):
        """The chain logo pixmap (circular), or None if not yet fetched —
        in which case a background fetch is kicked. Used by the Tokens
        panel as the native-asset icon for chains whose native symbol has
        no bundled icon (AVAX, BNB, XDAI, …): the native asset's logo is
        the chain's own logo."""
        pix = self._chain_icon_cache.get(chain_id)
        if pix is None:
            self._chain_icon_cache.request(chain_id)
        return pix

    def _on_chain_icon_ready(self, chain_id: int) -> None:
        pix = self._chain_icon_cache.get(chain_id)
        if pix is None:
            return
        for i in range(self.chain_combo.count()):
            if self.chain_combo.itemData(i) == chain_id:
                self.chain_combo.setItemIcon(i, QIcon(pix))
                break
        # Let the Tokens panel fill the native-asset row's icon (AVAX/BNB/…
        # use the chain logo, fetched async).
        self.tokens_plugin.on_chain_icon_ready(chain_id, pix)

    def _on_chain_added(self, chain_id: int) -> None:
        """A dapp called ``wallet_addEthereumChain``. Append the
        new entry to the chain combo and kick icon discovery so it
        gets a logo if Curve / TrustWallet ship one. No-op if the
        combo already has it (race with a parallel add)."""
        if self.chain_combo.findData(chain_id) >= 0:
            return
        chain = next(
            (c for c in self.store.chains if c.chain_id == chain_id),
            None,
        )
        if chain is None:
            return
        label = f"{chain.name} ({chain.chain_id})"
        pix = self._chain_icon_cache.get(chain.chain_id)
        if pix is not None:
            self.chain_combo.addItem(QIcon(pix), label, chain.chain_id)
        else:
            self.chain_combo.addItem(label, chain.chain_id)
            self._chain_icon_cache.request(chain.chain_id)

    def _build_chain_rpc_button(self):
        """Little ⋯ button next to the chain combo: opens the
        chain-RPC editor so the user can swap a rate-limited
        endpoint for any chain without re-adding it. The dialog
        offers manual paste OR a pick-from-chainlist.org list."""
        from PySide6.QtWidgets import QToolButton
        btn = QToolButton()
        # Try the freedesktop gear / settings icon names in order
        # of which I see most consistently rendered as a gear
        # across KDE / GNOME / Adwaita / Breeze. If none of them
        # resolves to a real icon, fall back to the U+2699 gear
        # glyph rather than a generic placeholder — the user
        # explicitly asked for something that *looks* like a
        # config control.
        icon = QIcon()
        for name in (
            "configure", "preferences-system",
            "applications-system", "emblem-system",
            "system-run", "preferences-other",
        ):
            cand = QIcon.fromTheme(name)
            if not cand.isNull() and cand.availableSizes():
                icon = cand
                break
        if not icon.isNull():
            btn.setIcon(icon)
        else:
            btn.setText("⚙")
            f = btn.font()
            f.setPointSizeF(f.pointSizeF() * 1.15)
            btn.setFont(f)
        btn.setToolTip("Edit the RPC endpoint for the current chain")
        btn.setAutoRaise(True)
        btn.clicked.connect(self._on_edit_chain_rpc)
        return btn

    def _on_edit_chain_rpc(self) -> None:
        from .chain_rpc_dialog import ChainRpcDialog
        chain = self.store.current_chain()
        dlg = ChainRpcDialog(
            chain, parent=self,
            etherscan_api_key=self.store.etherscan_api_key or "",
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        # Both the chain RPC URL and the (global) Etherscan key
        # come from the same dialog. Apply both; either may have
        # changed independently. A "refresh against new endpoint"
        # broadcast covers either change — both affect what the
        # right-slot plugins see on their next discovery.
        new_url = dlg.rpc_url
        url_changed = bool(
            new_url and new_url != chain.rpc_url
            and self.store.set_chain_rpc_url(chain.chain_id, new_url)
        )
        key_changed = self.store.set_etherscan_api_key(dlg.etherscan_api_key)
        if url_changed:
            self.status_message(
                f"RPC for {chain.name} updated to {new_url}", 4000,
            )
        elif key_changed:
            self.status_message(
                "Etherscan API key updated"
                if self.store.etherscan_api_key
                else "Etherscan API key cleared",
                4000,
            )
        if url_changed or key_changed:
            self.right_slot.broadcast_chain_changed()

    def _build_central(self) -> None:
        self._splitter_outer = outer = QSplitter(Qt.Orientation.Horizontal)

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
        self.chain_rpc_btn = self._build_chain_rpc_button()
        self.right_slot.add_shared_widget(self.chain_rpc_btn)
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
                    w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
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
                w.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
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
        # Mouse-click focus changes don't go through the Tab
        # filter, so wire QApplication.focusChanged for the
        # universal cursor-style refresh: whenever focus moves
        # between any two of the three tab_stop lists, repaint
        # both so the solid/outline swap is immediate.
        self._focus_tab_stops = set(tab_stops)
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        assert isinstance(app, QApplication)  # a QApplication exists by now
        app.focusChanged.connect(
            self._on_app_focus_changed,
        )
        # Norton-Commander cursor style: focused list paints
        # solid selection (default), unfocused list paints
        # outline-only. Delegate handles the cell-by-cell paint;
        # the table stylesheets no longer carry a ``:selected``
        # rule so they don't beat us.
        for w in tab_stops:
            _apply_focus_aware_selection(w)
        # Initial focus on the wallet tree so arrow keys work
        # immediately.
        tab_stops[0].setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_app_focus_changed(self, old, new) -> None:
        """QApplication.focusChanged → if either side of the
        transition is one of our tab-stop lists, force a paint
        so the focus-aware cursor swap is visible immediately.
        Handles the mouse-click path; the keyboard path is
        already covered by the explicit repaints in
        ``_TabCycleFilter``."""
        for w in (old, new):
            if w in self._focus_tab_stops:
                _repaint_view(w)

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

    def account_addresses(self) -> list[str]:
        """Every address the user owns — used to highlight self-sends in
        decoded calldata and to tint the send-dialog recipient field."""
        return [a["address"] for a in self.store.accounts]

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
            identity_source=self.transactions_plugin._identity_source,
            identity_cache=self.transactions_plugin._identity_cache,
            tx_cache=self.transactions_plugin._disk_cache,
            start_worker=self.start_worker,
            token_info=self.token_info,
            icon_cache=self.icon_cache(),
            native_price_usd=native_price_usd,
            known_addresses=self.account_addresses(),
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
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
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
            identity_source=self.transactions_plugin._identity_source,
            identity_cache=self.transactions_plugin._identity_cache,
            tx_cache=self.transactions_plugin._disk_cache,
            start_worker=self.start_worker,
            token_info=self.token_info,
            icon_cache=self.icon_cache(),
            native_price_usd=self.native_price_usd(
                chain.chain_id, from_addr,
            ),
            known_addresses=self.account_addresses(),
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

    def open_replace_tx(self, tx, cancel: bool) -> None:
        """Speed up (or cancel) a pending tx by re-signing the SAME nonce
        with bumped fees. ``cancel`` swaps the tx for a 0-value self-send
        so the original never lands. Routed through the normal
        sign+broadcast flow (which add_pending()s the replacement; the
        pending sweep then flips the original to 'dropped')."""
        from eth_utils import to_checksum_address

        from .plugins.transactions import SignTransactionDialog
        from .signing import build_replacement_request
        if not getattr(tx, "raw_signed", None):
            self.status_message(
                "Can't replace — the original signed tx isn't available", 6000)
            return
        chain = next((c for c in self.store.chains
                      if c.chain_id == tx.chain_id), None)
        if chain is None:
            chain = self.store.current_chain()
        try:
            req, floor = build_replacement_request(
                from_addr=to_checksum_address(tx.from_addr),
                to_addr=to_checksum_address(tx.to_addr) if tx.to_addr else None,
                value_wei=tx.value_wei, data=tx.input_data or "0x",
                nonce=tx.nonce, raw_signed=tx.raw_signed,
                chain_id=chain.chain_id, cancel=cancel)
        except Exception as e:
            self.status_message(f"Couldn't build replacement: {e}", 6000)
            return
        verb = "Cancel" if cancel else "Speed-up"
        dialog = SignTransactionDialog(
            req, chain,
            abi_source=self.transactions_plugin._abi_source,
            abi_cache=self.transactions_plugin._abi_cache,
            identity_source=self.transactions_plugin._identity_source,
            identity_cache=self.transactions_plugin._identity_cache,
            tx_cache=self.transactions_plugin._disk_cache,
            start_worker=self.start_worker,
            token_info=self.token_info,
            icon_cache=self.icon_cache(),
            native_price_usd=self.native_price_usd(chain.chain_id, tx.from_addr),
            known_addresses=self.account_addresses(),
            fixed_nonce=tx.nonce, fee_floor=floor,
            replace_label=f"{verb} Transaction",
            parent=self,
        )
        self._launch_sign_flow(
            dialog, chain,
            on_broadcast=lambda h: self.status_message(f"{verb} {h}", 6000),
            on_cancel=lambda: None,
            on_fail=lambda msg: self.status_message(
                f"{verb} failed: {msg}", 6000),
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
            warn(dialog, "Cannot sign", str(e))
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
        signer: Signer
        if source == "ledger":
            from .ledger import LedgerSigner
            signer = LedgerSigner(self.store)
            progress_text = "Confirm the transaction on your Ledger device…"
        elif source == "hot":
            from PySide6.QtWidgets import QInputDialog, QLineEdit
            passphrase, ok = QInputDialog.getText(
                dialog, "Hot wallet",
                f"Passphrase for {finalised.from_addr}:",
                QLineEdit.EchoMode.Password, "",
            )
            if not ok:
                return
            from .hot_wallet import HotWalletSigner
            signer = HotWalletSigner(self.store, passphrase)
            # Scrypt-derived key decrypt typically takes ~1 second.
            progress_text = "Decrypting keystore and signing…"
        else:
            warn(
                dialog, "Cannot sign",
                f"No known signer for {finalised.from_addr}",
            )
            return
        if not signer.can_sign(finalised.from_addr):
            warn(
                dialog, "Cannot sign",
                f"No known signer for {finalised.from_addr}",
            )
            return

        dialog.set_signing_in_progress(True)
        from PySide6.QtWidgets import QProgressDialog
        progress = QProgressDialog(
            labelText=progress_text,
            minimum=0, maximum=0,     # indeterminate spinner
            parent=dialog,            # parent on the sign dialog so the
                                      # progress sits on top of it.
        )
        progress.setCancelButton(None)   # no cancel button
        progress.setWindowTitle("Signing Transaction")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        worker = SignAndBroadcastWorker(signer, finalised, chain)
        worker.broadcast.connect(
            lambda h, raw, d=dialog, p=progress, r=finalised, c=chain,
                   ob=on_broadcast:
                self._on_tx_broadcast(h, raw, d, p, r, c, ob)
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
                QLineEdit.EchoMode.Password, "",
            )
            if not ok:
                return None, None
            from .hot_wallet import HotWalletSigner
            return (
                HotWalletSigner(self.store, passphrase),
                "Decrypting keystore and signing…",
            )
        warn(
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
        from PySide6.QtWidgets import QProgressDialog
        signer, progress_text = self._pick_signer_for(
            dialog, req.from_addr,
        )
        if signer is None:
            return
        if not signer.can_sign(req.from_addr):
            warn(
                dialog, "Cannot sign",
                f"No known signer for {req.from_addr}",
            )
            return

        dialog.set_signing_in_progress(True)
        progress = QProgressDialog(
            labelText=progress_text, minimum=0, maximum=0, parent=dialog,
        )
        progress.setCancelButton(None)   # no cancel button
        progress.setWindowTitle("Signing Message")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
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
        warn(dialog, "Signing failed", msg)
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
        from PySide6.QtWidgets import QProgressDialog
        signer, progress_text = self._pick_signer_for(self, req.from_addr)
        if signer is None:
            return
        if not signer.can_sign(req.from_addr):
            warn(
                self, "Cannot sign",
                f"No known signer for {req.from_addr}",
            )
            return

        progress = QProgressDialog(
            labelText=progress_text, minimum=0, maximum=0, parent=self,
        )
        progress.setCancelButton(None)   # no cancel button
        progress.setWindowTitle("Signing Message")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
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
        progress.close()
        warn(self, "Signing failed", msg)

    def _on_tx_broadcast(self, tx_hash, raw_signed, dialog, progress, req,
                          chain, on_broadcast) -> None:
        progress.close()
        dialog.accept()
        # Snapshot the just-sent tx into the transactions list as a
        # pending row so the user sees it immediately — without
        # waiting for Blockscout indexing (it lags mempool by tens of
        # seconds). The plugin's PendingTxWatcher polls the receipt
        # and flips the row to confirmed when the tx mines.
        try:
            self.transactions_plugin.add_pending(
                tx_hash, req, chain, raw_signed=raw_signed,
            )
        except Exception:
            import logging
            logging.getLogger("qeth.ui").exception("add_pending failed")
        # Make the pending row visible: switch the UI chain to the
        # one we just broadcast on, select the from account in the
        # wallets tree (pending lives in that account's bucket),
        # and flip the right slot to the Transactions tab. Chain
        # first so the wallets-tree / right-slot replays happen
        # against the new chain context. All three are idempotent
        # if we're already in the right place.
        chain_idx = self.chain_combo.findData(chain.chain_id)
        if chain_idx >= 0 and chain_idx != self.chain_combo.currentIndex():
            self.chain_combo.setCurrentIndex(chain_idx)
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
        # "Signing failed" is signer-neutral — Ledger AND hot
        # wallets flow through this handler, and broadcast failures
        # (e.g. insufficient funds) bubble up here too.
        warn(dialog, "Signing failed", msg)
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
        if event.type() != QEvent.Type.KeyPress:
            return False
        if not isinstance(event, QKeyEvent):
            return False
        key = event.key()
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            return self._handle_tab(obj)
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            return self._handle_left_right(obj, key == Qt.Key.Key_Right)
        return False

    # --- Tab between wallet ↔ right-slot table ---------------------------

    def _handle_tab(self, obj) -> bool:
        wallet_tree = self._wallet_tree()
        right_table = self._active_right_table()
        if wallet_tree is None or right_table is None:
            return False
        if obj is wallet_tree:
            old, new = wallet_tree, right_table
        else:
            old, new = obj, wallet_tree
        new.setFocus(Qt.FocusReason.TabFocusReason)
        # Ensure the newly-focused list has a selected row (the
        # delegate paints solid only on selected rows; an empty
        # selection makes the focused panel look identical to
        # the unfocused one).
        _FocusRepainter._ensure_selection(new)
        # Synchronous repaint of both sides.
        _repaint_view(old)
        _repaint_view(new)
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
            new_table.setFocus(Qt.FocusReason.OtherFocusReason)
            # Same belt-and-braces as Tab: tab-switching between
            # Tokens and Transactions can skip the FocusIn delivery
            # that ``_FocusRepainter`` would otherwise use to
            # ensure a row is selected. Call it directly here so
            # the newly-active table always shows the solid
            # cursor immediately.
            _FocusRepainter._ensure_selection(new_table)
            _repaint_view(new_table)
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


def _repaint_view(view) -> None:
    """Synchronous viewport repaint. ``view.viewport().repaint()``
    forces an immediate paint pass through the delegate, so the
    new focus state is on screen before this call returns —
    unlike ``update()`` which only schedules."""
    if view is None:
        return
    if hasattr(view, "viewport"):
        view.viewport().repaint()
    else:
        view.repaint()


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
    """Norton-Commander-style cursor: hand-paint the selection
    via an item delegate. Qt's stylesheet engine on the user's
    theme (qt6ct + system Qt) defers style refreshes after
    setStyleSheet / setProperty in ways that make the first
    FocusIn on a panel paint with the stale (unfocused) rule
    until the user nudges the cursor. The delegate path queries
    view.hasFocus() at paint time, so a viewport().repaint() on
    FocusIn IS enough to get the new state on screen."""
    delegate = _FocusAwareSelectionDelegate(widget)
    widget.setItemDelegate(delegate)
    widget._focus_aware_delegate = delegate
    repainter = _FocusRepainter(widget)
    widget.installEventFilter(repainter)
    widget._focus_repainter = repainter


class _FocusAwareSelectionDelegate(QStyledItemDelegate):
    """Hand-paints selected rows with focus awareness:

    - selected + focused: fill cell with ``palette(highlight)``
      and paint text in ``palette(highlightedText)``.
    - selected + unfocused: paint cell as if not selected, then
      overlay a 1-px row outline (top + bottom on every cell,
      left only on the first column, right only on the last —
      joins into a single rectangle around the row).
    - otherwise: default delegate paint.

    The cell stylesheets on TokenListPanel / TransactionListPanel
    must NOT contain a ``::item:selected`` rule — that would
    over-paint our hand-drawn fill regardless of option.state.
    """

    def _sticky_pill_rect(self, option, label):
        """Right-aligned pill rect for a wallet label."""
        fm = option.fontMetrics
        w = fm.horizontalAdvance(label) + 2 * _STICKY_PAD_H
        h = min(option.rect.height() - 2, fm.height() + 2 * _STICKY_PAD_V)
        x = option.rect.right() - w - _STICKY_MARGIN
        y = option.rect.top() + (option.rect.height() - h) // 2
        return QRect(x, y, w, h)

    def _draw_sticky_pill(self, painter, pill, label, font):
        if pill is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # A 1px outline a few shades darker than the fill gives the pill a
        # defined sticky-note edge. Shrink by the pen width so the stroke
        # stays inside the reserved rect.
        pen = QPen(QColor(_STICKY_BG).darker(150))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(QColor(_STICKY_BG))
        painter.drawRoundedRect(pill.adjusted(0, 0, -1, -1), 4, 4)
        painter.setPen(QColor(_STICKY_FG))
        painter.setFont(font)
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()

    def paint(self, painter, option, index):
        view = self.parent()
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_focused = isinstance(view, QWidget) and view.hasFocus()

        # A labeled wallet row draws the label as a sticky-note pill on the
        # right; reserve its width so the address text (which the tree
        # elides) shrinks to make room, leaving only the label tinted.
        label = index.data(ACCOUNT_LABEL_ROLE)
        pill = self._sticky_pill_rect(option, label) if label else None
        text_rect = (
            option.rect.adjusted(0, 0, -(pill.width() + _STICKY_GAP), 0)
            if pill else option.rect
        )

        if is_selected and is_focused:
            # Fill the cell with the highlight colour first.
            highlight = option.palette.color(QPalette.ColorRole.Highlight)
            painter.fillRect(option.rect, highlight)
            # Now paint the cell contents (icon + text) on top,
            # without letting the style re-fill the background.
            # Stripping ``State_Selected`` keeps the default style
            # from drawing its own selection bar (which would
            # double-paint or look subtle on the user's theme);
            # palette swap drives the text colour to the
            # highlighted-text role so it reads on the highlight
            # background.
            opt = QStyleOptionViewItem(option)
            opt.rect = text_rect
            opt.state &= ~QStyle.StateFlag.State_Selected
            opt.state &= ~QStyle.StateFlag.State_HasFocus
            opt.palette.setColor(
                QPalette.ColorRole.Text,
                option.palette.color(QPalette.ColorRole.HighlightedText),
            )
            opt.palette.setColor(
                QPalette.ColorRole.WindowText,
                option.palette.color(QPalette.ColorRole.HighlightedText),
            )
            # The default platform paint will fill the cell with
            # palette.Base; suppress by setting Base to highlight
            # too, so our underlying fillRect isn't overwritten.
            opt.palette.setColor(QPalette.ColorRole.Base, highlight)
            opt.palette.setColor(QPalette.ColorRole.AlternateBase, highlight)
            super().paint(painter, opt, index)
            self._draw_sticky_pill(painter, pill, label, option.font)
            return

        if is_selected and not is_focused:
            opt = QStyleOptionViewItem(option)
            opt.rect = text_rect
            opt.state &= ~QStyle.StateFlag.State_Selected
            opt.state &= ~QStyle.StateFlag.State_HasFocus
            super().paint(painter, opt, index)
            self._draw_sticky_pill(painter, pill, label, option.font)
            painter.save()
            try:
                color = option.palette.color(QPalette.ColorRole.Highlight)
                pen = QPen(color)
                pen.setWidth(1)
                painter.setPen(pen)
                rect = option.rect
                painter.drawLine(rect.topLeft(), rect.topRight())
                painter.drawLine(
                    rect.bottomLeft() - _PT_UP,
                    rect.bottomRight() - _PT_UP,
                )
                model = index.model()
                if index.column() == 0:
                    painter.drawLine(
                        rect.topLeft(), rect.bottomLeft() - _PT_UP,
                    )
                if (model is not None
                        and index.column()
                        == model.columnCount(index.parent()) - 1):
                    painter.drawLine(
                        rect.topRight() - _PT_LEFT,
                        rect.bottomRight() - _PT_UP - _PT_LEFT,
                    )
            finally:
                painter.restore()
            return

        # Not selected: default paint, but never the per-cell focus
        # rectangle. The view's *current* index (set by a row insert,
        # rebuild, or an auto-switch that moves focus here) would
        # otherwise draw a stray dotted outline on an unselected cell —
        # e.g. the narrow status-icon cell of a freshly-prepended
        # pending row, where it reads as a box in the empty space beside
        # the icon. Selection, not the current cell, is what we surface.
        # Also strip hover (State_MouseOver) so the wallet tree doesn't
        # tint hovered rows when the token/tx tables (which kill hover via
        # stylesheet) don't — keeping hover behaviour consistent across
        # all three delegate-painted views.
        opt = QStyleOptionViewItem(option)
        opt.rect = text_rect
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        opt.state &= ~QStyle.StateFlag.State_MouseOver
        super().paint(painter, opt, index)
        self._draw_sticky_pill(painter, pill, label, option.font)


from PySide6.QtCore import QPoint as _QPoint   # noqa: E402
_PT_UP = _QPoint(0, 1)
_PT_LEFT = _QPoint(1, 0)


class _FocusRepainter(QObject):
    """Force a synchronous viewport repaint when keyboard focus
    enters or leaves the host widget — AND make sure a row is
    actually selected so the focus-aware delegate has something
    to paint. Without a selection, Qt draws only its native
    focus indicator (a thin dashed rect around the current
    cell), which looks identical to our "unfocused outline"
    cursor — the user reads it as "still outline" because they
    never see the solid version."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.FocusIn:
            self._ensure_selection(obj)
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            if hasattr(obj, "viewport"):
                obj.viewport().repaint()
            else:
                obj.repaint()
        return False

    @staticmethod
    def _ensure_selection(view) -> None:
        """If the view has no rows in its selection model, select
        the current item (or the first row if no current). Called
        only on FocusIn so a user who has explicitly cleared
        selection by clicking empty space stays cleared until
        they refocus."""
        sm = view.selectionModel() if hasattr(view, "selectionModel") else None
        if sm is None or sm.hasSelection():
            return
        # Pick the row to focus on. Prefer current; otherwise
        # walk the model for the first selectable index.
        idx = view.currentIndex() if hasattr(view, "currentIndex") else None
        if idx is None or not idx.isValid():
            model = view.model()
            if model is None or model.rowCount() == 0:
                return
            # First top-level row, column 0. For trees, skip
            # group-only rows (no UserRole) by walking depth-first.
            idx = _first_selectable_index(view, model)
            if idx is None or not idx.isValid():
                return
            view.setCurrentIndex(idx)
        # Select the row (or item) that's now current.
        from PySide6.QtCore import QItemSelectionModel
        sm.select(
            idx,
            QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
        )


def _first_selectable_index(view, model):
    """Depth-first walk for the first item with a non-empty
    UserRole (used by the tree to mark address leaves), or the
    very first index for non-tree models."""
    # Try top-level rows first; for trees, descend into children
    # of any group-only row.
    def walk(parent_index):
        for r in range(model.rowCount(parent_index)):
            child = model.index(r, 0, parent_index)
            from PySide6.QtCore import Qt as _Qt
            data = model.data(child, _Qt.ItemDataRole.UserRole)
            if data:
                return child
            grand = walk(child)
            if grand is not None:
                return grand
        return None

    from PySide6.QtCore import QModelIndex
    return walk(QModelIndex())
