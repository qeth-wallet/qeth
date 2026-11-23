"""``DialogInteraction`` — the Qt implementation of ``SignerInteraction``.

Owns the modal "working…" spinner and the secret prompt for one signing flow,
and marshals any call made from a signing WORKER thread onto the Qt main loop —
blocking that worker on a ``Future`` until the user answers. Same worker↔main
pattern as ``SignerBridge`` (RPC-thread → main), generalised so a signer can
drive UI from ``sign()``. In step 2 the calls happen up front on the main
thread (the marshaling is a no-op fast path); step 3's QR exchange is what
actually runs it from a worker.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from threading import Lock
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import QApplication, QProgressDialog, QWidget

from .dialog import prompt_text
from .signing import SignerError


class DialogInteraction(QObject):
    # (fn, future): a worker asks the main thread to run fn and deliver its
    # result. Queued onto the main loop (this QObject lives there).
    _call = Signal(object, object)

    def __init__(self, parent: QWidget, title: str) -> None:
        super().__init__(parent)
        self._parent = parent
        self._title = title
        self._progress: QProgressDialog | None = None
        self._call.connect(self._run)
        self._lock = Lock()
        self._pending: set[Future] = set()
        self._cancelled = False

    # --- worker→main marshaling -------------------------------------------

    def _run(self, fn: Callable[[], Any], fut: Future) -> None:
        """Runs on the main thread (queued receiver). Deliver fn()'s result —
        or its exception — to the awaiting worker."""
        with self._lock:
            if fut.done():
                return
        try:
            result = fn()
        except Exception as e:
            with self._lock:
                if not fut.done():
                    fut.set_exception(e)
        else:
            with self._lock:
                if not fut.done():
                    fut.set_result(result)

    def _on_main(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` on the Qt main thread and block for its result. Direct
        when already on the main thread (a queued round-trip would deadlock —
        the loop that must service it is the caller); queued from a worker."""
        fut: Future = Future()
        with self._lock:
            if self._cancelled:
                raise SignerError("Application is shutting down")
            self._pending.add(fut)
        try:
            app = QApplication.instance()
            if app is None or QThread.currentThread() == app.thread():
                self._run(fn, fut)
            else:
                self._call.emit(fn, fut)
            return fut.result()
        finally:
            with self._lock:
                self._pending.discard(fut)

    def cancel(self) -> None:
        """Main-thread shutdown: release waiting workers and reject future calls."""
        with self._lock:
            self._cancelled = True
            for fut in self._pending:
                if not fut.done():
                    fut.set_exception(SignerError("Application is shutting down"))
        self._close_progress()

    # --- SignerInteraction -------------------------------------------------

    def progress(self, text: str) -> None:
        self._on_main(lambda: self._show_progress(text))

    def request_secret(self, prompt: str, *, title: str = "") -> str | None:
        return self._on_main(lambda: self._prompt(prompt, title))

    def exchange_qr(self, next_frame: Callable[[], str]) -> str | None:
        return self._on_main(lambda: self._run_exchange(next_frame))

    def close(self) -> None:
        """Dismiss the spinner (on signing success/failure)."""
        try:
            self._on_main(self._close_progress)
        except SignerError:
            # cancel() already closed the spinner; worker cleanup must not wait.
            pass

    # --- main-thread widget ops -------------------------------------------

    def _show_progress(self, text: str) -> None:
        if self._progress is None:
            p = QProgressDialog(
                labelText=text, minimum=0, maximum=0, parent=self._parent)
            p.setCancelButton(None)               # not user-cancellable
            p.setWindowTitle(self._title)
            p.setWindowModality(Qt.WindowModality.WindowModal)
            p.setMinimumDuration(0)
            self._progress = p
        else:
            self._progress.setLabelText(text)
        self._progress.show()

    def _prompt(self, prompt: str, title: str) -> str | None:
        text, ok = prompt_text(
            self._parent, title=title or self._title, label=prompt,
            password=True)
        return text if ok else None

    def _close_progress(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None

    def _run_exchange(self, next_frame: Callable[[], str]) -> str | None:
        """Main-thread: open the modal QR exchange window and return the scanned
        ``ur:…`` (or ``None`` on cancel). ``exec()`` spins a nested loop while
        the signing worker blocks on the marshaling Future."""
        from .qr_exchange_dialog import QRExchangeDialog
        dialog = QRExchangeDialog(next_frame, parent=self._parent)
        dialog.exec()
        return dialog.scanned_ur()
