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

    app = QApplication(sys.argv)
    app.setApplicationName("qeth")
    app.setOrganizationName("qeth")

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
