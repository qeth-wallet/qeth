"""Plugin + Slot — building blocks for the topic-based UI refactor.

A ``Plugin`` is a self-contained UI module: a widget, optional action
widgets shown on the slot's bottom row, and lifecycle hooks for
account/chain/activation changes. A ``Slot`` is a pane (left or right
column) that hosts one or more plugins; it shows a tab bar only when
≥2 plugins are mounted, otherwise the single plugin's widget fills it.

The aim is to decouple topics (Wallets, Tokens, Transactions, …) from
the specific pane they happen to live in, so adding a fourth topic
later (NFTs, signing queue, settings) is one ``add_plugin`` call.
Plugins are tested in isolation; ``Slot`` carries the only structural
logic and is tested directly in ``tests/test_plugin.py``.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Optional, Protocol

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QStackedWidget, QTabBar, QVBoxLayout, QWidget,
)


class Host(Protocol):
    """Services a plugin needs from its host (MainWindow).

    Defining this as a Protocol lets plugins depend on the *shape* of
    what they need rather than the concrete MainWindow class — both
    keeps the coupling explicit and makes plugin tests easy (pass a
    minimal stand-in object that quacks the same)."""

    @property
    def selected_address(self) -> Optional[str]:
        ...

    def current_chain(self):
        """Return the qeth.chains.Chain currently selected by the user."""

    def chain_by_id(self, chain_id: int):
        """Return the configured ``qeth.chains.Chain`` whose id matches,
        or None if no such chain is configured. Used by the pending-tx
        watcher which polls receipts on whatever chain a tx was sent
        on, not necessarily the user's current view."""

    def start_worker(self, worker: QThread) -> QThread:
        """Register a QThread so the host keeps it alive while running."""

    def status_message(self, text: str, timeout_ms: int = 3000) -> None:
        """Show a transient message in the status bar."""

    def token_info(self, chain_id: int, address: str):
        """Curated token metadata for (chain_id, address), or None.
        Returns a ``qeth.tokenlists.TokenListEntry`` when the address
        is in the merged whitelist — used by sibling plugins (e.g. the
        transactions details dialog) to annotate contract addresses
        with symbol + icon without coupling to TokensPlugin."""

    def icon_cache(self):
        """The shared ``qeth.icons.IconCache`` — used by sibling
        plugins that need to display token icons (after looking up
        the contract via ``token_info``)."""

    def native_price_usd(self, chain_id: int, address: str):
        """Latest cached USD-per-native price for the given
        (chain_id, address). Returns a ``Decimal`` when there's a
        cached entry, ``None`` otherwise. Sourced from the wallet
        cache TokensPlugin already maintains; used by the
        transactions dialogs to annotate fees with their dollar
        value without re-fetching prices."""

    def request_transaction(self, req, chain, label: str,
                            on_broadcast=None, on_confirmed=None) -> None:
        """Open the review + sign + broadcast flow for a locally-built
        transaction (``req`` is a ``signing.SigningRequest`` with
        ``to_addr`` / ``data`` set). Reuses the Send / Speed-up
        pipeline: gas + fee + nonce estimation, simulation preview,
        Ledger / hot-wallet signing, and pending-tx tracking.
        ``on_broadcast(tx_hash)`` fires after a successful broadcast;
        ``on_confirmed(receipt)`` fires once when the tx mines. Used by
        the ENS plugin's record / subdomain writes."""


class Plugin(QObject):
    """One topic in the UI. Subclasses define a ``name`` class attribute
    (the tab label) and implement ``widget()``. Everything else is
    optional and defaults to a no-op.

    Inherits from QObject so subclasses can declare ``Signal(...)`` for
    cross-plugin events (e.g. WalletsPlugin.selected_address_changed)."""

    name: str = ""

    def __init__(self) -> None:
        super().__init__()
        # ``host`` is wired in attach(); plugins reach back through it
        # rather than receiving five constructor args.
        self.host: Optional[Host] = None

    @abstractmethod
    def widget(self) -> QWidget:
        """The plugin's main widget. Built once; ``Slot`` keeps a
        reference and reuses it across tab switches."""

    def action_widgets(self) -> list[QWidget]:
        """Buttons/widgets the slot mounts on its bottom row when this
        plugin is active. Empty list = no actions for this plugin."""
        return []

    def attach(self, host: Host) -> None:
        """Called by ``Slot.add_plugin``. Subclasses can override to do
        first-time setup that depends on the host; remember to call
        ``super().attach(host)`` so ``self.host`` gets wired up."""
        self.host = host

    # --- lifecycle hooks (the host calls these) --------------------------

    def on_account_changed(self, address: Optional[str]) -> None:
        """Fires when the user picks a different wallet."""

    def on_chain_changed(self) -> None:
        """Fires when the user switches networks."""

    def on_activated(self) -> None:
        """Fires when this plugin becomes the active tab.
        Useful for lazy data loading."""


