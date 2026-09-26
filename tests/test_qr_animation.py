"""Timing and lifecycle contracts for production QR preparation/display."""

import threading
import time

import pytest

from qeth.qr.multipart import frame_source
from qeth.qr_animation import FrameDeadline, FramePreparer, QRAnimation, TARGET_FPS
from qeth.qr_scan import _qimage_to_gray, decode_qr
from qeth.qr_widget import QRWidget


@pytest.mark.parametrize("fps", [5, 8, 10, 12, 15])
def test_fractional_deadlines_do_not_accumulate_rounding(fps):
    clock = FrameDeadline(fps)
    clock.displayed(100)
    for i in range(1, 10001):
        # Model Qt rounding each requested timeout up to the next millisecond.
        now = 100 + i / fps
        clock.displayed(now + 0.0009)
    assert clock.next == pytest.approx(100 + 10001 / fps, abs=1e-7)
    assert clock.delay_ms(clock.next - 0.0001) == 1


@pytest.mark.parametrize("fps,delay", [(10, 100), (12, 84), (15, 67)])
def test_stall_rebases_instead_of_catching_up(fps, delay):
    clock = FrameDeadline(fps)
    clock.displayed(100)
    clock.displayed(100.5)
    assert clock.next == pytest.approx(100.5 + 1 / fps)
    assert clock.delay_ms(100.5) == delay


@pytest.mark.parametrize("fps", [0, -1, float("inf"), float("nan")])
def test_invalid_fps_rejected(fps):
    with pytest.raises(ValueError):
        FrameDeadline(fps)


def test_worker_is_bounded_and_cancelled_with_full_queue(qtbot):
    calls = []
    source = frame_source("eth-sign-request", bytes(2000))

    def pull():
        calls.append(threading.get_ident())
        return source()

    worker = FramePreparer(pull)
    try:
        qtbot.waitUntil(lambda: worker.frames.qsize() == 4)
        qtbot.wait(80)
        assert len(calls) == 4
        assert set(calls) == {worker.thread.ident}
        assert threading.get_ident() not in calls
    finally:
        worker.stop()
    qtbot.waitUntil(lambda: not worker.thread.is_alive())


def test_starvation_retains_image_and_cancellation_drops_pending_frame(qtbot):
    release = threading.Event()
    entered = threading.Event()
    source = frame_source("eth-sign-request", bytes(2000))
    calls = 0

    def pull():
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            release.wait(5)
        return source()

    widget = QRWidget()
    qtbot.addWidget(widget)
    widget.show()
    animation = QRAnimation(widget, pull)
    times = []
    animation.displayed.connect(lambda _: times.append(time.monotonic()))
    try:
        qtbot.waitUntil(lambda: animation.shown is not None and entered.is_set())
        first = animation.shown
        qtbot.wait(220)
        assert animation.shown is first
        assert (
            decode_qr(_qimage_to_gray(widget.grab().toImage())) == first.content.upper()
        )
        release.set()
        qtbot.waitUntil(lambda: animation.shown is not first)
        qtbot.waitUntil(lambda: len(times) >= 3)
        assert times[2] - times[1] >= 1 / TARGET_FPS - 0.008
        animation.stop()
        last = animation.shown
        qtbot.wait(150)
        assert animation.shown is last
        qtbot.waitUntil(lambda: not animation._preparer.thread.is_alive())
    finally:
        release.set()
        animation.stop()


def test_cancel_during_encoding_is_nonblocking_and_never_displays(qtbot, monkeypatch):
    import qeth.qr_animation as module

    entered, release = threading.Event(), threading.Event()
    encode = module.qr_to_image

    def slow_encode(*args, **kwargs):
        entered.set()
        release.wait(5)
        return encode(*args, **kwargs)

    monkeypatch.setattr(module, "qr_to_image", slow_encode)
    widget = QRWidget()
    qtbot.addWidget(widget)
    animation = QRAnimation(widget, frame_source("x", bytes(2000)))
    try:
        qtbot.waitUntil(entered.is_set)
        animation.stop()
        assert animation.shown is None
        release.set()
        qtbot.waitUntil(lambda: not animation._preparer.thread.is_alive())
        assert animation._preparer.frames.empty()
        assert animation.shown is None
    finally:
        release.set()
        animation.stop()


def test_worker_failure_is_reported(qtbot):
    widget = QRWidget()
    qtbot.addWidget(widget)

    def broken():
        raise ValueError("test source failed")

    animation = QRAnimation(widget, broken)
    with qtbot.waitSignal(animation.failed) as signal:
        pass
    assert signal.args == ["test source failed"]
    assert not animation.timer.isActive()


def test_destroying_widget_cancels_its_worker(qtbot):
    widget = QRWidget()
    animation = QRAnimation(widget, frame_source("x", bytes(2000)))
    worker = animation._preparer
    qtbot.waitUntil(lambda: worker.frames.qsize() == 4)
    widget.deleteLater()
    qtbot.waitUntil(lambda: not worker.thread.is_alive())
    assert worker.cancelled.is_set()


def test_alias_retry_continues_the_original_chunks_mask_cycle(qtbot):
    from qeth.qr.multipart import _part, _plan, _PlainRetries

    message = bytes(range(200))
    count, fragments, checksum = _plan(message, 120)
    retries = _PlainRetries(count, checksum)
    sequence = retries.sequence(0)
    assert sequence > count
    original = _part("x", 1, count, len(message), checksum, fragments)
    alias = _part("x", sequence, count, len(message), checksum, fragments)
    frames = iter([original] + [alias] * 8)
    worker = FramePreparer(lambda: next(frames))
    try:
        shifts = []
        for i in range(9):
            qtbot.waitUntil(lambda: not worker.frames.empty())
            frame = worker.take()
            shifts.append(frame.mask_shift)
            assert (
                decode_qr(_qimage_to_gray(frame.image))
                == (original if i == 0 else alias).upper()
            )
        assert shifts == [0, 1, 2, 3, 4, 5, 6, 7, 0]
    finally:
        worker.stop()


@pytest.mark.parametrize("fixed_version", [True, False])
def test_worker_framing_policies_preserve_content_through_high_sequences(
    qtbot, fixed_version
):
    import random
    import segno
    from qeth.qr.multipart import _part, _plan, _fragment_len_for

    message = random.Random(892).randbytes(44_345)
    count, fragments, checksum = _plan(message, _fragment_len_for(len(message)))
    # Mixed metadata lengths exercise a real QR version boundary.
    # High numbers cover the alias range and uint32 boundary.
    contents = [
        _part("eth-sign-request", sequence, count, len(message), checksum, fragments)
        for sequence in (1, 10, 1 << 31, (1 << 32) - 1, 1)
    ]
    # Independently find the common size needed across the sequence range.
    natural_sides = [
        segno.make_qr(c.upper(), error="l").symbol_size()[0] for c in contents
    ]
    assert natural_sides == [85, 89, 89, 89, 85]
    expected_sides = [89] * len(contents) if fixed_version else natural_sides
    source = iter(contents)
    worker = FramePreparer(lambda: next(source), fixed_version=fixed_version)
    try:
        for content, side in zip(contents, expected_sides, strict=True):
            qtbot.waitUntil(lambda: not worker.frames.empty())
            frame = worker.take()
            assert frame.content == content
            assert (frame.image.width(), frame.image.height()) == (
                side,
                side,
            )
            assert decode_qr(_qimage_to_gray(frame.image)) == content.upper()
    finally:
        worker.stop()
