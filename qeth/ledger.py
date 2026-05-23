from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal

from .chain import EthClient
from .chains import Chain


LEDGER_LIVE = "44'/60'/{i}'/0/0"
LEGACY = "44'/60'/0'/{i}"
BIP44 = "44'/60'/0'/0/{i}"

PATH_SCHEMES: dict[str, str] = {
    "Ledger Live": LEDGER_LIVE,
    "Legacy": LEGACY,
    "BIP44 Standard": BIP44,
}

AUTO_STOP_CONSECUTIVE_ZEROS = 3
AUTO_DETECT_HARD_CAP = 100


@dataclass
class DiscoveredAccount:
    address: str
    path: str
    index: int
    balance_wei: int = 0


class LedgerWorker(QThread):
    """Enumerates Ledger accounts in a background thread.

    If `count` is 0, scans until `AUTO_STOP_CONSECUTIVE_ZEROS` empty accounts
    in a row (up to `AUTO_DETECT_HARD_CAP`). Balances are fetched from
    `chain` if provided.
    """

    discovered = Signal(object)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, scheme: str, count: int, chain: Chain | None = None, parent=None):
        super().__init__(parent)
        self.scheme = scheme
        self.count = count
        self.client = EthClient(chain) if chain is not None else None

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

        auto = self.count == 0
        max_scan = AUTO_DETECT_HARD_CAP if auto else self.count
        consecutive_zero = 0

        try:
            for i in range(max_scan):
                path = template.format(i=i)
                acct = get_account_by_path(path, dongle=dongle)
                balance = self._balance(acct.address) if self.client else 0
                self.discovered.emit(DiscoveredAccount(
                    address=acct.address, path=path, index=i, balance_wei=balance
                ))
                if auto:
                    if balance == 0:
                        consecutive_zero += 1
                        if consecutive_zero >= AUTO_STOP_CONSECUTIVE_ZEROS:
                            break
                    else:
                        consecutive_zero = 0
            self.finished_ok.emit()
        except Exception as e:
            self.failed.emit(f"Error reading account: {e}")

    def _balance(self, address: str) -> int:
        try:
            return self.client.get_balance(address)
        except Exception:
            return 0
