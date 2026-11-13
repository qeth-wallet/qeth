#!/usr/bin/env python3
"""Exercise ledgereth/hidapi from different thread shapes on macOS.

This is a diagnostic tool, not part of qeth's runtime. Use matrix mode when
comparing crash behavior: each mode runs in a child process so a native HID
crash in one mode does not kill the whole diagnostic run.
"""

from __future__ import annotations

import argparse
import os
import platform
import queue
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

DEFAULT_PATH = "44'/60'/0'/0/0"
CHILD_TIMEOUT_S = 120.0


class _Closeable(Protocol):
    def close(self) -> None:
        pass


@dataclass
class ProbeResult:
    ok: bool
    iteration: int
    thread_id: int
    thread_name: str
    elapsed_s: float
    message: str


def _clear_ledgereth_cache(dongle: object | None = None) -> None:
    try:
        from ledgereth import comms

        comms.DONGLE_CACHE = None
        comms.DONGLE_CONFIG_CACHE = None
    except Exception:
        pass
    if dongle is not None:
        try:
            cast(_Closeable, dongle).close()
        except Exception:
            pass


def _probe_once(iteration: int, path: str) -> ProbeResult:
    start = time.monotonic()
    dongle: object | None = None
    try:
        from ledgereth.accounts import get_account_by_path
        from ledgereth.comms import init_dongle

        dongle = init_dongle()
        account = get_account_by_path(path, dongle=dongle)
        return ProbeResult(
            ok=True,
            iteration=iteration,
            thread_id=threading.get_ident(),
            thread_name=threading.current_thread().name,
            elapsed_s=time.monotonic() - start,
            message=f"derived {account.address} at {path}",
        )
    except Exception as exc:
        return ProbeResult(
            ok=False,
            iteration=iteration,
            thread_id=threading.get_ident(),
            thread_name=threading.current_thread().name,
            elapsed_s=time.monotonic() - start,
            message="".join(traceback.format_exception_only(type(exc), exc)).strip(),
        )
    finally:
        _clear_ledgereth_cache(dongle)


def _print_result(result: ProbeResult) -> None:
    status = "ok" if result.ok else "error"
    print(
        f"{status} iter={result.iteration} thread={result.thread_name}"
        f" id={result.thread_id} elapsed={result.elapsed_s:.3f}s"
        f" {result.message}",
        flush=True,
    )


def _run_main_thread(iterations: int, path: str) -> int:
    ok = True
    for i in range(iterations):
        result = _probe_once(i, path)
        _print_result(result)
        ok = ok and result.ok
    return 0 if ok else 2


def _run_transient_python_thread(iterations: int, path: str) -> int:
    ok = True
    for i in range(iterations):
        results: queue.Queue[ProbeResult] = queue.Queue(maxsize=1)
        thread = threading.Thread(
            target=lambda: results.put(_probe_once(i, path)),
            name=f"qeth-hid-repro-python-{i}",
        )
        thread.start()
        thread.join()
        result = results.get()
        _print_result(result)
        ok = ok and result.ok
    return 0 if ok else 2


def _run_transient_qthread(iterations: int, path: str) -> int:
    from PySide6.QtCore import QCoreApplication, QThread

    app = QCoreApplication.instance() or QCoreApplication([])

    class ProbeThread(QThread):
        def __init__(self, iteration: int) -> None:
            super().__init__()
            self.iteration = iteration
            self.result: ProbeResult | None = None

        def run(self) -> None:
            self.setObjectName(f"qeth-hid-repro-qthread-{self.iteration}")
            self.result = _probe_once(self.iteration, path)

    ok = True
    for i in range(iterations):
        thread = ProbeThread(i)
        thread.start()
        thread.wait()
        result = thread.result
        if result is None:
            result = ProbeResult(
                ok=False,
                iteration=i,
                thread_id=-1,
                thread_name="qeth-hid-repro-qthread",
                elapsed_s=0.0,
                message="QThread exited without a result",
            )
        _print_result(result)
        ok = ok and result.ok
    del app
    return 0 if ok else 2


def _run_persistent_thread(iterations: int, path: str) -> int:
    jobs: queue.Queue[int | None] = queue.Queue()
    results: queue.Queue[ProbeResult] = queue.Queue()

    def worker() -> None:
        while True:
            iteration = jobs.get()
            if iteration is None:
                return
            results.put(_probe_once(iteration, path))

    thread = threading.Thread(target=worker, name="qeth-hid-repro-persistent")
    thread.start()
    for i in range(iterations):
        jobs.put(i)
    jobs.put(None)

    ok = True
    seen_thread_id: int | None = None
    for _ in range(iterations):
        result = results.get()
        _print_result(result)
        if seen_thread_id is None:
            seen_thread_id = result.thread_id
        elif result.thread_id != seen_thread_id:
            result.ok = False
            result.message = "persistent mode changed thread id"
            _print_result(result)
        ok = ok and result.ok
    thread.join()
    return 0 if ok else 2


MODES: dict[str, Callable[[int, str], int]] = {
    "main": _run_main_thread,
    "python-thread": _run_transient_python_thread,
    "qt-thread": _run_transient_qthread,
    "persistent-thread": _run_persistent_thread,
}


def _run_matrix(iterations: int, path: str, timeout: float) -> int:
    script = Path(__file__).resolve()
    env = os.environ.copy()
    exit_code = 0
    for mode in MODES:
        cmd = [
            sys.executable,
            str(script),
            "--mode",
            mode,
            "--iterations",
            str(iterations),
            "--path",
            path,
        ]
        print(f"\n=== {mode} ===", flush=True)
        start = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                env=env,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            print(f"timeout after {timeout:.1f}s", flush=True)
            if exc.stdout:
                print(exc.stdout, end="")
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
            exit_code = 124
            continue
        elapsed = time.monotonic() - start
        print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        print(
            f"mode={mode} returncode={completed.returncode}"
            f" elapsed={elapsed:.3f}s",
            flush=True,
        )
        if completed.returncode != 0 and exit_code == 0:
            exit_code = completed.returncode
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=[*MODES, "matrix"],
        default="matrix",
        help="threading shape to exercise",
    )
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--child-timeout", type=float, default=CHILD_TIMEOUT_S)
    args = parser.parse_args(argv)

    if args.iterations < 1:
        parser.error("--iterations must be >= 1")
    if platform.system() != "Darwin":
        print("warning: this HIDAPI threading repro is intended for macOS")

    if args.mode == "matrix":
        return _run_matrix(args.iterations, args.path, args.child_timeout)
    return MODES[args.mode](args.iterations, args.path)


if __name__ == "__main__":
    raise SystemExit(main())
