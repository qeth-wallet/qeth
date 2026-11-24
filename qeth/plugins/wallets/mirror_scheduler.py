"""The timer that keeps a hardware wallet's local metadata copy fresh — the
Ledger token mirror (``ledger_mirror.py``), Trezor's definitions
(``trezor_mirror.py``). Deliberately decoupled from signing: it runs while the
store holds an account of that device, never because a signature is about to
happen, so a lookup at signing time tells no server anything."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer

log = logging.getLogger(__name__)


class MirrorScheduler(QObject):
    """Checks hourly — the first check a minute or two after start, jittered —
    and starts ``make_worker()`` when ``needs_refresh()``. A worker emits
    ``done(result)`` or ``failed(message)``; one runs at a time."""

    CHECK_INTERVAL_MS = 3600 * 1000
    FIRST_CHECK_MS = 60 * 1000
    FIRST_CHECK_JITTER_MS = 120 * 1000
    SOURCE = ""          # the account source this copy serves
    LABEL = ""           # for the log

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

    def _has_device(self) -> bool:
        return any(a.get("source") == self.SOURCE for a in self._store.accounts)

    def needs_refresh(self) -> bool:
        raise NotImplementedError

    def make_worker(self) -> QThread:
        raise NotImplementedError

    def check(self) -> None:
        """Refresh if needed. Also called right after an account of the device
        is added, so a new user's copy fills before their first signature."""
        if self._running or not self._has_device() or not self.needs_refresh():
            return
        self._running = True
        worker: Any = self.make_worker()
        worker.done.connect(self._on_done)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(self._on_finished)
        self._start_worker(worker)

    def _on_done(self, result: object) -> None:
        log.info("%s refreshed (%s)", self.LABEL, result)

    def _on_failed(self, message: str) -> None:
        log.info("%s refresh failed, retrying later: %s", self.LABEL, message)

    def _on_finished(self) -> None:
        self._running = False
