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

import queue
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
from typing import Any, TypeVar, cast

T = TypeVar("T")

# Generous: a job can include a human confirming a tx on the device.
DEFAULT_LEDGER_HID_TIMEOUT_S = 180.0


@dataclass
class _QueuedJob:
    future: Future[Any]
    fn: Callable[[], Any]


class LedgerHidService:
    """Runs Ledger/HID jobs on one process-lifetime worker thread."""

    def __init__(self, *, name: str = "qeth-ledger-hid") -> None:
        self._name = name
        self._queue: queue.Queue[_QueuedJob | None] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopped = False

    def submit(self, fn: Callable[[], T]) -> Future[T]:
        """Queue ``fn`` for execution on the Ledger HID thread."""
        future: Future[T] = Future()
        with self._lock:
            if self._stopped:
                raise RuntimeError("Ledger HID service has been stopped")
            self._ensure_started_locked()
            # Enqueue INSIDE the lock: shutdown_for_tests also holds it while
            # setting _stopped and putting the sentinel, so a job can't be
            # queued after the sentinel (which the worker never drains → the
            # caller would block for the full timeout).
            self._queue.put(_QueuedJob(future=cast("Future[Any]", future), fn=fn))
        return future

    def call(
        self,
        fn: Callable[[], T],
        *,
        timeout: float = DEFAULT_LEDGER_HID_TIMEOUT_S,
    ) -> T:
        """Run ``fn`` on the HID thread and wait for its result.

        The timeout is caller-side only: on timeout we raise but the HID
        thread keeps running the job to completion (and clears the cache
        before taking the next), so the device isn't left mid-exchange."""
        future = self.submit(fn)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            from .signing import SignerError

            raise SignerError(
                "Ledger operation timed out. Check the device prompt and "
                "try again.",
            ) from exc

    def shutdown_for_tests(self, timeout: float = 5.0) -> None:
        """Stop the worker thread. Runtime code should not call this — the
        service lives for the whole process."""
        with self._lock:
            self._stopped = True
            thread = self._thread
            if thread is not None:
                self._queue.put(None)
        if thread is not None:
            thread.join(timeout=timeout)

    def _ensure_started_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name=self._name,
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            if job.future.cancelled():
                continue
            try:
                result = job.fn()
            except BaseException as exc:
                _clear_ledgereth_cache()
                if not job.future.cancelled():
                    job.future.set_exception(exc)
            else:
                _clear_ledgereth_cache()
                if not job.future.cancelled():
                    job.future.set_result(result)


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
