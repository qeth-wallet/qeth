"""Shutdown must finish network workers before Qt destroys their wrappers."""

import subprocess
import sys
import textwrap
from threading import Event, Timer

import pytest
from PySide6.QtCore import QThread


class DelayedRead(QThread):
    def __init__(self):
        super().__init__()
        self.entered = Event()
        self.release = Event()
        self.completed = Event()

    def run(self):
        self.entered.set()
        self.release.wait(3)
        self.completed.set()


def test_quit_waits_for_a_worker_that_finishes_in_time(mainwindow):
    worker = DelayedRead()
    mainwindow.start_worker(worker)
    assert worker.entered.wait(1)
    mainwindow._SHUTDOWN_JOIN_S = 1.0
    release = Timer(0.1, worker.release.set)
    release.start()
    try:
        mainwindow._join_workers()
        assert worker.completed.is_set()
        assert worker.isFinished()
    finally:
        worker.release.set()
        worker.wait(3000)
        release.join()


@pytest.mark.parametrize("stall", ["worker", "startup-lock", "logging-lock", "sidecar-stop"])
def test_quit_exits_when_a_worker_misses_the_deadline(qtbot, tmp_path, stall):
    from qeth.single_instance import SingleInstanceGuard

    script = textwrap.dedent('''
        import logging
        import sys
        from threading import Event
        from types import SimpleNamespace
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QApplication, QWidget
        from qeth import helios
        from qeth.single_instance import SingleInstanceGuard
        from qeth.ui import MainWindow

        app = QApplication([])
        guard = SingleInstanceGuard(sys.argv[1])
        assert guard.is_primary()
        assert guard._server is not None  # A listen failure also returns True.
        stall = sys.argv[2]
        helios._FORCED_STOP_TIMEOUT_S = 0.5
        if stall == "sidecar-stop":
            helios._sidecars[10] = SimpleNamespace(stop=lambda: Event().wait(20))
        helios._sidecars[1] = SimpleNamespace(
            stop=lambda: print("helios stopped", flush=True),
        )

        class Window(QWidget):
            _join_workers = MainWindow._join_workers
            _SHUTDOWN_JOIN_S = 0.05

        started = Event()

        class BlockedHandler(logging.Handler):
            def emit(self, record):
                started.set()
                Event().wait(20)

        class StuckWorker(QThread):
            def requestInterruption(self):
                super().requestInterruption()
                print("interruption requested", flush=True)

            def run(self):
                if stall == "logging-lock":
                    logger = logging.getLogger()
                    logger.addHandler(BlockedHandler())
                    logger.warning("worker entered log handler")
                    return
                if stall == "startup-lock":
                    # _ensure_sidecar holds this lock while starting Helios.
                    with helios._lock:
                        started.set()
                        Event().wait(20)
                    return
                started.set()
                Event().wait(20)

        worker = StuckWorker()
        worker.start()
        assert started.wait(3)
        window = Window()
        window._active_workers = {worker}
        window.rpc = SimpleNamespace(stop=lambda: print("rpc stopped", flush=True))
        window.plugins = {
            "transactions": SimpleNamespace(
                shutdown=lambda: print("cache drained", flush=True),
            )
        }
        window._join_workers()
        raise AssertionError("Qt teardown would encounter a live worker")
    ''')
    key = str(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-c", script, key, stall],
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.splitlines() == [
        "interruption requested",
        "cache drained",
        "rpc stopped",
        "helios stopped",
    ]
    guard = SingleInstanceGuard(key)
    assert guard.is_primary()
    assert guard._server is not None


def test_no_callback_can_start_another_worker_after_quit(mainwindow):
    mainwindow._join_workers()
    worker = DelayedRead()
    try:
        mainwindow.start_worker(worker)
        assert not worker.entered.wait(0.1)
        assert not worker.isRunning()
    finally:
        worker.release.set()
        worker.wait(3000)


def test_cancelled_ens_text_key_read_finishes_without_emitting(qtbot, monkeypatch):
    from qeth.chains import DEFAULT_CHAINS
    from qeth.plugins import ens

    entered, release = Event(), Event()
    cancellation = []
    results = []

    def discover(chain, address, *, cancelled):
        entered.set()
        release.wait(3)
        cancellation.append(cancelled())
        return {"lt"}

    monkeypatch.setattr(ens, "discover_custom_text_keys", discover)
    worker = ens.EnsTextKeysWorker(DEFAULT_CHAINS[0], "0x" + "ab" * 20)
    worker.ready.connect(results.append)
    worker.start()
    try:
        assert entered.wait(1)
        worker.requestInterruption()
        assert worker.isRunning()
        release.set()
        assert worker.wait(3000)
        qtbot.wait(10)
        assert cancellation == [True]
        assert results == []
    finally:
        release.set()
        worker.wait(3000)
