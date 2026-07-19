"""qeth.ui — top-level MainWindow shell.

The three topic plugins (Wallets, Tokens, Transactions) and their
widgets / workers live in ``qeth.plugins.tokens``, ``.plugins.transactions``
and ``.plugins.wallets``. This module just orchestrates: instantiates
the plugins, mounts them in two slots, wires cross-plugin signals,
and handles geometry persistence."""

import random

from PySide6.QtCore import (
    QByteArray, QEvent, QObject, QRect, QSize, Qt, QThread, QTimer,
)
from PySide6.QtGui import QColor, QIcon, QKeyEvent, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QComboBox, QDialog, QLabel, QMainWindow, QSplitter, QStatusBar,
    QStyle, QStyledItemDelegate, QStyleOption, QStyleOptionViewItem,
    QTableWidget, QTreeView, QWidget,
)

from typing import TYPE_CHECKING, cast

from .icons import ChainIconCache, smooth_icon
from .notify import DesktopNotifier
from .plugin import Plugin, Slot
from .plugins.registry import PluginManifest, enabled_manifests
# Required plugins — imported eagerly for their runtime symbols (the sign dialog,
# the wallet-label roles). Optional plugins (tokens/ens) are imported ONLY by the
# registry factories when enabled, so a disabled plugin's package never loads;
# their classes appear here under TYPE_CHECKING purely for the property types.
from .plugins.transactions import SignTransactionDialog
from .plugins.wallets import ACCOUNT_LABEL_ROLE, TREE_LABEL_ROLE

if TYPE_CHECKING:
    from .plugins.ens import EnsPlugin
    from .plugins.tokens import TokensPlugin
    from .plugins.transactions import TransactionsPlugin
    from .plugins.wallets import WalletsPlugin

