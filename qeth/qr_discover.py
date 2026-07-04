"""Offline account discovery for an imported QR wallet: derive addresses locally
from the exported account xpub (no more QR exchanges) and look up each nonce to
surface used accounts — the air-gapped analogue of ``LedgerWorker``. Emits the
same ``DiscoveredAccount`` so the add dialog is shared.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

from .ledger import (
    AUTO_DETECT_HARD_CAP,
    AUTO_STOP_CONSECUTIVE_ZEROS,
    DiscoveredAccount,
)
from .qr.derive import derive_address
from .qr.schemes import QR_ADDRESS_SCHEMES, full_path

if TYPE_CHECKING:
    from .chains import Chain
    from .qr.account import AccountKey


class QRAccountWorker(QThread):
    """Derive ``count`` addresses under an imported xpub for a scheme (or
    auto-detect until ``AUTO_STOP_CONSECUTIVE_ZEROS`` unused), with nonces from
    ``chain`` when given. Derivation is local + fast; only the nonce lookup
    touches the network."""

    discovered = Signal(object)   # DiscoveredAccount
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, account_key: AccountKey, scheme: str, count: int,
                 chain: Chain | None = None, parent=None) -> None:
        super().__init__(parent)
        self._key = account_key
        self._scheme = scheme
        self._count = count
        self._chain = chain

    def run(self) -> None:
        suffix_for = QR_ADDRESS_SCHEMES.get(self._scheme)
        if suffix_for is None:
            self.failed.emit(f"Unknown derivation scheme: {self._scheme}")
            return
        client = None
        if self._chain is not None:
            from .chain import EthClient
            client = EthClient(self._chain)

        auto = self._count == 0
        cap = AUTO_DETECT_HARD_CAP if auto else self._count
        consecutive_unused = 0
        try:
            for index in range(cap):
                suffix = suffix_for(index)
                address = derive_address(
                    self._key.pubkey, self._key.chain_code, suffix)
                nonce = 0
                if client is not None:
                    try:
                        nonce = client.get_transaction_count(address, "latest")
                    except Exception:
                        nonce = 0   # a lookup hiccup shouldn't drop the row
                self.discovered.emit(DiscoveredAccount(
                    address=address,
                    path=full_path(self._key.origin_path, suffix),
                    index=index,
                    nonce=nonce,
                ))
                if auto:
                    consecutive_unused = consecutive_unused + 1 if nonce == 0 else 0
                    if consecutive_unused >= AUTO_STOP_CONSECUTIVE_ZEROS:
                        break
        except Exception as e:   # noqa: BLE001 — surface any derivation failure
            self.failed.emit(str(e))
            return
        self.finished_ok.emit()
