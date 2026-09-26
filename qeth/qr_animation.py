"""Bounded off-thread QR preparation and monotonic display scheduling."""

from __future__ import annotations

import math
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from cbor2 import loads
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QImage

from .qr.fountain import choose_fragments
from .qr.multipart import MAX_FRAGMENTS, _split_part
from .qr_widget import QRWidget, qr_to_image, ur_animation_version

TARGET_FPS = 15.0
PREPARED_FRAMES = 4


class FrameDeadline:
    """Keep fractional periods; rebase after stalls instead of catching up."""

    def __init__(self, fps: float) -> None:
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError("fps must be finite and positive")
        self.period = 1 / fps
        self.next = 0.0

    def displayed(self, now: float) -> None:
        # Allow only timer quantization (~1 ms), never short catch-up flashes.
        if not self.next or now - self.next > 0.002:
            self.next = now + self.period
        else:
            self.next += self.period

    def delay_ms(self, now: float) -> int:
        return max(0, math.ceil((self.next - now) * 1000))


@dataclass(frozen=True)
class PreparedFrame:
    content: str
    image: QImage
    mask_shift: int
    static: bool


class FramePreparer:
    """One producer, at most four prepared/in-flight frames, no GUI objects.

    Cancellation never waits for an encoder or callable on the GUI thread.
    The daemon exits after the current preparation; it cannot touch a deleted
    widget. A semaphore reserves space *before* consuming the next source part.
    """

    def __init__(
        self, next_frame: Callable[[], str], *, fixed_version: bool = True
    ) -> None:
        self.frames: queue.Queue[PreparedFrame | Exception] = queue.Queue(
            PREPARED_FRAMES
        )
        self.cancelled = threading.Event()
        self._slots = threading.Semaphore(PREPARED_FRAMES)
        self._next_frame = next_frame
        self._fixed_version = fixed_version
        self.thread = threading.Thread(target=self._run, name="qr-prepare", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.cancelled.set()

    def take(self) -> PreparedFrame | Exception | None:
        try:
            frame = self.frames.get_nowait()
        except queue.Empty:
            return None
        self._slots.release()
        return frame

    def _run(self) -> None:
        visits: dict[int, int] = {}
        version = None
        first = True
        while not self.cancelled.is_set():
            if not self._slots.acquire(timeout=0.05):
                continue
            if self.cancelled.is_set():
                return
            try:
                content = self._next_frame()
                if first:
                    if self._fixed_version:
                        version = ur_animation_version(content)
                    first = False
                static = content.count("/") == 1
                shift = 0
                if not static:
                    _, sequence, total, payload = _split_part(content)
                    if (
                        sequence is not None
                        and total is not None
                        and 1 <= total <= MAX_FRAGMENTS
                    ):
                        fields = loads(payload)
                        indexes = choose_fragments(sequence, total, fields[3])
                        if len(indexes) == 1:
                            index = next(iter(indexes))
                            shift = visits.get(index, 0)
                            visits[index] = (shift + 1) % 8
                image = qr_to_image(
                    content.upper(), error="l", version=version, mask_shift=shift
                )
                frame = PreparedFrame(content, image, shift, static)
            except Exception as exc:
                if not self.cancelled.is_set():
                    self.frames.put_nowait(exc)
                return
            if self.cancelled.is_set():
                return
            self.frames.put_nowait(frame)
            if static:
                return


class QRAnimation(QObject):
    """Shared by the signer and diagnostic harness; only presentation is in Qt."""

    displayed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        widget: QRWidget,
        next_frame: Callable[[], str],
        *,
        fps: float = TARGET_FPS,
        fixed_version: bool = True,
    ):
        super().__init__(widget)
        self._widget = widget
        self._deadline = FrameDeadline(fps)
        self._preparer = FramePreparer(next_frame, fixed_version=fixed_version)
        self._stopped = False
        self.shown: PreparedFrame | None = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self._tick)
        # This callback owns no QObject; destruction also cancels queued work.
        self.destroyed.connect(self._preparer.stop)
        self.timer.start(0)

    def stop(self) -> None:
        self._stopped = True
        self.timer.stop()
        self._preparer.stop()

    def _tick(self) -> None:
        if self._stopped:
            return
        now = time.monotonic()
        delay = self._deadline.delay_ms(now)
        if delay > 0:
            self.timer.start(delay)
            return
        frame = self._preparer.take()
        if frame is None:
            # Retain the current QR while the worker catches up.
            self.timer.start(5)
            return
        if isinstance(frame, Exception):
            self.stop()
            self.failed.emit(str(frame))
            return
        self._widget.set_image(frame.image)
        self.shown = frame
        now = time.monotonic()
        self._deadline.displayed(now)
        if not frame.static:
            self.timer.start(self._deadline.delay_ms(now))
        self.displayed.emit(frame)