# Sticky-note colours for the wallet-label pill: a self-consistent (bg, fg)
# pair (pale yellow / dark brown) that reads on any palette because the pill
# carries its own background — same self-consistent-pair approach as the
# Send-dialog recipient tints (feedback_theme_safe_colors).
_STICKY_BG = "#fcefa1"
_STICKY_FG = "#43360a"
# The per-device tree label rides the same pill, in a distinct pale-blue / dark-
# navy pair so a whole-tree label reads apart from a per-account one (same
# lightness as the yellow, so it stays legible on light and dark palettes).
_TREE_STICKY_BG = "#c7e0f7"
_TREE_STICKY_FG = "#123a5e"
# Pill geometry: horizontal/vertical text padding, gap from the address,
# and right margin inside the row.
_STICKY_PAD_H = 6
_STICKY_PAD_V = 1
_STICKY_GAP = 8
_STICKY_MARGIN = 4
from .alerts import warn
from .signer_interaction import DialogInteraction
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
        # Tray controller (set by the entry point after install_tray); the
        # fallback sink for desktop notifications. None when there's no tray.
        self._tray = None
        # Primary notification path: the freedesktop service (renders our
        # custom icon, and needs no tray). See qeth.notify.
        self._notifier = DesktopNotifier()
        self.setWindowTitle("qeth — Ethereum wallet")
        self.resize(1060, 720)
        # Override QMainWindow's inflated minimumSizeHint (it reports
        # ~950x565 even when child widgets only need ~370x500).
        self.setMinimumSize(420, 360)

        # Topic plugins, built from the registry (restart-to-apply: a disabled
        # optional plugin is never constructed, and its package never imported).
        # Each owns its sources/caches/workers/widgets.
        self.plugins: dict[str, Plugin] = {}
        self._manifests: dict[str, PluginManifest] = {}
        for m in enabled_manifests(self.store):
            self.plugins[m.id] = m.factory(self.store)
            self._manifests[m.id] = m

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
        self.signer_bridge.chain_add_requested.connect(
            self._on_chain_add_requested,
            type=Qt.ConnectionType.QueuedConnection,
        )
        if self.rpc is not None:
            self.rpc.signer_bridge = self.signer_bridge

        # Workers tracked here so Python doesn't GC them while running
        # (Qt's QThread destructor aborts the process if the thread is
        # alive). Plugins register workers via host.start_worker(...);
        # they self-evict via the ``finished`` signal.
        self._active_workers: set[QThread] = set()
        # On quit, join any still-running workers before Qt tears them down —
        # the same QThread-alive-on-destroy abort as above, but at shutdown: it
        # SIGABRTs, which on macOS pops a "closed unexpectedly" dialog on Ctrl+C.
        # aboutToQuit fires after the window is gone, so the join isn't a frozen
        # window. (The ws watcher self-joins in its own stop().)
        from PySide6.QtWidgets import QApplication
        _app = QApplication.instance()
        if _app is not None:
            _app.aboutToQuit.connect(self._join_workers)

        self._build_central()
        self._build_statusbar()

        # Wallets is the source of the selection broadcast: when the
        # user picks an account, the plugin emits this signal and we
        # forward it to the right slot's mounted plugins (Tokens,
        # Transactions). Default-account changes refresh the status bar.
        self.wallets_plugin.selected_address_changed.connect(
            self.right_slot.broadcast_account_changed
        )
        self.wallets_plugin.default_account_changed.connect(
            self._push_accounts_changed
        )
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

        # Pre-warm the Helios verified-state sidecar for the current
        # chain (no-op without a helios binary / on unsupported chains):
        # one instant Popen here lets checkpoint sync overlap with the
        # user looking at balances, so the FIRST event preview is
        # already verified-warm instead of paying spawn+sync inline.
        from .helios import prewarm as _helios_prewarm
        _helios_prewarm(self.store.current_chain())
        # Also warm the MAINNET sidecar now, even when it isn't the current
        # chain: the ENS ✓ (name ownership) always proves on mainnet via Helios,
        # and Helios's cold-sync time is variable. Warming it at app start —
        # rather than only when the ENS tab first opens — gives it a long head
        # start, so the ownership proof is usually ready by the time the user
        # looks, instead of the verify racing the sync and dropping the badge.
        _mainnet = next((c for c in self.store.chains if c.chain_id == 1), None)
        if _mainnet is not None:
            _helios_prewarm(_mainnet)

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
        # (The wallets panel's internal splitter was retired when the
        # bottom details panel became action-row buttons + popups;
        # splitter_state_left is now vestigial in the config.)
        # Restore each panel's column layout (widths, order, sort
        # indicator) from the per-plugin map in the store.
        for name, plugin in self._header_persisters().items():
            saved = self.store.get_header_state(name)
            if saved:
                plugin.restore_header_state(saved)

    def _header_persisters(self) -> dict:
        """Plugins whose panel column layouts we persist across runs (those
        whose manifest sets ``persists_header`` — today just Tokens). Only
        mounted plugins appear, so a disabled one persists nothing."""
        return {m.id: self.plugins[m.id]
                for m in self._manifests.values() if m.persists_header}

    def closeEvent(self, event):
        self.store.set_window_geometry(bytes(self.saveGeometry().toHex().data()).decode())
        self.store.set_splitter_states(
            bytes(self._splitter_outer.saveState().toHex().data()).decode(),
            "",  # wallets panel no longer has an internal splitter
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
                combo.addItem(smooth_icon(pix), label, c.chain_id)
            else:
                combo.addItem(label, c.chain_id)
                self._chain_icon_cache.request(c.chain_id, c.name)
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
            name = next((c.name for c in self.store.chains
                         if c.chain_id == chain_id), None)
            self._chain_icon_cache.request(chain_id, name)
        return pix

    def _on_chain_icon_ready(self, chain_id: int) -> None:
        pix = self._chain_icon_cache.get(chain_id)
        if pix is None:
            return
        for i in range(self.chain_combo.count()):
            if self.chain_combo.itemData(i) == chain_id:
                self.chain_combo.setItemIcon(i, smooth_icon(pix))
                break
        # Let the Tokens panel fill the native-asset row's icon (AVAX/BNB/…
        # use the chain logo, fetched async).
        self.tokens_plugin.on_chain_icon_ready(chain_id, pix)

    def _on_chain_added(self, chain_id: int) -> None:
        """A dapp's ``wallet_addEthereumChain`` was approved. Append the
        new entry to the chain combo (kicking icon discovery so it gets a
        logo if Curve / TrustWallet ship one), then switch the wallet to
        it — approving an add is implicitly a request to use the network.
        Append is skipped if the combo already has it (race with a
        parallel add); the switch still runs."""
        idx = self.chain_combo.findData(chain_id)
        if idx < 0:
            chain = next(
                (c for c in self.store.chains if c.chain_id == chain_id),
                None,
            )
            if chain is None:
                return
            label = f"{chain.name} ({chain.chain_id})"
            pix = self._chain_icon_cache.get(chain.chain_id)
            if pix is not None:
                self.chain_combo.addItem(smooth_icon(pix), label, chain.chain_id)
            else:
                self.chain_combo.addItem(label, chain.chain_id)
                self._chain_icon_cache.request(chain.chain_id, chain.name)
            idx = self.chain_combo.findData(chain_id)
        if idx >= 0 and idx != self.chain_combo.currentIndex():
            self.chain_combo.setCurrentIndex(idx)

    def _on_chain_add_requested(self, info: dict, fut) -> None:
        """Slot for ``SignerBridge.chain_add_requested`` — a dapp asked
        to add a network qeth doesn't know yet
        (``wallet_addEthereumChain``). The site supplied the RPC URL, and
        once persisted qeth uses it for every balance read, simulation,
        and broadcast on that chain — for *all* apps, not just this one.
        So confirm with the user before the endpoint lands; declining
        returns a 4001 to the dapp. Runs on the Qt main thread (queued
        connection); the modal's answer resolves the bridge future."""
        from PySide6.QtWidgets import QMessageBox
        origin = info.get("origin") or "A connected site"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Add network?")
        box.setText(f"{origin} wants to add a network to qeth.")
        box.setInformativeText(
            f"Network: {info['name']} (chain {info['chain_id']})\n"
            f"Currency: {info['symbol']}\n"
            f"RPC URL: {info['rpc_url']}\n\n"
            "This RPC endpoint is provided by the site. If you add it, "
            "qeth will use it for all balances, previews, and transaction "
            "broadcasts on this network — including for other apps. Only "
            "add networks from sources you trust."
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        # The prompt fires while the user is in the browser (driving the
        # dapp), so qeth's window is usually backgrounded — without this
        # the first click only raises/focuses the window and the button
        # needs a second click. Raise + activate so one click suffices.
        box.show()
        box.raise_()
        box.activateWindow()
        approved = box.exec() == QMessageBox.StandardButton.Yes
        self.signer_bridge.resolve_chain(fut, approved)

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
        btn.setToolTip("Edit chain RPC")
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
            # Helios bakes the execution-rpc in at spawn, so a running sidecar
            # is now stale. Re-prewarm the current chain: _ensure_sidecar sees
            # the changed RPC, retires the old process and spawns a fresh one on
            # the new endpoint (no-op if the chain isn't Helios-supported).
            from .helios import prewarm as _helios_prewarm
            _helios_prewarm(self.store.current_chain())
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

        # Left slot shows its single "Accounts" tab anyway, so its top chrome
        # matches the right slot's tab bar and the two lists start at the same y
        # (the toolbar that used to sit here was a different height).
        self.left_slot = Slot(show_single_tab=True)
        self.right_slot = Slot()
        slots = {"left": self.left_slot, "right": self.right_slot}
        # Mount each enabled plugin into its slot, in manifest order.
        for m in sorted(self._manifests.values(), key=lambda m: m.order):
            slots[m.slot].add_plugin(self.plugins[m.id], self)
        outer.addWidget(self.left_slot)

        self.chain_combo = self._build_chain_combo()
        self.right_slot.add_shared_widget(self.chain_combo)
        self.chain_rpc_btn = self._build_chain_rpc_button()
        self.right_slot.add_shared_widget(self.chain_rpc_btn)
        # The chain selector is meaningless on the ENS tab — ENS lives only on
        # Ethereum mainnet, so the plugin pins to chain 1 regardless of the
        # selected network. Hide it there (showing it just invites a switch
        # that does nothing, which reads as a bug).
        self.right_slot.active_plugin_changed.connect(self._on_right_plugin_changed)
        self._on_right_plugin_changed(self.right_slot.active())   # sync initial
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
        # The ENS tree paints its own focus-aware selection (custom
        # delegate + multi-column status icons), so don't overlay the
        # generic one — just give it the FocusIn repaint + selection.
        ens_tree = getattr(
            getattr(self.ens_plugin, "_panel", None), "tree", None)
        for w in tab_stops:
            _apply_focus_aware_selection(w, with_delegate=(w is not ens_tree))
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
        epanel = getattr(self.ens_plugin, "_panel", None)
        if epanel is not None and hasattr(epanel, "tree"):
            out.append(epanel.tree)
        return out

    # Idle hints rotated through the status bar — keyboard / navigation
    # affordances that otherwise go undiscovered. (The RPC URL and the
    # default-wallet/chain line that used to live here were dev noise /
    # duplicated by the Wallets tree + chain selector.) A QStatusBar hides
    # non-permanent widgets while a temporary status_message() shows and
    # restores them after, so the hint sits *under* transient messages.
    _STATUS_HINTS = (
        "Hold Alt to reveal each button's underlined shortcut letter.",
        "Tab moves between the Wallets list and the right panel; "
        "←/→ cycle the Tokens / Transactions / ENS tabs.",
        "In a dialog, Tab / Shift+Tab move between fields; Enter confirms.",
        "Ctrl+C copies the selected address, token, or tx hash.",
        "Del removes the selected account.",
        "Double-click an account to connect it to the browser (set as default).",
        "Drag accounts — or whole device branches — in the tree to reorder them.",
        "Right-click an account for Copy, Remove, and Connect-to-Browser.",
        "Ctrl+F filters accounts by label or address; ↑/↓ step through the matches.",
        "Edit Label… renames an account — or a whole hardware-device subtree.",
        "Double-click a transaction to see its decoded details.",
        "Right-click a pending transaction to Speed up or Cancel it.",
        "Scroll to the bottom of the history to load older transactions.",
        "The Contract row names who deployed a contract and how often you've "
        "used it.",
        "Double-click a token to open its transfers in the block explorer.",
        "In Send, Max fills your full balance; the USD value updates as you type.",
        "Before you sign, qeth simulates the transaction and previews the token "
        "and balance changes it will make.",
        "Switch networks with the chain selector in the toolbar.",
        "Point any network at your own RPC — edit it beside the chain selector.",
        "Right-click an address or hash to copy it or open it in the explorer.",
        "Connect dapps — qeth serves a Frame-compatible wallet on 127.0.0.1:1248.",
        "Select a token to Hide it from this wallet, or pin ★ to keep it shown.",
        "Toggle 'Show all' to reveal hidden tokens and dust-value balances.",
        "Sign an arbitrary message: pick an account, then Sign Message… "
        "(action-row button or right-click).",
        "The QR button shows an account's address as a scannable code.",
        "Track any address read-only: Add → Watch-only Address.",
        "Add → Air-gapped signs with a QR hardware wallet (Keystone / Keycard).",
        "In the ENS tab, set a name's ETH address, text and IPFS records, and "
        "add or remove subdomains.",
        "Renew or transfer your .eth names from the ENS tab — Extend one before "
        "it expires.",
    )

    def _build_statusbar(self) -> None:
        sb = QStatusBar()
        self.setStatusBar(sb)
        # A single label we drive ourselves — NOT QStatusBar.showMessage(),
        # which paints the message *over* a custom addWidget() widget rather
        # than replacing it. A status_message() swaps the label text and
        # schedules _restore_hint(); the rotating hint sits underneath.
        self._hint_label = QLabel()
        sb.addWidget(self._hint_label, 1)
        # Random order, reshuffled each full pass — different sequence (and
        # starting hint) every session, no immediate repeats.
        self._hint_queue: list[int] = []
        self._last_hint = -1
        self._current_hint = ""
        self._msg_timer: QTimer | None = None   # active transient message
        self._show_next_hint()
        self._hint_timer = QTimer(self)
        self._hint_timer.setInterval(15_000)
        self._hint_timer.timeout.connect(self._show_next_hint)
        self._hint_timer.start()

    def _show_next_hint(self) -> None:
        if not self._hint_queue:
            order = list(range(len(self._STATUS_HINTS)))
            random.shuffle(order)
            # Don't repeat the last-shown hint across the reshuffle seam.
            if len(order) > 1 and order[-1] == self._last_hint:
                order[-1], order[0] = order[0], order[-1]
            self._hint_queue = order
        idx = self._hint_queue.pop()
        self._last_hint = idx
        self._current_hint = "💡 " + self._STATUS_HINTS[idx]
        # Don't clobber a transient message that's currently on screen;
        # _restore_hint will pick up the latest hint when it expires.
        if self._msg_timer is None:
            self._hint_label.setText(self._current_hint)

    def _restore_hint(self) -> None:
        self._msg_timer = None
        self._hint_label.setText(self._current_hint)

    # --- Host protocol (consumed by plugins via plugin.attach) -----------

    @property
    def selected_address(self) -> "str | None":
        return self.wallets_plugin.selected_address

    @property
    def selected_key(self) -> "tuple[str, str] | None":
        """(address, path) of the single selected account row — used to route
        a UI-driven send/sign to the exact signer record the user selected when
        an address is held by two signers (Ledger + Air-gapped)."""
        return self.wallets_plugin.selected_key

    def plugin(self, plugin_id: str):
        """The mounted plugin with this id, or None if it isn't mounted (an
        optional plugin the user disabled). The sanctioned way for one plugin
        to reach an optional sibling — e.g. TransactionsPlugin relaying
        live-balance events into TokensPlugin."""
        return self.plugins.get(plugin_id)

    # Convenience accessors over the plugins dict. Typed non-Optional here
    # because nothing disables a plugin yet (the toggle UI + optional-off
    # correctness land next commit, which flips tokens/ens to Optional and
    # guards their consumers). Reach an optional sibling via plugin(id).
    @property
    def wallets_plugin(self) -> "WalletsPlugin":
        return cast("WalletsPlugin", self.plugins["wallets"])

    @property
    def transactions_plugin(self) -> "TransactionsPlugin":
        return cast("TransactionsPlugin", self.plugins["transactions"])

    @property
    def tokens_plugin(self) -> "TokensPlugin":
        return cast("TokensPlugin", self.plugins["tokens"])

    @property
    def ens_plugin(self) -> "EnsPlugin":
        return cast("EnsPlugin", self.plugins["ens"])

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

    # Total wall-clock budget for joining in-flight workers at quit. Workers run
    # concurrently, so this bounds the SLOWEST one, not their sum; they do
    # timeout-bounded network I/O, so most finish well inside it.
    _SHUTDOWN_JOIN_S = 5.0

    def _join_workers(self) -> None:
        """Wait (bounded) for in-flight background workers on quit so none is
        still running when Qt destroys its QThread — that aborts (SIGABRT). They
        self-evict via ``finished``, but at quit there's no event loop left to
        run that, so we join explicitly here."""
        # Silence every mounted plugin's long-lived poll timers / ws watchers
        # first, so nothing kicks a fresh worker while we drain the in-flight
        # ones. (Iterating self.plugins means a new plugin is covered for free.)
        for plugin in self.plugins.values():
            try:
                plugin.shutdown()
            except RuntimeError:
                pass          # C++ side already gone
        import time as _t
        deadline = _t.monotonic() + self._SHUTDOWN_JOIN_S
        for w in list(self._active_workers):
            remaining = deadline - _t.monotonic()
            if remaining <= 0:
                break
            try:
                if w.isRunning():
                    w.wait(int(remaining * 1000))
            except RuntimeError:
                pass          # C++ object already gone — nothing to join

    def set_tray(self, tray) -> None:
        """Adopt the tray controller (the desktop-notification sink). Called
        by the entry point once the tray is installed; ``None`` when the
        platform has no system tray."""
        self._tray = tray

    def notify(self, title: str, body: str, icon=None) -> None:
        """Raise a sent/received desktop notification (host protocol, called
        by the Tokens / Transactions plugins). ``icon`` is the composed
        token/coin + direction badge. Prefers the freedesktop notification
        service (which actually renders the custom icon — Qt's tray drops it
        on xfce4-notifyd) and falls back to the tray. No-op when notifications
        are disabled. Failures are swallowed — never worth crashing a
        handler."""
        if not self.store.notifications_enabled:
            return
        try:
            pixmap = icon.pixmap(128, 128) if icon is not None else None
            if self._notifier.send(title, body, pixmap):
                return
            if self._tray is not None:
                self._tray.show_message(title, body, icon)
        except Exception:
            import logging
            logging.getLogger("qeth.ui").exception("notification failed")

    def status_message(self, text: str, timeout_ms: int = 3000) -> None:
        """Show a transient message in the status bar, replacing the idle
        hint, then restore the hint after ``timeout_ms``."""
        self._hint_label.setText(text)
        if self._msg_timer is not None:
            self._msg_timer.stop()
        if timeout_ms > 0:
            self._msg_timer = QTimer(self)
            self._msg_timer.setSingleShot(True)
            self._msg_timer.timeout.connect(self._restore_hint)
            self._msg_timer.start(timeout_ms)
        else:
            # No timeout → message stays until the next status_message;
            # mark active so hint rotation doesn't overwrite it.
            self._msg_timer = QTimer(self)

    def token_info(self, chain_id: int, address: str):
        return self.tokens_plugin.token_lists.get(chain_id, address)

    def account_addresses(self) -> list[str]:
        """Every address the user owns — used to highlight self-sends in
        decoded calldata and to tint the send-dialog recipient field."""
        return [a["address"] for a in self.store.accounts]

    def account_book(self) -> list[tuple[str, str]]:
        """(address, label) for every account the user owns — the Send
        dialog's recipient autocomplete + own-wallet label. Scoped to the
        user's own wallets only (no arbitrary saved contacts), so the
        picker can never suggest an address you didn't add yourself.

        One entry per ADDRESS, carrying its effective label — a repeat address
        (held in two branches, one unlabelled) still resolves by its label,
        instead of the empty-label twin winning the de-dup."""
        book: dict[str, tuple[str, str]] = {}
        for a in self.store.accounts:
            addr = a["address"]
            low = addr.lower()
            label = a.get("label") or ""
            existing = book.get(low)
            if existing is None:
                book[low] = (addr, label)
            elif not existing[1] and label:      # fill an empty label from a twin
                book[low] = (existing[0], label)
        return list(book.values())

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

        Any exception while building/showing the dialog rejects the future,
        so ``submit_async`` never awaits it forever — an unresolved future
        hangs the dapp request and, since a WS socket's messages are handled
        serially, its whole connection."""
        try:
            self._launch_signing_dialog(req, fut)
        except Exception as e:
            import logging
            logging.getLogger("qeth.ui").exception(
                "signing request setup failed")
            self.signer_bridge.reject(fut, SignerError(str(e)))

    def _launch_signing_dialog(self, req, fut) -> None:
        """Build + show the dialog for a signing request, dispatching by
        request type. ``req`` is one of ``SigningRequest`` (transactions),
        ``MessageSigningRequest`` (personal_sign), or
        ``TypedDataSigningRequest`` (EIP-712). Raising is safe — the caller
        rejects the future."""
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
            sim_floor_provider=self.transactions_plugin.fork_floor_block,
            nonce_floor_provider=self.transactions_plugin.pending_nonce_floor,
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
                          on_broadcast, on_cancel, on_fail,
                          signing_key: "tuple[str, str] | None" = None) -> None:
        """Wire a sign-style dialog (SignTransactionDialog or
        SendTokenDialog — both expose ``sign_requested`` /
        ``finalised_request`` / ``set_signing_in_progress`` /
        ``accept``) to the worker pipeline. Callbacks fire on
        broadcast success / dialog cancel / signing failure so the
        same code path serves both the RPC-driven and the locally
        UI-driven signing flows. ``signing_key`` is the (address, path) of
        the account row a UI-driven opener was launched for; it disambiguates
        the signer when one address is held by two signers (Ledger +
        Air-gapped). The RPC flow leaves it None (signs as the connected
        default)."""
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.sign_requested.connect(
            lambda d=dialog, c=chain, ob=on_broadcast, of=on_fail, sk=signing_key:
                self._begin_sign(d, c, ob, of, signing_key=sk)
        )
        dialog.rejected.connect(on_cancel)
        dialog.show()

    def _composer_shared_kwargs(self, chain, from_addr: str) -> dict:
        """The kwargs every transaction-composer / sign dialog needs — the
        ABI/identity/tx-history sources, worker pump, icon + price providers,
        the user's own addresses, and the sim/nonce floor providers. Pulled
        once here so the Send / Speed-up / ENS / request_transaction openers
        don't each re-spell the same block."""
        tp = self.transactions_plugin
        return {
            "abi_source": tp._abi_source,
            "abi_cache": tp._abi_cache,
            "identity_source": tp._identity_source,
            "identity_cache": tp._identity_cache,
            "tx_cache": tp._disk_cache,
            "start_worker": self.start_worker,
            "token_info": self.token_info,
            "icon_cache": self.icon_cache(),
            "native_price_usd": self.native_price_usd(chain.chain_id, from_addr),
            "known_addresses": self.account_addresses(),
            "sim_floor_provider": tp.fork_floor_block,
            "nonce_floor_provider": tp.pending_nonce_floor,
        }

    def open_send_dialog(self, asset: dict, chain, from_addr: str) -> None:
        """Host-facing entry point used by TokensPlugin's Send
        button. Opens SendTokenDialog and runs the same worker
        pipeline as the RPC flow; success / cancel / failure
        produce status-bar messages (no bridge future)."""
        from .plugins.transactions import SendTokenDialog
        dialog = SendTokenDialog(
            asset, chain, from_addr,
            **self._composer_shared_kwargs(chain, from_addr),
            address_book=self.account_book(),
            parent=self,
        )
        self._launch_sign_flow(
            dialog, chain,
            signing_key=self.selected_key,
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
            **self._composer_shared_kwargs(chain, tx.from_addr),
            fixed_nonce=tx.nonce, fee_floor=floor,
            replace_label=f"{verb} Transaction",
            parent=self,
        )
        self._launch_sign_flow(
            dialog, chain,
            signing_key=self.selected_key,
            on_broadcast=lambda h: self.status_message(f"{verb} {h}", 6000),
            on_cancel=lambda: None,
            on_fail=lambda msg: self.status_message(
                f"{verb} failed: {msg}", 6000),
        )

    def request_transaction(self, req, chain, label: str,
                            on_broadcast=None, on_confirmed=None) -> None:
        """Open the review + sign + broadcast flow for an arbitrary locally-built
        transaction (used by the ENS plugin's record/subdomain writes). Same
        pipeline as Send / Speed-up: the dialog estimates gas + fees + nonce and
        simulates, then the worker signs (Ledger / hot wallet) and broadcasts and
        the pending watcher tracks it. ``on_broadcast(tx_hash)`` fires after a
        successful broadcast; ``on_confirmed(receipt)`` fires once when that tx
        mines (so the caller can refresh against confirmed — not finalized —
        state)."""
        from .plugins.transactions import SignTransactionDialog
        dialog = SignTransactionDialog(
            req, chain,
            **self._composer_shared_kwargs(chain, req.from_addr),
            replace_label=label,
            parent=self,
        )
        self._launch_composer(dialog, chain, label=label, signing_key=self.selected_key,
                              on_broadcast=on_broadcast, on_confirmed=on_confirmed)

    def open_ens_composer(self, name: str, op, chain, from_addr: str, *,
                          on_confirmed=None) -> None:
        """Open the rich ENS write composer for ``op`` (built by the ENS
        plugin) and drive it through the shared sign+broadcast pipeline. Same
        machinery as Send: inline inputs + decoded calldata + Events sim + gas
        settings; the op's payable value (renew) flows through ``_build_request``
        into both the gas probe and the simulation. ``on_confirmed`` fires once
        when the write mines, so the plugin can refresh against confirmed
        state."""
        from .plugins.ens import _EnsWriteComposer
        dialog = _EnsWriteComposer(
            op, name, chain, from_addr,
            **self._composer_shared_kwargs(chain, from_addr),
            address_book=self.account_book(),
            parent=self,
        )
        self._launch_composer(dialog, chain, label=op.confirm_label,
                              signing_key=self.selected_key, on_confirmed=on_confirmed)

    def _launch_composer(self, dialog, chain, *, label: str,
                         on_broadcast=None, on_confirmed=None,
                         signing_key: "tuple[str, str] | None" = None) -> None:
        """Drive a composer / sign-style dialog through the shared
        sign+broadcast pipeline with status-bar feedback. ``on_confirmed`` is
        wired (once) to the tx mining; ``on_broadcast`` fires on a successful
        broadcast. ``signing_key`` (the selected account row) is forwarded so
        signing resolves to that exact record. Used by ``request_transaction``
        and the ENS composer opener — both build their own dialog, then hand
        it here."""
        def _bcast(h: str) -> None:
            self.status_message(f"{label}: {h[:12]}…", 6000)
            if on_confirmed is not None:
                self._call_on_confirm(chain.chain_id, h, on_confirmed)
            if on_broadcast is not None:
                try:
                    on_broadcast(h)
                except Exception:
                    import logging
                    logging.getLogger("qeth.ui").debug(
                        "_launch_composer on_broadcast failed", exc_info=True)

        self._launch_sign_flow(
            dialog, chain,
            signing_key=signing_key,
            on_broadcast=_bcast,
            on_cancel=lambda: None,
            on_fail=lambda msg: self.status_message(f"{label} failed: {msg}", 6000),
        )

    def _call_on_confirm(self, chain_id: int, tx_hash: str, callback) -> None:
        """Fire ``callback(receipt)`` once, when the pending tx with this hash
        mines. Subscribes to the transactions plugin's ``tx_confirmed`` and
        self-disconnects on the first match — the watcher already polls the
        receipt, so we just piggy-back on its confirmation. Also disconnects if
        the tx is DROPPED (its other terminal state): otherwise a tx that never
        confirms leaves this listener — and its captured callback — connected to
        the app-lifetime signal forever (5f)."""
        plugin = self.transactions_plugin
        want = tx_hash.lower()

        def _cleanup() -> None:
            for sig, slot in ((plugin.tx_confirmed, _on_confirmed),
                              (plugin.tx_dropped, _on_dropped)):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass

        def _on_confirmed(chain, h: str, receipt) -> None:
            if h.lower() != want or chain.chain_id != chain_id:
                return
            _cleanup()
            try:
                callback(receipt)
            except Exception:
                import logging
                logging.getLogger("qeth.ui").debug(
                    "request_transaction on_confirmed failed", exc_info=True)

        def _on_dropped(chain, h: str) -> None:
            if h.lower() != want or chain.chain_id != chain_id:
                return
            _cleanup()   # never confirms → stop waiting; the callback isn't run

        plugin.tx_confirmed.connect(_on_confirmed)
        plugin.tx_dropped.connect(_on_dropped)

    def _begin_sign(self, dialog, chain, on_broadcast, on_fail,
                    signing_key: "tuple[str, str] | None" = None) -> None:
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

        # Pick the right Signer from the account's source via the registry.
        # The interaction host owns the "signing…" spinner and any unlock
        # prompt (a hot wallet asks for its passphrase up-front on the main
        # thread, so the worker has the decrypted key by the time it calls
        # signer.sign()); it also marshals a worker-side backend's UI (step 3's
        # QR) onto the main loop. (None, None) means no signer or the user
        # cancelled the unlock — _pick_signer_for already warned if needed.
        #
        # When one address is held by two signers (Ledger + Air-gapped), the
        # exact record is the SELECTED tree row (``signing_key``), captured at
        # open time by the UI-driven opener. Use its path only when it matches
        # the from-address; otherwise leave it None so account_for_signing falls
        # back to the connected default (the dapp/RPC flow passes no key, so it
        # always signs as the default).
        interaction = DialogInteraction(dialog, title="Signing Transaction")
        signing_path = self._signing_path_for(signing_key, finalised.from_addr)
        signer, progress_text = self._pick_signer_for(
            dialog, finalised.from_addr, interaction, path=signing_path)
        if signer is None:
            return
        if not signer.can_sign(finalised.from_addr):
            warn(
                dialog, "Cannot sign",
                f"No known signer for {finalised.from_addr}",
            )
            return

        dialog.set_signing_in_progress(True)
        if progress_text:                 # empty for a QR signer — it
            interaction.progress(progress_text)   # drives its own window

        worker = SignAndBroadcastWorker(signer, req=finalised, chain=chain)
        worker.broadcast.connect(
            lambda h, raw, ok, d=dialog, it=interaction, r=finalised, c=chain,
                   ob=on_broadcast:
                self._on_tx_broadcast(
                    h, raw, ok, dialog=d, interaction=it, req=r, chain=c,
                    on_broadcast=ob)
        )
        worker.failed.connect(
            lambda msg, d=dialog, it=interaction, of=on_fail:
                self._on_tx_sign_failed(
                    msg, dialog=d, interaction=it, on_fail=of)
        )
        self.start_worker(worker)

    def _signing_path_for(
        self, signing_key: "tuple[str, str] | None", from_addr: str,
    ) -> str | None:
        """The derivation path of the account record this sign should use, or
        None. ``signing_key`` is the SELECTED tree row's ``(address, path)``,
        captured by a UI-driven opener; when its address matches ``from_addr``
        we return that exact path so ``account_for_signing`` resolves to the
        record the user selected — the disambiguator when one address is held
        by two signers (Ledger + Air-gapped). Returns None when there is no key
        (dapp/RPC flow) or the selection has moved to a different address, so
        signing falls back to the connected default."""
        if signing_key is not None and signing_key[0].lower() == from_addr.lower():
            return signing_key[1]
        return None

    def _pick_signer_for(
        self, dialog, address: str, interaction, path: str | None = None,
    ) -> tuple[Signer | None, str]:
        """Pick a Signer for the account's ``source`` via the signer REGISTRY
        (``qeth.signers``). The plugin drives ``interaction`` for any up-front
        unlock (a hot wallet prompts for its passphrase on the main thread, so
        the worker's slow scrypt decrypt runs off-thread). ``path`` disambiguates
        when the same address is held by two signers (Ledger + Air-gapped) — else
        the connected default's remembered record is used. Returns
        ``(signer, progress_text)``, or ``(None, None)`` if the user cancelled
        the prompt or the source has no signer (shows the "no signer" warning
        in that case)."""
        acct = self.store.account_for_signing(address, path)
        source = acct.get("source") if acct else None
        from .signers import signer_for_source
        plugin = signer_for_source(source)
        if plugin is None or not plugin.can_sign() or acct is None:
            warn(dialog, "Cannot sign", f"No known signer for {address}")
            return None, ""
        signer = plugin.make_signer(self.store, account=acct, ui=interaction)
        if signer is None:
            return None, ""   # user cancelled the unlock prompt
        return signer, plugin.progress_text

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
        interaction = DialogInteraction(dialog, title="Signing Message")
        signer, progress_text = self._pick_signer_for(
            dialog, req.from_addr, interaction,
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
        if progress_text:                 # empty for a QR signer — it
            interaction.progress(progress_text)   # drives its own window

        worker = SignMessageWorker(signer, req=req)
        worker.signed.connect(
            lambda sig, d=dialog, it=interaction, ok=on_signed:
                self._on_message_signed(
                    sig, dialog=d, interaction=it, on_signed=ok)
        )
        worker.failed.connect(
            lambda msg, d=dialog, it=interaction, of=on_fail:
                self._on_message_sign_failed(
                    msg, dialog=d, interaction=it, on_fail=of)
        )
        self.start_worker(worker)

    def _on_message_signed(self, sig_hex, dialog, interaction, on_signed):
        interaction.close()
        dialog.accept()
        on_signed(sig_hex)

    def _on_message_sign_failed(self, msg, dialog, interaction, on_fail):
        interaction.close()
        dialog.set_signing_in_progress(False)
        warn(dialog, "Signing failed", msg)
        on_fail(msg)

    def open_sign_message_dialog(self, address: str, path: str | None = None) -> None:
        """User-initiated 'sign anything you paste' flow. Triggered
        from the details panel button. Opens
        ``ComposeMessageDialog`` to collect the payload; the
        compose dialog IS the review — the user just typed the
        text, no separate confirmation step needed. After signing,
        ``SignatureResultDialog`` shows the 0x-hex with a copy-
        to-clipboard button (no dapp to receive it
        automatically). ``path`` routes a repeat address to the
        selected branch's signer."""
        from .plugins.sign_message import ComposeMessageDialog

        compose = ComposeMessageDialog(address, parent=self)
        compose.request_built.connect(
            lambda req, p=path: self._sign_local_message(req, p))
        compose.show()

    def _sign_local_message(self, req, path: str | None = None) -> None:
        """Sign a locally-composed message (from
        ComposeMessageDialog). No review step — the user typed it
        themselves. Picks signer, prompts for passphrase if hot,
        runs SignMessageWorker, then shows the resulting
        signature."""
        interaction = DialogInteraction(self, title="Signing Message")
        signer, progress_text = self._pick_signer_for(
            self, req.from_addr, interaction, path)
        if signer is None:
            return
        if not signer.can_sign(req.from_addr):
            warn(
                self, "Cannot sign",
                f"No known signer for {req.from_addr}",
            )
            return

        if progress_text:                 # empty for a QR signer — it
            interaction.progress(progress_text)   # drives its own window

        from .signing import SignMessageWorker
        worker = SignMessageWorker(signer, req=req)
        worker.signed.connect(
            lambda sig, it=interaction:
                self._on_local_message_signed(sig, interaction=it)
        )
        worker.failed.connect(
            lambda msg, it=interaction:
                self._on_local_message_sign_failed(msg, interaction=it)
        )
        self.start_worker(worker)

    def _on_local_message_signed(self, signature_hex, interaction) -> None:
        from .plugins.sign_message import SignatureResultDialog
        interaction.close()
        dlg = SignatureResultDialog(signature_hex, parent=self)
        dlg.show()

    def _on_local_message_sign_failed(self, msg, interaction) -> None:
        interaction.close()
        warn(self, "Signing failed", msg)

    def _on_tx_broadcast(self, tx_hash, raw_signed, first_push_ok, dialog,
                          interaction, req, chain, on_broadcast) -> None:
        # Read anything we need off the dialog BEFORE accept() — accept() emits
        # finished, which now schedules the dialog's deleteLater (5f). It's
        # deferred (survives this call stack), but capturing first keeps it
        # robust against any future event-loop turn between here and the use.
        sim_logs = getattr(dialog, "_logs", None)
        interaction.close()
        dialog.accept()
        if not first_push_ok:
            # The first push never reached the node (transport failure) —
            # the tx is recorded as pending below and the watcher keeps
            # re-broadcasting it, but the user should know it's in limbo
            # rather than cleanly sent.
            self.status_message(
                "⚠ Broadcast did not reach the node — the wallet will keep "
                "re-trying in the background", timeout_ms=8000,
            )
        # Snapshot the just-sent tx into the transactions list as a
        # pending row so the user sees it immediately — without
        # waiting for Blockscout indexing (it lags mempool by tens of
        # seconds). The plugin's PendingTxWatcher polls the receipt
        # and flips the row to confirmed when the tx mines.
        try:
            self.transactions_plugin.add_pending(
                tx_hash, req, chain, raw_signed=raw_signed,
            )
            # The Sign/Send dialog already simulated this tx and holds the
            # Transfer logs it will emit — fold them into the pending row's
            # activity so a swap shows its coins immediately (before the
            # receipt, before Blockscout indexes). The confirmed receipt
            # later re-asserts the same legs. (sim_logs captured above.)
            if sim_logs:
                self.transactions_plugin.note_transfer_legs(
                    chain.chain_id, tx_hash, sim_logs, req.from_addr,
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

    def _on_tx_sign_failed(self, msg: str, dialog, interaction,
                            on_fail) -> None:
        """Signing failed (Ledger unavailable, user cancelled on
        device, broadcast rejected, …). Don't close the sign
        dialog — show the message on top of it and re-enable
        Confirm so the user can fix the device and retry without
        losing the dialog state."""
        interaction.close()
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

    def _on_right_plugin_changed(self, plugin) -> None:
        """Hide the shared chain controls on tabs where a network choice is
        meaningless (manifest ``hides_chain_selector`` — the ENS tab, which is
        mainnet-only)."""
        m = self._manifest_for(plugin)
        show = not (m is not None and m.hides_chain_selector)
        self.chain_combo.setVisible(show)
        self.chain_rpc_btn.setVisible(show)

    def _manifest_for(self, plugin) -> "PluginManifest | None":
        """The manifest of a mounted plugin instance, or None."""
        for pid, p in self.plugins.items():
            if p is plugin:
                return self._manifests.get(pid)
        return None

    def _on_chain_changed(self, idx: int) -> None:
        cid = self.chain_combo.itemData(idx)
        if cid is not None:
            self.store.set_current_chain(int(cid))
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
            # Pre-warm the verified-state sidecar for the new chain so a
            # preview shortly after the switch doesn't pay sync inline.
            from .helios import prewarm as _helios_prewarm
            _helios_prewarm(self.store.current_chain())

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
        # Cycle over EVERY plugin mounted in the right slot (Tokens,
        # Transactions, ENS, …) — in tab-bar order — not a hardcoded pair.
        plugins = self._mw.right_slot.plugins()
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
        for plugin in self._mw.right_slot.plugins():
            t = self._table_for_plugin(plugin)
            if t is not None:
                out.append(t)
        return out

    def _table_for_plugin(self, plugin):
        # A right-slot plugin's focusable list: Tokens/Transactions expose a
        # QTableWidget as ``.table``, ENS a QTreeWidget as ``.tree``.
        panel = getattr(plugin, "_panel", None)
        if panel is None:
            return None
        return getattr(panel, "table", None) or getattr(panel, "tree", None)

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
    while child is not None:
        if child is ancestor:
            return True
        child = child.parent()
    return False


def _apply_focus_aware_selection(widget, *, with_delegate: bool = True) -> None:
    """Norton-Commander-style cursor: hand-paint the selection
    via an item delegate. Qt's stylesheet engine on the user's
    theme (qt6ct + system Qt) defers style refreshes after
    setStyleSheet / setProperty in ways that make the first
    FocusIn on a panel paint with the stale (unfocused) rule
    until the user nudges the cursor. The delegate path queries
    view.hasFocus() at paint time, so a viewport().repaint() on
    FocusIn IS enough to get the new state on screen.

    ``with_delegate`` False for a list that already paints its own
    focus-aware selection (the ENS tree) — it still gets the
    FocusIn repaint + ensure-a-row-is-selected behaviour, just not
    our generic delegate over its custom one."""
    if with_delegate:
        delegate = _FocusAwareSelectionDelegate(widget)
        widget.setItemDelegate(delegate)
        widget._focus_aware_delegate = delegate
    repainter = _FocusRepainter(widget)
    widget.installEventFilter(repainter)
    widget._focus_repainter = repainter


_TABLE_ROW_H = 0


def _table_row_height() -> int:
    """The current style's natural QTableView row height. A QTreeView's
    rows come out shorter than a QTableView's under most styles (Fusion:
    18 vs 30), so the wallet tree on the left looked cramped next to the
    token/tx tables on the right. The delegate raises the tree's rows to
    this. Cached — the style is fixed at startup."""
    global _TABLE_ROW_H
    if not _TABLE_ROW_H:
        ref = QTableWidget(0, 0)
        _TABLE_ROW_H = ref.verticalHeader().defaultSectionSize()
        ref.deleteLater()
    return _TABLE_ROW_H


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

    def _draw_sticky_pill(self, painter, pill, label, font,
                          bg=_STICKY_BG, fg=_STICKY_FG):
        if pill is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # A 1px outline a few shades darker than the fill gives the pill a
        # defined sticky-note edge. Shrink by the pen width so the stroke
        # stays inside the reserved rect.
        pen = QPen(QColor(bg).darker(150))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(pill.adjusted(0, 0, -1, -1), 4, 4)
        painter.setPen(QColor(fg))
        painter.setFont(font)
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()

    def sizeHint(self, option, index):
        # Raise rows to the table row height so the wallet tree lines up
        # with the token/tx tables beside it (a QTreeView's natural rows
        # are shorter). No effect on the tables — they already sit there.
        size = super().sizeHint(option, index)
        floor = _table_row_height()
        if size.height() < floor:
            size.setHeight(floor)
        return size

    def _row_fill_rect(self, option, index):
        """The rect to fill for a selected row, extended left over the
        tree's indent column (column 0). The view leaves that strip to the
        row background and the delegate's own rect starts after it, so a
        fill confined to option.rect leaves a gap there. A no-op in the
        tables, where column 0 already starts at the left edge."""
        if index.column() == 0:
            return option.rect.adjusted(-option.rect.left(), 0, 0, 0)
        return option.rect

    def _redraw_disclosure(self, painter, option, index):
        """Redraw the tree's +/- disclosure control ON TOP of a selected group
        row's fill. The fill extends over the indent column (to close a Kvantum
        gap), so it covers the disclosure the style drew *before* the delegate —
        this puts it back, visible and within the selection highlight. No-op for
        leaf rows / the flat tables (no children → no disclosure)."""
        view = self.parent()
        model = index.model()
        if (not isinstance(view, QTreeView) or model is None
                or model.rowCount(index) <= 0):     # leaf row / flat table
            return
        indent = view.indentation()
        opt = QStyleOption()
        opt.rect = QRect(option.rect.left() - indent, option.rect.top(),
                         indent, option.rect.height())
        opt.palette = option.palette
        opt.state = (QStyle.StateFlag.State_Item | QStyle.StateFlag.State_Children
                     | QStyle.StateFlag.State_Enabled)
        if view.isExpanded(index):
            opt.state |= QStyle.StateFlag.State_Open
        view.style().drawPrimitive(
            QStyle.PrimitiveElement.PE_IndicatorBranch, opt, painter, view)

    def paint(self, painter, option, index):
        view = self.parent()
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_focused = isinstance(view, QWidget) and view.hasFocus()

        # A labeled wallet row draws the label as a sticky-note pill on the
        # right; reserve its width so the address text (which the tree
        # elides) shrinks to make room, leaving only the label tinted. An
        # account label (yellow) wins over a device-tree label (blue) when a
        # row somehow carries both — though in practice they're on different
        # rows (leaf vs subgroup).
        label = index.data(ACCOUNT_LABEL_ROLE)
        pill_bg, pill_fg = _STICKY_BG, _STICKY_FG
        if not label:
            label = index.data(TREE_LABEL_ROLE)
            pill_bg, pill_fg = _TREE_STICKY_BG, _TREE_STICKY_FG
        pill = self._sticky_pill_rect(option, label) if label else None
        text_rect = (
            option.rect.adjusted(0, 0, -(pill.width() + _STICKY_GAP), 0)
            if pill else option.rect
        )

        if is_selected and is_focused:
            # Fill the row with the highlight colour first — extended left
            # over the tree's indent column (a no-op in the tables) so the
            # fill reaches the edge the same way the muted unfocused fill
            # does; under some styles (Kvantum) the view leaves that strip
            # unpainted, which otherwise shows as a white gap only here.
            highlight = option.palette.color(QPalette.ColorRole.Highlight)
            painter.fillRect(self._row_fill_rect(option, index), highlight)
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
            self._draw_sticky_pill(painter, pill, label, option.font,
                                   pill_bg, pill_fg)
            self._redraw_disclosure(painter, option, index)
            return

        if is_selected and not is_focused:
            # Inactive selection: a *muted* fill — the highlight blended
            # toward the row background — over the whole row, contents on
            # top. Hand-painted exactly like the focused fill (rather than
            # left to the style, which a cell stylesheet on the tables
            # suppresses entirely, and which would only cover part of a
            # labelled tree row), but in a quieter colour so the focused
            # panel still stands out. Replaces a hand-drawn outline.
            hl = option.palette.color(QPalette.ColorRole.Highlight)
            bg = option.palette.color(QPalette.ColorRole.Base)
            muted = QColor(
                (hl.red() * 11 + bg.red() * 9) // 20,
                (hl.green() * 11 + bg.green() * 9) // 20,
                (hl.blue() * 11 + bg.blue() * 9) // 20,
            )
            painter.fillRect(self._row_fill_rect(option, index), muted)
            text_color = option.palette.color(
                QPalette.ColorRole.HighlightedText
                if muted.lightness() < 140
                else QPalette.ColorRole.Text)
            opt = QStyleOptionViewItem(option)
            opt.rect = text_rect
            opt.state &= ~QStyle.StateFlag.State_Selected
            opt.state &= ~QStyle.StateFlag.State_HasFocus
            opt.palette.setColor(QPalette.ColorRole.Text, text_color)
            opt.palette.setColor(QPalette.ColorRole.WindowText, text_color)
            opt.palette.setColor(QPalette.ColorRole.Base, muted)
            opt.palette.setColor(QPalette.ColorRole.AlternateBase, muted)
            super().paint(painter, opt, index)
            self._draw_sticky_pill(painter, pill, label, option.font,
                                   pill_bg, pill_fg)
            self._redraw_disclosure(painter, option, index)
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
        self._draw_sticky_pill(painter, pill, label, option.font,
                               pill_bg, pill_fg)


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
