"""Measure qeth startup phases with the user's real
``~/.qeth/config.json``. Times each major step on the main thread
so we can tell which work is CPU-bound vs IO-bound vs
worker-thread (background, non-blocking).

Not a pytest test (no asserts) — a manual benchmark you run when
you suspect startup has regressed. Lives next to the assertion-
based tests; pytest won't auto-collect it because the filename
doesn't match the ``test_*.py`` glob.

Run with::

    QT_QPA_PLATFORM=offscreen uv run python tests/profile_startup.py
"""

import functools
import logging
import sys
import time

logging.basicConfig(level=logging.WARNING)


def _wrap(obj, name: str, label: str, counter: dict):
    orig = getattr(obj, name)

    @functools.wraps(orig)
    def w(*a, **kw):
        t0 = time.perf_counter()
        try:
            return orig(*a, **kw)
        finally:
            dt = (time.perf_counter() - t0) * 1000
            counter[label] = counter.get(label, [0, 0.0])
            counter[label][0] += 1
            counter[label][1] += dt
    setattr(obj, name, w)


def main() -> int:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from qeth.chain import _ensure_heavy_imports
    from qeth.rpc import RpcServer
    from qeth.store import Store
    from qeth.ui import MainWindow
    from qeth.plugins.transactions import (
        TransactionsPlugin, TransactionListPanel,
    )
    from qeth.plugins.tokens import TokensPlugin
    from qeth.plugins.wallets import WalletsPlugin

    app = QApplication.instance() or QApplication(sys.argv)

    # Time the heavy import-warmup step (web3/eth_utils — already
    # done at import time the first time we touch them, so this
    # is a no-op once warmed up but still good to confirm).
    t = time.perf_counter()
    _ensure_heavy_imports()
    t_heavy = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    store = Store.load()
    t_store = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    rpc = RpcServer(store)
    rpc.start()
    t_rpc = (time.perf_counter() - t) * 1000

    # Instrument the hot spots before constructing MainWindow.
    counter: dict = {}
    _wrap(TransactionListPanel, "show_transactions", "panel.show_transactions", counter)
    _wrap(TransactionListPanel, "_populate_row", "panel._populate_row", counter)
    _wrap(TransactionsPlugin, "__init__", "TransactionsPlugin.__init__", counter)
    _wrap(TokensPlugin, "__init__", "TokensPlugin.__init__", counter)
    _wrap(WalletsPlugin, "__init__", "WalletsPlugin.__init__", counter)

    t = time.perf_counter()
    win = MainWindow(store, rpc)
    t_window = (time.perf_counter() - t) * 1000

    t = time.perf_counter()
    win.show()
    app.processEvents()
    t_first_paint = (time.perf_counter() - t) * 1000

    print("\n--- main-thread blocking phases ---")
    print(f"  ensure_heavy_imports:    {t_heavy:8.1f} ms")
    print(f"  Store.load:              {t_store:8.1f} ms")
    print(f"  RpcServer.start:         {t_rpc:8.1f} ms")
    print(f"  MainWindow(store, rpc):  {t_window:8.1f} ms")
    print(f"  show() + first paint:    {t_first_paint:8.1f} ms")
    print(f"  TOTAL blocking:          "
          f"{t_heavy + t_store + t_rpc + t_window + t_first_paint:8.1f} ms")
    print("\n--- per-method counters during MainWindow init ---")
    rows = sorted(counter.items(), key=lambda kv: -kv[1][1])
    for name, (n, total_ms) in rows:
        print(f"  {name:32s}  n={n:5d}   total={total_ms:7.1f} ms"
              f"   avg={total_ms/n:6.3f} ms")
    print(f"\nconfig: {len(store.accounts)} accounts, "
          f"{len(store.chains)} chains, "
          f"default={store.default_account}")

    # Brief tick of the event loop to drain Qt-internal initial
    # work but then exit so the profile isn't dominated by idle.
    QTimer.singleShot(50, app.quit)
    app.exec()
    rpc.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
