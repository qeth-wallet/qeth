"""Decode the actual displayed pixels, including resize and display scaling."""

import io

import pytest
import segno
from PIL import Image, ImageChops

from qeth.qr.multipart import frame_source
from qeth.qr_scan import _qimage_to_gray, decode_qr
from qeth.qr_widget import QRWidget


@pytest.mark.parametrize("size", [(240, 320), (900, 700), (533, 411)])
@pytest.mark.parametrize("payload_size", [120, 10_000, 30_000])
def test_display_has_uniform_modules_full_border_and_exact_payload(
    qtbot, size, payload_size
):
    content = frame_source("eth-sign-request", bytes(payload_size))().upper()
    widget = QRWidget()
    qtbot.addWidget(widget)
    widget.set_content(content, error="l")
    widget.resize(*size)
    widget.show()
    displayed = _qimage_to_gray(widget.grab().toImage())
    assert decode_qr(displayed) == content
    assert {value for count, value in displayed.getcolors()} == {0, 255}

    # Compare every pixel against an independent, integer-scaled Segno render.
    # This catches uneven modules, a cropped quiet zone, smoothing and off-center
    # drawing. The same test runs under QT_SCALE_FACTOR=1, 1.5 and 2.
    qr = segno.make_qr(content, error="l")
    module_count = qr.symbol_size()[0]
    scale = min(displayed.size) // module_count
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=4)
    symbol = Image.open(buf).convert("L")
    expected = Image.new("L", displayed.size, 255)
    expected.paste(
        symbol,
        (
            (displayed.width - symbol.width) // 2,
            (displayed.height - symbol.height) // 2,
        ),
    )
    assert ImageChops.difference(displayed, expected).getbbox() is None


def test_resize_reuses_encoding_and_new_content_replaces_cache(qtbot, monkeypatch):
    import qeth.qr_widget as qr_widget

    original = qr_widget.qr_to_pixmap
    encoded = []

    def encode(content, **kwargs):
        encoded.append(content)
        return original(content, **kwargs)

    monkeypatch.setattr(qr_widget, "qr_to_pixmap", encode)
    widget = QRWidget()
    qtbot.addWidget(widget)
    first = "ethereum:0x1234567890aBcDeF1234567890aBcDeF12345678"
    second = "ethereum:0xABCDEF0123456789ABCDEF0123456789ABCDEF01"
    widget.set_content(first)
    widget.show()
    for size in ((320, 320), (700, 500), (260, 300)):
        widget.resize(*size)
        assert decode_qr(_qimage_to_gray(widget.grab().toImage())) == first
    widget.set_content(second)
    assert decode_qr(_qimage_to_gray(widget.grab().toImage())) == second
    assert encoded == [first, second]


def test_mask_variants_preserve_payload_and_grid(qtbot):
    import random

    content = frame_source(
        "eth-sign-request", random.Random(71).randbytes(44_345)
    )().upper()
    widget = QRWidget()
    qtbot.addWidget(widget)
    widget.resize(720, 720)
    widget.show()
    patterns = set()
    for shift in range(8):
        widget.set_content(content, error="l", version=16, mask_shift=shift)
        shown = _qimage_to_gray(widget.grab().toImage())
        assert decode_qr(shown) == content
        assert {value for count, value in shown.getcolors()} == {0, 255}
        patterns.add(shown.tobytes())
    assert len(patterns) == 8


@pytest.mark.parametrize(
    "content", ["ethereum:0x1234567890aBcDeF1234567890aBcDeF12345678", "UR:BYTES/AEAD"]
)
def test_events_and_rendering_without_dpr_change_enum(qtbot, monkeypatch, content):
    from types import SimpleNamespace
    from PySide6.QtCore import QEvent
    import qeth.qr_widget as qr_widget

    delivered = []

    class EventProbe(QRWidget):
        def customEvent(self, event):
            delivered.append(event.type())

    widget = EventProbe()
    qtbot.addWidget(widget)
    # Qt 6.4 does not expose this newer enum. Keep real Qt event dispatch and
    # rendering, but give the module the older enum surface.
    monkeypatch.setattr(qr_widget, "QEvent", SimpleNamespace(Type=SimpleNamespace()))
    widget.event(QEvent(QEvent.Type.User))
    assert delivered == [QEvent.Type.User]  # Still delegates to QWidget.event.
    widget.set_content(content)
    widget.resize(320, 320)
    widget.show()
    assert decode_qr(_qimage_to_gray(widget.grab().toImage())) == content


def test_dpr_change_requests_repaint_when_supported(qtbot, monkeypatch):
    from PySide6.QtCore import QEvent

    event_type = getattr(QEvent.Type, "DevicePixelRatioChange", None)
    if event_type is None:
        pytest.skip("Qt does not provide DevicePixelRatioChange")
    widget = QRWidget()
    qtbot.addWidget(widget)
    updates = []
    monkeypatch.setattr(widget, "update", lambda: updates.append(True))
    widget.event(QEvent(QEvent.Type.User))
    assert updates == []
    widget.event(QEvent(event_type))
    assert updates == [True]
