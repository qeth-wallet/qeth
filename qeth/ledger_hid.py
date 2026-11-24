"""Single-thread Ledger/HID execution service.

hidapi's macOS backend ties HID enumeration/open/close to CoreFoundation
run-loop state, so the handle is only valid on the thread that opened it.
qeth drives Ledger work from transient Qt worker threads (the signing
worker, the account-discovery worker, the availability probe), and letting
each touch HID directly corrupts that state and crashes/hangs on macOS.

The fix: funnel every hidapi call through ONE process-lifetime thread.
Callers submit a closure and block on a Future; the service runs jobs strictly
serialized on its own thread. Each job opens the device and closes it
(``ledger_app.connect``), so a stale USB-HID handle never carries between
operations.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import TypeVar

from .device_thread import DEFAULT_DEVICE_TIMEOUT_S, DeviceJobService

T = TypeVar("T")

# Generous: a job can include a human confirming a tx on the device.
DEFAULT_LEDGER_HID_TIMEOUT_S = DEFAULT_DEVICE_TIMEOUT_S


class LedgerHidService(DeviceJobService):
    """Runs Ledger/HID jobs on one process-lifetime worker thread."""

    def __init__(self, *, name: str = "qeth-ledger-hid") -> None:
        super().__init__(name=name, device_label="Ledger")


_SERVICE: LedgerHidService | None = None
_SERVICE_LOCK = threading.Lock()


def ledger_hid_service() -> LedgerHidService:
    """Return the process-wide Ledger HID service (created on first use)."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = LedgerHidService()
        return _SERVICE


def submit_ledger_hid_job(fn: Callable[[], T]) -> Future[T]:
    return ledger_hid_service().submit(fn)


def run_ledger_hid_job(
    fn: Callable[[], T],
    *,
    timeout: float = DEFAULT_LEDGER_HID_TIMEOUT_S,
) -> T:
    return ledger_hid_service().call(fn, timeout=timeout)
