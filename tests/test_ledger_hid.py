from __future__ import annotations

import threading
import time

import pytest

from qeth.ledger_hid import LedgerHidService


def _service() -> LedgerHidService:
    return LedgerHidService(name="qeth-ledger-hid-test")


def test_jobs_run_on_one_stable_thread_and_are_serialized() -> None:
    service = _service()
    active = 0
    max_active = 0
    thread_ids: list[int] = []
    order: list[int] = []
    lock = threading.Lock()

    def job(i: int) -> int:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            thread_ids.append(threading.get_ident())
            order.append(i)
        time.sleep(0.01)
        with lock:
            active -= 1
        return i

    try:
        futures = [service.submit(lambda i=i: job(i)) for i in range(5)]
        assert [future.result(timeout=1) for future in futures] == list(range(5))
    finally:
        service.shutdown_for_tests()

    assert len(set(thread_ids)) == 1
    assert max_active == 1
    assert order == list(range(5))


def test_timeout_returns_signer_error_while_worker_finishes() -> None:
    service = _service()
    from qeth.signing import SignerError

    started = threading.Event()
    finished = threading.Event()
    followup_thread_ids: list[int] = []

    def slow() -> None:
        started.set()
        time.sleep(0.05)
        finished.set()

    try:
        with pytest.raises(SignerError, match="timed out"):
            service.call(slow, timeout=0.001)
        assert started.wait(1)
        assert finished.wait(1)
        followup = service.call(
            lambda: followup_thread_ids.append(threading.get_ident()) or "done",
            timeout=1,
        )
        assert followup == "done"
    finally:
        service.shutdown_for_tests()

    assert len(set(followup_thread_ids)) == 1
