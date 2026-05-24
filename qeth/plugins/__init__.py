"""qeth topic plugins.

Each module exports a ``Plugin`` subclass that owns its data sources,
caches, workers, panel widget, and lifecycle. The plugins are loaded
into ``Slot``s by MainWindow and don't know about each other; cross-
plugin events (selection broadcasts, chain changes) flow through the
``Host`` protocol implemented by MainWindow.

The base ``Plugin``, ``Slot``, and ``Host`` types live in
``qeth.plugin`` — they're the framework, not topics, so they stay
outside this package.
"""

from .tokens import TokensPlugin
from .transactions import TransactionsPlugin
from .wallets import WalletsPlugin

__all__ = ["TokensPlugin", "TransactionsPlugin", "WalletsPlugin"]
