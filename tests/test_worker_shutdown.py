"""Shutdown must finish network workers before Qt destroys their wrappers."""

from threading import Event, Timer

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


def test_quit_waits_for_a_read_beyond_the_old_shutdown_deadline(mainwindow):
    worker = DelayedRead()
    mainwindow.start_worker(worker)
    assert worker.entered.wait(1)
    # Shorten the old five-second deadline so this reproduces the real crash
    # boundary without spending five seconds or aborting pytest itself.
    mainwindow._SHUTDOWN_JOIN_S = 0.001
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
