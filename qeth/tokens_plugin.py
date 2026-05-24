"""TokensPlugin — wraps the existing TokenListPanel as a Plugin.

Transitional in step 2 of the refactor: the panel and its action
buttons move into a Plugin contract, but the refresh / caches /
workers still live on MainWindow. Step 3 will pull those in too,
making this plugin self-contained like ``TransactionsPlugin``.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QWidget

from .plugin import Plugin


class TokensPlugin(Plugin):
    name = "Tokens"

    def __init__(self, store, icon_cache):
        super().__init__()
        self._store = store
        self._icon_cache = icon_cache
        self._panel = None

    @property
    def token_panel(self):
        # Convenience alias used by MainWindow + tests during the
        # transition. Once the plugin is fully self-contained, callers
        # should go through the plugin's own API instead.
        return self.widget()

    def widget(self) -> QWidget:
        if self._panel is None:
            from .ui import TokenListPanel
            self._panel = TokenListPanel(self._icon_cache, self._store)
        return self._panel

    def action_widgets(self):
        # The +/-/★/👁 buttons live inside the panel; expose them so
        # the slot's bottom row gets them when this plugin is active.
        return self._panel.action_widgets() if self._panel is not None else []

    # --- lifecycle hooks ----------------------------------------------------
    # Logic still lives in MainWindow; hooks just delegate. Step 3 will
    # invert this so the plugin owns its workers/caches.

    def on_account_changed(self, address: Optional[str]) -> None:
        if self.host is None:
            return
        if address is None:
            if self._panel is not None:
                self._panel.clear()
            return
        self.host.refresh_tokens(address)

    def on_chain_changed(self) -> None:
        if self.host is None:
            return
        addr = self.host.selected_address
        if addr is not None:
            self.host.refresh_tokens(addr)
