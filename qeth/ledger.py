from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal


LEDGER_LIVE = "44'/60'/{i}'/0/0"
LEGACY = "44'/60'/0'/{i}"
BIP44 = "44'/60'/0'/0/{i}"

PATH_SCHEMES: dict[str, str] = {
    "Ledger Live": LEDGER_LIVE,
    "Legacy": LEGACY,
    "BIP44 Standard": BIP44,
}


@dataclass
class DiscoveredAccount:
    address: str
    path: str
    index: int


class LedgerWorker(QThread):
    """Enumerates Ledger accounts in a background thread.

    Emits `discovered` per account, then `finished_ok`, or `failed` with message.
    """

    discovered = Signal(object)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, scheme: str, count: int, parent=None):
        super().__init__(parent)
        self.scheme = scheme
        self.count = count

    def run(self) -> None:
        try:
            from ledgereth.accounts import get_account_by_path
            from ledgereth.comms import init_dongle
        except ImportError as e:
            self.failed.emit(f"ledgereth not installed: {e}")
            return

        try:
            dongle = init_dongle()
        except Exception as e:
            self.failed.emit(
                "Could not open Ledger. Make sure the device is connected, "
                f"unlocked, and the Ethereum app is open.\n\n{e}"
            )
            return

        template = PATH_SCHEMES.get(self.scheme)
        if template is None:
            self.failed.emit(f"Unknown derivation scheme: {self.scheme}")
            return

        try:
            for i in range(self.count):
                path = template.format(i=i)
                acct = get_account_by_path(path, dongle=dongle)
                self.discovered.emit(
                    DiscoveredAccount(address=acct.address, path=path, index=i)
                )
            self.finished_ok.emit()
        except Exception as e:
            self.failed.emit(f"Error reading account: {e}")
