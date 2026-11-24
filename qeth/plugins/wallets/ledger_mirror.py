"""Keeps the local mirror of Ledger's signed token metadata fresh
(``qeth.ledger_metadata``) — the only network side of Ledger clear signing,
and deliberately decoupled from signing: it runs on a timer while the store
holds a Ledger account, never because a signature is about to happen, so a
lookup at signing time tells no server anything (docs/ledger-clear-signing.md).
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal

from ...ledger_metadata import default_mirror
from .mirror_scheduler import MirrorScheduler


class LedgerMirrorWorker(QThread):
    """One mirror refresh off the main thread."""

    done = Signal(int)      # networks (re)downloaded
    failed = Signal(str)

    def __init__(self, chain_ids: list[int], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._chain_ids = chain_ids

    def run(self) -> None:
        try:
            self.done.emit(default_mirror().refresh(self._chain_ids))
        except Exception as e:   # noqa: BLE001 — retried on the next check
            self.failed.emit(str(e))


class LedgerMirrorScheduler(MirrorScheduler):
    """Refreshes the mirror when it's stale (``REFRESH_INTERVAL_S``) or a
    chain is missing. A revalidation that finds nothing new is one request."""

    SOURCE = "ledger"
    LABEL = "ledger token mirror"

    def chain_ids(self) -> list[int]:
        """Every EVM chain qeth knows, used or not — mirroring only the
        chains in use would itself say which ones those are."""
        return sorted({c.chain_id for c in self._store.chains if c.is_evm})

    def needs_refresh(self) -> bool:
        return default_mirror().needs_refresh(self.chain_ids())

    def make_worker(self) -> QThread:
        return LedgerMirrorWorker(self.chain_ids())
