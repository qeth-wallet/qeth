"""QR scanning for the air-gapped exchange: a zxing-cpp decoder plus a camera
frame source (system QtMultimedia, imported lazily so the rest of the app — and
the tests — run without it).

The dialog talks to a ``scanner``: a ``QObject`` exposing ``decoded(str)`` and
``frame(QImage)`` signals and ``start()`` / ``stop()``. ``CameraScanner`` is the
real one; tests inject a fake, so QtMultimedia is never needed to exercise the
dialog logic.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal


def decode_qr(image: Any) -> str | None:
    """First QR payload found in ``image`` (a PIL image or ndarray), or ``None``.
    zxing-cpp is loaded lazily — it's only needed when actually scanning."""
    import zxingcpp
    for result in zxingcpp.read_barcodes(image):
        if result.text:
            return str(result.text)
    return None


def _qimage_to_pil(image: Any) -> Any:
    """A ``QImage`` → a PIL RGB image zxing-cpp can read. Honours the row
    stride (``bytesPerLine`` is padded), so no numpy needed."""
    from PIL import Image
    from PySide6.QtGui import QImage
    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height, stride = rgb.width(), rgb.height(), rgb.bytesPerLine()
    buf = bytes(rgb.constBits())[: height * stride]
    return Image.frombuffer("RGB", (width, height), buf, "raw", "RGB", stride, 1)


class CameraScanner(QObject):
    """Live camera → decoded-QR signals, over QtMultimedia. Constructed only
    when the exchange dialog opens for real (QtMultimedia is imported here, not
    at module load). Each frame is handed to :func:`decode_qr`; a non-empty
    decode fires ``decoded``."""

    decoded = Signal(str)
    frame = Signal(object)   # QImage, for the live preview

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtMultimedia import (
            QCamera, QMediaCaptureSession, QVideoSink,
        )
        self._camera = QCamera()
        self._sink = QVideoSink()
        self._session = QMediaCaptureSession()
        self._session.setCamera(self._camera)
        self._session.setVideoSink(self._sink)
        self._sink.videoFrameChanged.connect(self._on_frame)

    def start(self) -> None:
        self._camera.start()

    def stop(self) -> None:
        self._camera.stop()

    def _on_frame(self, video_frame: Any) -> None:
        image = video_frame.toImage()
        if image.isNull():
            return
        self.frame.emit(image)
        try:
            text = decode_qr(_qimage_to_pil(image))
        except Exception:
            return   # a bad frame must not kill the scan loop
        if text:
            self.decoded.emit(text)
