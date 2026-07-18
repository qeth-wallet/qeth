"""QR scanning for the air-gapped exchange: a zxing-cpp decoder plus a camera
frame source (system QtMultimedia, imported lazily so the rest of the app — and
the tests — run without it).

The dialog talks to a ``scanner``: a ``QObject`` exposing ``decoded(str)`` and
``frame(QImage)`` signals and ``start()`` / ``stop()``. ``CameraScanner`` is the
real one; tests inject a fake, so QtMultimedia is never needed to exercise the
dialog logic.

The camera pipeline is tuned for reading QR off a screen or paper in varied
light (see the module functions):

- **Focus** — continuous autofocus, the single biggest factor (a blurry frame
  never decodes). Guarded: many built-in webcams are fixed-focus.
- **Resolution** — aim for ~720p, enough module-pixels for dense/animated
  (fountain-coded UR) QRs without the decode latency of 1080p+. Falls back
  gracefully — a 640×480-only webcam keeps its best format.
- **Exposure** — the barcode-tuned mode (else a slight negative comp) so a
  bright, mostly-white QR doesn't clip and wash out the finder patterns.
- **Decode** — grayscale (what zxing binarizes anyway) restricted to QR, run
  OFF the GUI thread with frames dropped while a decode is in flight, so the
  preview stays smooth and we always work on the freshest frame.
"""

from __future__ import annotations

import sys
from typing import Any

from PySide6.QtCore import QObject, Signal


def decode_qr(image: Any) -> str | None:
    """First QR payload found in ``image`` (a PIL image or ndarray), or ``None``.
    Restricted to the QR format — we never scan other symbologies, so zxing-cpp
    doesn't waste each frame attempting every barcode type. Loaded lazily; it's
    only needed when actually scanning."""
    import zxingcpp
    for result in zxingcpp.read_barcodes(
            image, formats=zxingcpp.BarcodeFormat.QRCode):
        if result.text:
            return str(result.text)
    return None


def _qimage_to_gray(image: Any) -> Any:
    """A ``QImage`` → a single-channel luminance PIL image for the decoder.
    Grayscale is exactly what zxing binarizes, so this skips the RGB round-trip
    (~3× less data than RGB888). Honours the padded row stride."""
    from PIL import Image
    from PySide6.QtGui import QImage
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    width, height, stride = gray.width(), gray.height(), gray.bytesPerLine()
    buf = bytes(gray.constBits())[: height * stride]
    return Image.frombuffer("L", (width, height), buf, "raw", "L", stride, 1)


def _pick_video_format(device: Any) -> Any | None:
    """Choose a capture format near 720p, or ``None`` to keep the camera's own
    default. ~720p gives enough pixels-per-module for dense / animated QRs
    without the decode latency (and dropped frames) of 1080p+. Fallback is
    graceful: a device that only offers 640×480 lands on its best format, and
    one that reports nothing returns ``None``."""
    formats = [f for f in device.videoFormats()
               if not f.resolution().isEmpty()]
    if not formats:
        return None

    def height(f: Any) -> int:
        return f.resolution().height()

    def fps(f: Any) -> float:
        return f.maxFrameRate()

    # Sweet spot: 720p..1080p. Among those pick the SMALLEST (nearest 720, so
    # fastest to decode), tie-broken by the higher frame rate.
    sweet = [f for f in formats if 700 <= height(f) <= 1080]
    if sweet:
        return min(sweet, key=lambda f: (height(f), -fps(f)))
    # No ~720p option (a 640×480 webcam lands here): best available under 4K —
    # 4K decodes too slowly to keep up — else whatever exists.
    capped = [f for f in formats if height(f) <= 1080] or formats
    return max(capped, key=lambda f: (height(f) * f.resolution().width(), fps(f)))


class _DecodeWorker(QObject):
    """Runs the zxing decode off the GUI thread. One frame at a time — the
    scanner drops any frame that arrives while a decode is still running, so it
    always works on the freshest one and the preview never stutters."""

    found = Signal(str)
    done = Signal()

    def decode(self, image: Any) -> None:      # runs on the worker thread
        text = None
        try:
            text = decode_qr(_qimage_to_gray(image))
        except Exception:
            text = None            # a bad frame must not kill the scan loop
        if text:
            self.found.emit(text)
        self.done.emit()