class Slot(QWidget):
    """A column that hosts one or more plugins.

    Structure (top to bottom):
        - QTabBar (hidden when only one plugin is mounted)
        - QStackedWidget — the active plugin's widget
        - bottom row — active plugin's action widgets, stretch,
                       slot-level shared widgets (chain selector, etc.)

    Tab visibility follows the rule "show tabs only when there's a
    choice to make". One plugin → no chrome above the widget.
    """

    active_plugin_changed = Signal(object)   # emits the new Plugin

    def __init__(self, parent: Optional[QWidget] = None,
                 show_single_tab: bool = False):
        super().__init__(parent)
        self._plugins: list[Plugin] = []
        # Normally the tab bar appears only with ≥2 plugins ("show tabs
        # only when there's a choice"). A slot can opt to show its single
        # tab anyway, so its top chrome matches a sibling multi-plugin
        # slot's tab bar and their lists line up (the Wallets slot does
        # this — see MainWindow._build_central).
        self._show_single_tab = show_single_tab

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tab_bar = QTabBar()
        self._tab_bar.setDocumentMode(True)
        self._tab_bar.setExpanding(False)
        self._tab_bar.setVisible(False)
        self._tab_bar.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self._tab_bar)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)

        # Bottom row is [ plugin_actions | stretch | shared widgets ].
        # plugin_actions is a sub-layout that gets cleared and refilled
        # on tab change; shared widgets are added directly to the right
        # of the stretch and survive plugin switches.
        self._bottom = QHBoxLayout()
        self._bottom.setContentsMargins(4, 2, 4, 4)
        self._plugin_actions = QHBoxLayout()
        self._plugin_actions.setContentsMargins(0, 0, 0, 0)
        self._bottom.addLayout(self._plugin_actions)
        self._bottom.addStretch(1)
        layout.addLayout(self._bottom)

    # --- mounting -------------------------------------------------------

    def add_plugin(self, plugin: Plugin, host: Host) -> None:
        """Mount a plugin in this slot. ``host`` is forwarded to
        ``plugin.attach()`` so the plugin can talk back.

        Adding the first plugin transitions the tab bar's current index
        from -1 to 0, which fires ``currentChanged`` and runs the
        normal activation path (stack swap, action-row rebuild,
        ``on_activated`` hook). Subsequent tabs are added behind the
        scenes without changing the current index — no activation
        fires, which is what we want for lazy plugins."""
        plugin.attach(host)
        self._plugins.append(plugin)
        self._stack.addWidget(plugin.widget())
        self._tab_bar.addTab(plugin.name)
        # Tab bar appears with ≥2 plugins, or when this slot opts to show
        # its single tab (so its chrome height matches a sibling slot).
        self._tab_bar.setVisible(self._show_single_tab or len(self._plugins) > 1)

    def add_shared_widget(self, widget: QWidget) -> None:
        """Add a widget to the bottom row that persists across plugin
        switches. Mounted on the right side (after the stretch), so
        slot-shared widgets (e.g. the chain combo) cluster on the right
        and plugin-specific actions stay on the left."""
        self._bottom.addWidget(widget)

    # --- queries --------------------------------------------------------

    def plugins(self) -> list[Plugin]:
        return list(self._plugins)

    def active(self) -> Optional[Plugin]:
        idx = self._stack.currentIndex()
        if 0 <= idx < len(self._plugins):
            return self._plugins[idx]
        return None

    def set_active(self, plugin: Plugin) -> None:
        """Switch to a specific plugin. No-op if the plugin isn't mounted."""
        try:
            idx = self._plugins.index(plugin)
        except ValueError:
            return
        self._tab_bar.setCurrentIndex(idx)
        if not self._tab_bar.isVisible():
            # Single-plugin slot: tab bar is hidden so currentChanged
            # won't fire; drive the stack and action row by hand.
            self._stack.setCurrentIndex(idx)
            self._rebuild_action_row()

    # --- broadcasts to mounted plugins ----------------------------------

    def broadcast_account_changed(self, address: Optional[str]) -> None:
        for p in self._plugins:
            p.on_account_changed(address)

    def broadcast_chain_changed(self) -> None:
        for p in self._plugins:
            p.on_chain_changed()

    # --- internals ------------------------------------------------------

    def _on_tab_changed(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        self._rebuild_action_row()
        plugin = self.active()
        if plugin is not None:
            plugin.on_activated()
        self.active_plugin_changed.emit(plugin)

    def _rebuild_action_row(self) -> None:
        """Empty the plugin-actions sub-layout and repopulate it from
        the active plugin's ``action_widgets()``. Shared widgets (on
        the right of the stretch) are untouched."""
        while self._plugin_actions.count():
            item = self._plugin_actions.takeAt(0)
            assert item is not None  # count() > 0 guarantees an item
            w = item.widget()
            if w is not None:
                # Reparent away so the layout no longer manages it; the
                # plugin keeps a Python ref so the C++ widget survives.
                w.setParent(None)
        plugin = self.active()
        if plugin is None:
            return
        for w in plugin.action_widgets():
            self._plugin_actions.addWidget(w)
