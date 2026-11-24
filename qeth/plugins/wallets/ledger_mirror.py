"""Keeps the local mirror of Ledger's signed token metadata fresh
(``qeth.ledger_metadata``) — the only network side of Ledger clear signing,
and deliberately decoupled from signing: it runs on a timer while the store
holds a Ledger account, never because a signature is about to happen, so a
lookup at signing time tells no server anything (docs/ledger-clear-signing.md).
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ...ledger_metadata import default_mirror

log = logging.getLogger(__name__)


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


class LedgerMirrorScheduler(QObject):
    """Checks hourly — the first check a minute or two after start, jittered
    — and refreshes the mirror when it's stale (``REFRESH_INTERVAL_S``) or a
    chain is missing. A revalidation that finds nothing new is one request."""

    CHECK_INTERVAL_MS = 3600 * 1000
    FIRST_CHECK_MS = 60 * 1000
    FIRST_CHECK_JITTER_MS = 120 * 1000

    def __init__(self, store: Any, start_worker: Callable[[QThread], QThread],
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._start_worker = start_worker
        self._running = False
        self._timer = QTimer(self)
        self._timer.setInterval(self.CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self.check)
        self._first = QTimer(self)
        self._first.setSingleShot(True)
        self._first.timeout.connect(self.check)

    def start(self) -> None:
        self._timer.start()
        self._first.start(self.FIRST_CHECK_MS
                          + random.randrange(self.FIRST_CHECK_JITTER_MS))

    def stop(self) -> None:
        self._timer.stop()
        self._first.stop()

    def _has_ledger(self) -> bool:
        return any(a.get("source") == "ledger" for a in self._store.accounts)

    def chain_ids(self) -> list[int]:
        """Every EVM chain qeth knows, used or not — mirroring only the
        chains in use would itself say which ones those are."""
        return sorted({c.chain_id for c in self._store.chains if c.is_evm})

    def check(self) -> None:
        """Refresh if needed. Also called right after a Ledger account is
        added, so a new user's mirror fills before their first signature."""
        if self._running or not self._has_ledger():
            return
        chains = self.chain_ids()
        if not default_mirror().needs_refresh(chains):
            return
        self._running = True
        worker = LedgerMirrorWorker(chains)
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_finished)
        self._start_worker(worker)

    def _on_done(self, fetched: int) -> None:
        log.info("ledger token mirror refreshed (%d networks downloaded)", fetched)

    def _on_failed(self, message: str) -> None:
        log.info("ledger token mirror refresh failed, retrying later: %s", message)

    def _on_finished(self) -> None:
        self._running = False
