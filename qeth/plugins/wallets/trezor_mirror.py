"""Keeps the local copy of Trezor's signed definitions fresh
(``qeth.trezor_definitions``) while the store holds a Trezor account — never
because a signature is about to happen, so answering the device from the copy
tells no server what is being signed (docs/trezor-clear-signing.md)."""

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal

from ...trezor_definitions import default_definitions
from .mirror_scheduler import MirrorScheduler


class TrezorMirrorWorker(QThread):
    """One archive revalidation (and, when it changed, download + split) off
    the main thread."""

    done = Signal(bool)     # a new archive was downloaded
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def run(self) -> None:
        try:
            self.done.emit(default_definitions().refresh())
        except Exception as e:   # noqa: BLE001 — retried on the next check
            self.failed.emit(str(e))


class TrezorMirrorScheduler(MirrorScheduler):
    """Revalidates the archive daily (``REFRESH_INTERVAL_S``); an unchanged
    archive is one request."""

    SOURCE = "trezor"
    LABEL = "trezor definitions"

    def needs_refresh(self) -> bool:
        return default_definitions().needs_refresh()

    def make_worker(self) -> QThread:
        return TrezorMirrorWorker()
