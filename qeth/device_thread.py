"""A single-thread job service for a USB hardware wallet.

Hardware-wallet stacks are not safe to drive from whichever transient Qt worker
happens to be signing or discovering: hidapi's macOS backend ties a HID handle to
the thread that opened it, and trezorlib's client/session objects carry transport
state that must not be shared across threads. So each device family gets ONE
process-lifetime thread: callers submit a closure and block on a ``Future``, and
the service runs jobs strictly serialized on its own thread, calling an optional
``after_job`` hook after each (Ledger clears ledgereth's dongle cache there).
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
from typing import Any, TypeVar, cast

T = TypeVar("T")

# Generous: a job can include a human confirming a tx on the device.
DEFAULT_DEVICE_TIMEOUT_S = 180.0


@dataclass
class _QueuedJob:
    future: Future[Any]
    fn: Callable[[], Any]


class DeviceJobService:
    """Runs one device family's jobs on a single process-lifetime thread."""

    def __init__(
        self,
        *,
        name: str,
        device_label: str,
        after_job: Callable[[], None] | None = None,
    ) -> None:
        self._name = name
        self._device_label = device_label
        self._after_job = after_job
        self._queue: queue.Queue[_QueuedJob | None] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopped = False

    def submit(self, fn: Callable[[], T]) -> Future[T]:
        """Queue ``fn`` for execution on the device thread."""
        future: Future[T] = Future()
        with self._lock:
            if self._stopped:
                raise RuntimeError(f"{self._device_label} device service has been stopped")
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
        timeout: float = DEFAULT_DEVICE_TIMEOUT_S,
    ) -> T:
        """Run ``fn`` on the device thread and wait for its result.

        The timeout is caller-side only: on timeout we raise but the device
        thread keeps running the job to completion (and runs ``after_job``
        before taking the next), so the device isn't left mid-exchange."""
        future = self.submit(fn)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            from .signing import SignerError

            raise SignerError(
                f"{self._device_label} operation timed out. Check the device "
                "prompt and try again.",
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
                self._cleanup()
                if not job.future.cancelled():
                    job.future.set_exception(exc)
            else:
                self._cleanup()
                if not job.future.cancelled():
                    job.future.set_result(result)

    def _cleanup(self) -> None:
        if self._after_job is not None:
            self._after_job()
