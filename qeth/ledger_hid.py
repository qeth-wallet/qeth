"""Single-thread Ledger/HID execution service.

hidapi's macOS backend ties HID enumeration/open/close to CoreFoundation
run-loop state, so the handle is only valid on the thread that opened it.
qeth drives Ledger work from transient Qt worker threads (the signing
worker, the account-discovery worker, the availability probe), and letting
each touch HID directly corrupts that state and crashes/hangs on macOS.

The fix: funnel every ledgereth/hidapi call through ONE process-lifetime
thread. Callers submit a closure and block on a Future; the service runs
jobs strictly serialized on its own thread and clears ledgereth's global
dongle cache after each (close + null the handle), so a stale USB-HID
handle never carries between operations.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import TypeVar

from .device_thread import DEFAULT_DEVICE_TIMEOUT_S, DeviceJobService

T = TypeVar("T")

# Generous: a job can include a human confirming a tx on the device.
DEFAULT_LEDGER_HID_TIMEOUT_S = DEFAULT_DEVICE_TIMEOUT_S


class LedgerHidService(DeviceJobService):
    """Runs Ledger/HID jobs on one process-lifetime worker thread, clearing
    ledgereth's dongle cache after every job."""

    def __init__(self, *, name: str = "qeth-ledger-hid") -> None:
        super().__init__(
            name=name, device_label="Ledger", after_job=_clear_ledgereth_cache)


def _clear_ledgereth_cache() -> None:
    """Close and clear ledgereth's process-global dongle cache.

    ledgereth keeps the Dongle handle in module-level slots across calls;
    reusing one between operations is unreliable (the USB-HID session goes
    stale and the next exchange fails without waking the device). Reading
    via ``sys.modules`` avoids importing ledgereth when it isn't installed."""
    _comms = sys.modules.get("ledgereth.comms")
    if _comms is None:
        return
    cached = getattr(_comms, "DONGLE_CACHE", None)
    setattr(_comms, "DONGLE_CACHE", None)
    setattr(_comms, "DONGLE_CONFIG_CACHE", None)
    if cached is not None:
        try:
            cached.close()
        except Exception:
            pass


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
