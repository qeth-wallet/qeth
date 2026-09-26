"""The air-gapped QR exchange window: shows the request UR as a QR *and* runs
the camera to read the device's response QR at the same time. Returns the
scanned ``ur:…`` string (or ``None`` on cancel) — the signer owns UR decode.

Step 3c of docs/signers-qr.md. The camera ``scanner`` is injectable (default:
``qr_scan.CameraScanner``), so the dialog's accept/cancel/decode logic is
unit-tested with a fake source; the real QtMultimedia camera is verified on
hardware.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QImage, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .dialog import Dialog, group_spacing, item_spacing
from .qr_widget import QRWidget, qr_to_pixmap

# Initial preferred side; the panes expand with the window.
PANE = 320


def ur_to_pixmap(ur_string: str, *, scale: int = 8) -> QPixmap:
    """Render a UR string as a QR ``QPixmap``. UR is uppercased so the QR uses
    the compact alphanumeric mode (``ur:``/``/``/``-`` and digits are all in
    that charset). ``scale`` is the source px-per-module."""
    return qr_to_pixmap(ur_string.upper(), error="l", scale=scale)


def _fill_square(pixmap: QPixmap, size: QSize) -> QPixmap:
    """Scale a camera frame to *fill* the (square) ``size`` and centre-crop the
    overflow, so the preview shows edge-to-edge video instead of a letterboxed
    4:3 image with bars. The QR decoder works on the full frame (qr_scan.py), so
    cropping the preview doesn't shrink the scanned area — it's purely visual."""
    scaled = pixmap.scaled(
        size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation)
    x = (scaled.width() - size.width()) // 2
    y = (scaled.height() - size.height()) // 2
    return scaled.copy(x, y, size.width(), size.height())


class _CameraPreview(QLabel):
    """Keep the original camera frame so a resize can refit it immediately."""

    def __init__(self) -> None:
        super().__init__("Starting camera…")
        self._frame: QPixmap | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(self.minimumSizeHint())

    def sizeHint(self) -> QSize:  # noqa: N802 — Qt override
        return QSize(PANE, PANE)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 — Qt override
        return QSize(192, 192)

    def set_frame(self, image: QImage) -> None:
        self._frame = QPixmap.fromImage(image)
        self._render_frame()

    def _render_frame(self) -> None:
        if self._frame is None or self._frame.isNull():
            return
        dpr = self.devicePixelRatioF()
        side = int(min(self.width(), self.height()) * dpr)
        if side < 1:
            return
        pixmap = _fill_square(self._frame, QSize(side, side))
        pixmap.setDevicePixelRatio(dpr)
        self.setPixmap(pixmap)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 — Qt override
        super().resizeEvent(event)
        self._render_frame()

    def clear(self) -> None:
        self._frame = None
        super().clear()


def _view_framed(inner: QWidget) -> QScrollArea:
    """Wrap an expanding widget so it gets the theme's native sunken "view"
    frame — the inset border item-views and text fields have. A plain ``QFrame``
    border is suppressed by some styles (Kvantum); a scroll-area's view frame is
    drawn natively. Same trick as the ENS renewal calendar. Scrollbars are off
    (the inner is sized to fit, so it never scrolls)."""
    scroll = QScrollArea()
    scroll.setWidget(inner)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    border = 2 * scroll.frameWidth()
    scroll.setMinimumSize(inner.minimumSizeHint() + QSize(border, border))
    return scroll


class QRExchangeDialog(Dialog):
    """Modal exchange: our request QR (left) and the live camera (right), side by
    side to suit a wide desktop screen. The first scanned ``ur:…`` accepts and is
    returned by :meth:`scanned_ur`."""

    # Animated-QR frame cadence (ms). Slow enough for a device camera to lock
    # onto each fragment, fast enough to cycle a few-part request quickly.
    FRAME_MS = 200

    def __init__(
        self, next_frame: Callable[[], str], *, scanner: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Scan with your air-gapped wallet")
        self._scanned: str | None = None
        self._scanner = scanner if scanner is not None else _default_scanner()
        # Pull a fresh UR each animation tick. A small tx returns the same string
        # (one static QR); a large tx returns an unbounded stream of fresh
        # fountain parts, so the device keeps getting new frames and converges.
        self._next_frame = next_frame
        self._shown: str | None = None

        root = QVBoxLayout(self)

        # Captions in row 0, the two square panes in row 1 — the grid keeps the
        # QR and camera aligned however the captions wrap. Caption↔pane gap is
        # item_spacing (within a paragraph); the between-column gap is
        # group_spacing (two logically distinct groups) — the house rhythm.
        grid = QGridLayout()
        grid.setVerticalSpacing(item_spacing(self))
        grid.setHorizontalSpacing(group_spacing(self))
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(1, 1)
        # Use each full column width so Qt's height-for-width calculation
        # matches the caption's actual width when the dialog is resized.
        top = Qt.AlignmentFlag.AlignTop

        show_caption = QLabel("1. Show this to your wallet's camera:")
        show_caption.setWordWrap(True)
        self._qr_label = QRWidget(preferred_side=PANE)
        grid.addWidget(show_caption, 0, 0, top)
        grid.addWidget(_view_framed(self._qr_label), 1, 0)

        scan_caption = QLabel("2. Point your camera at the wallet's signature QR:")
        scan_caption.setWordWrap(True)
        self._preview = _CameraPreview()
        grid.addWidget(scan_caption, 0, 1, top)
        grid.addWidget(_view_framed(self._preview), 1, 1)
        root.addLayout(grid, 1)

        self._render_frame()   # first frame
        self._anim: QTimer | None = QTimer(self)
        self._anim.timeout.connect(self._render_frame)
        self._anim.start(self.FRAME_MS)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        if self._scanner is not None:
            self._scanner.decoded.connect(self._on_decoded)
            self._scanner.frame.connect(self._on_frame)
            self._scanner.failed.connect(self._on_camera_failed)

    def scanned_ur(self) -> str | None:
        """The scanned ``ur:…`` string, or ``None`` if the user cancelled."""
        return self._scanned

    # --- lifecycle ---------------------------------------------------------

    def showEvent(self, event: Any) -> None:  # noqa: N802 — Qt override
        super().showEvent(event)
        if self._scanner is not None:
            self._scanner.start()
        else:
            self._preview.setText("No camera available")

    def done(self, result: int) -> None:  # noqa: N802 — Qt override
        if self._anim is not None:
            self._anim.stop()
        if self._scanner is not None:
            self._scanner.stop()
        super().done(result)

    # --- request animation -------------------------------------------------

    def _render_frame(self) -> None:
        ur_string = self._next_frame()
        if ur_string != self._shown:      # a constant single part renders once
            self._shown = ur_string
            # URs are case-insensitive; uppercase uses compact alphanumeric QR.
            self._qr_label.set_content(ur_string.upper(), error="l")

    # --- scanner signals ---------------------------------------------------

    def _on_decoded(self, text: str) -> None:
        candidate = text.strip()
        # Only a UR accepts — ignore any other barcode in view (the signer
        # validates the specific type). Accept once, and DEFER the close: this
        # runs inside the camera's frame-delivery callback, so closing here
        # would tear the QCamera down mid-frame and crash the FFmpeg backend.
        if self._scanned is None and candidate.lower().startswith("ur:"):
            self._scanned = candidate
            QTimer.singleShot(0, self.accept)

    def _on_frame(self, image: Any) -> None:
        self._preview.set_frame(image)

    def _on_camera_failed(self, message: str) -> None:
        self._preview.clear()
        self._preview.setText(message)


class QRScanDialog(Dialog):
    """Scan-only: run the camera and return the first ``ur:…`` seen (no QR to
    display). Used to read a wallet's account export at import. Same injectable
    ``scanner`` as :class:`QRExchangeDialog`."""

    def __init__(
        self, *, prompt: str = "Scan your wallet's account QR:",
        scanner: Any = None, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Scan air-gapped wallet")
        self._scanned: str | None = None
        self._scanner = scanner if scanner is not None else _default_scanner()

        root = QVBoxLayout(self)
        # The prompt and its (framed, square) camera pane are one paragraph.
        body = QVBoxLayout()
        body.setSpacing(item_spacing(self))
        body.addWidget(QLabel(prompt))
        self._preview = _CameraPreview()
        body.addWidget(_view_framed(self._preview), 1)
        root.addLayout(body, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        if self._scanner is not None:
            self._scanner.decoded.connect(self._on_decoded)
            self._scanner.frame.connect(self._on_frame)
            self._scanner.failed.connect(self._on_camera_failed)

    def scanned_ur(self) -> str | None:
        return self._scanned

    def showEvent(self, event: Any) -> None:  # noqa: N802 — Qt override
        super().showEvent(event)
        if self._scanner is not None:
            self._scanner.start()
        else:
            self._preview.setText("No camera available")

    def done(self, result: int) -> None:  # noqa: N802 — Qt override
        if self._scanner is not None:
            self._scanner.stop()
        super().done(result)

    def _on_decoded(self, text: str) -> None:
        candidate = text.strip()
        # Accept once, and defer the close out of the frame-delivery callback
        # (closing here crashes the FFmpeg camera backend — see QRExchangeDialog).
        if self._scanned is None and candidate.lower().startswith("ur:"):
            self._scanned = candidate
            QTimer.singleShot(0, self.accept)

    def _on_frame(self, image: Any) -> None:
        self._preview.set_frame(image)

    def _on_camera_failed(self, message: str) -> None:
        self._preview.clear()
        self._preview.setText(message)


def _default_scanner() -> Any:
    """The real camera scanner, or ``None`` if the camera can't open (QtMultimedia
    absent / no device). Kept out of import time."""
    try:
        from .qr_scan import CameraScanner
        return CameraScanner()
    except Exception:
        return None