class CameraScanner(QObject):
    """Live camera → decoded-QR signals, over QtMultimedia. Constructed only
    when the exchange dialog opens for real (QtMultimedia is imported here, not
    at module load). Frames go to a background :class:`_DecodeWorker`; a
    non-empty decode fires ``decoded``."""

    decoded = Signal(str)
    frame = Signal(object)          # QImage, for the live preview
    failed = Signal(str)            # camera/permission failure for the UI
    _want_decode = Signal(object)   # QImage → the worker thread (queued)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtCore import QThread
        from PySide6.QtMultimedia import (
            QCamera, QMediaCaptureSession, QVideoSink,
        )
        self._camera = QCamera()
        self._sink = QVideoSink()
        self._session = QMediaCaptureSession()
        self._session.setCamera(self._camera)
        self._session.setVideoSink(self._sink)
        self._configure_camera()
        self._camera.errorOccurred.connect(self._on_camera_error)
        self._start_requested = False
        self._permission_request_in_flight = False

        # Decode off the GUI thread; ``_busy`` (only ever touched on the GUI
        # thread — set here, cleared by the worker's queued ``done``) drops
        # frames while a decode is in flight.
        self._busy = False
        self._decode_thread = QThread(self)
        self._worker = _DecodeWorker()
        self._worker.moveToThread(self._decode_thread)
        self._want_decode.connect(self._worker.decode)   # queued (cross-thread)
        self._worker.found.connect(self.decoded)          # forward to listeners
        self._worker.done.connect(self._on_decode_done)
        self._decode_thread.finished.connect(self._worker.deleteLater)
        self._decode_thread.start()

        self._sink.videoFrameChanged.connect(self._on_frame)

    def _configure_camera(self) -> None:
        """Best-effort camera tuning for QR — every control is guarded, so a
        webcam that doesn't expose it just keeps its default."""
        from PySide6.QtMultimedia import QCamera
        cam = self._camera
        try:
            device = cam.cameraDevice()
            fmt = _pick_video_format(device) if device is not None else None
            if fmt is not None:
                cam.setCameraFormat(fmt)
        except Exception:
            pass
        # Continuous autofocus — a sharp frame is what actually decodes.
        auto_focus = QCamera.FocusMode.FocusModeAuto
        if cam.isFocusModeSupported(auto_focus):
            cam.setFocusMode(auto_focus)
        # Keep a bright white QR from clipping (glare on a screen washes the
        # finder patterns out). ExposureBarcode is purpose-built; else nudge
        # exposure down a touch.
        barcode = QCamera.ExposureMode.ExposureBarcode
        if cam.isExposureModeSupported(barcode):
            cam.setExposureMode(barcode)
        else:
            try:
                cam.setExposureCompensation(-0.5)
            except Exception:
                pass
        auto_wb = QCamera.WhiteBalanceMode.WhiteBalanceAuto
        if cam.isWhiteBalanceModeSupported(auto_wb):
            cam.setWhiteBalanceMode(auto_wb)

    def start(self) -> None:
        """Start capture once the platform has granted camera access.

        macOS starts at ``Undetermined`` and requires an explicit request;
        calling ``QCamera.start()`` directly only logs "Access to camera not
        granted" and never reports an error to the application. Other desktop
        platforms retain the direct start path, including Linux distributions
        whose system Qt predates ``QCameraPermission``.
        """
        self._start_requested = True
        if sys.platform != "darwin":
            self._camera.start()
            return

        from PySide6.QtCore import QCameraPermission, QCoreApplication, Qt

        app = QCoreApplication.instance()
        if app is None:
            self.failed.emit("Camera access is unavailable")
            return

        permission = QCameraPermission()
        status = app.checkPermission(permission)
        if status == Qt.PermissionStatus.Granted:
            self._camera.start()
        elif status == Qt.PermissionStatus.Denied:
            self._emit_permission_denied()
        elif not self._permission_request_in_flight:
            self._permission_request_in_flight = True
            app.requestPermission(permission, self, self._permission_decided)

    def stop(self) -> None:
        # Detach the frame callback FIRST so no frame arrives mid-teardown, then
        # stop the camera and drain the decode thread. Idempotent (stop may be
        # called more than once).
        self._start_requested = False
        try:
            self._sink.videoFrameChanged.disconnect(self._on_frame)
        except (RuntimeError, TypeError):
            pass
        self._camera.stop()
        if self._decode_thread.isRunning():
            self._decode_thread.quit()
            self._decode_thread.wait(2000)

    def _permission_decided(self, permission: Any) -> None:
        """Continue an asynchronous permission request while the dialog lives."""
        from PySide6.QtCore import Qt

        self._permission_request_in_flight = False
        if not self._start_requested:
            return
        if permission.status() == Qt.PermissionStatus.Granted:
            self._camera.start()
        else:
            self._emit_permission_denied()

    def _emit_permission_denied(self) -> None:
        self.failed.emit(
            "Camera access denied. Allow qeth to use the camera in System Settings."
        )

    def _on_camera_error(self, _error: Any, message: str) -> None:
        self.failed.emit(message or "The camera could not be started")

    def _on_frame(self, video_frame: Any) -> None:
        image = video_frame.toImage()
        if image.isNull():
            return
        self.frame.emit(image)          # live preview (GUI thread)
        if self._busy:
            return                       # a decode is in flight — drop this one
        self._busy = True
        # Detach from the frame's buffer before the image crosses to the worker
        # (the camera reuses that buffer once this slot returns).
        self._want_decode.emit(image.copy())

    def _on_decode_done(self) -> None:
        self._busy = False
