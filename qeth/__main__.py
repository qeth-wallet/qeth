import locale
import logging
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from .rpc import RpcServer
from .store import Store
from .ui import MainWindow


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Honor the user's LC_TIME for strftime("%x %X") in tx timestamps.
    # Python starts in the POSIX C locale until something flips it;
    # without this call all timestamps would render as MM/DD/YY.
    try:
        locale.setlocale(locale.LC_TIME, "")
    except locale.Error:
        # Misconfigured environment (e.g. LC_ALL set to a locale that
        # isn't installed). Fall back silently; strftime works in C.
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("qeth")
    app.setOrganizationName("qeth")

    # Pull the web3/eth_abi/requests stack now, while no window
    # exists yet. They're deferred at import time (qeth.chain only
    # loads them on first EthClient construction); leaving the
    # trigger to a worker thread firing right after win.show() puts
    # the ~400 ms of Python module init in GIL contention with the
    # main thread's first paint, leaving the window frame visible
    # but its contents blank until the import finishes.
    from .chain import _ensure_heavy_imports
    _ensure_heavy_imports()

    store = Store.load()
    rpc = RpcServer(store)
    rpc.start()

    if rpc.error:
        QMessageBox.warning(
            None,
            "qeth — JSON-RPC failed to start",
            f"Could not bind to {rpc.host}:{rpc.port}.\n\n{rpc.error}\n\n"
            "Frame may already be running. The wallet UI will still work, "
            "but dapps won't be able to connect.",
        )

    win = MainWindow(store, rpc)
    win.show()
    try:
        return app.exec()
    finally:
        rpc.stop()


if __name__ == "__main__":
    sys.exit(main())
