"""The air-gapped QR exchange window: shows the request UR as a QR *and* runs
the camera to read the device's response QR at the same time. Returns the
scanned ``ur:…`` string (or ``None`` on cancel) — the signer owns UR decode.

Step 3c of docs/signers-qr.md. The camera ``scanner`` is injectable (default:
``qr_scan.CameraScanner``), so the dialog's accept/cancel/decode logic is
unit-tested with a fake source; the real QtMultimedia camera is verified on
hardware.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

import segno
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .dialog import Dialog, item_spacing


def ur_to_pixmap(ur_string: str, *, scale: int = 6) -> QPixmap:
    """Render a UR string as a QR ``QPixmap``. UR is uppercased so the QR uses
    the compact alphanumeric mode (``ur:``/``/``/``-`` and digits are all in
    that charset)."""
    buf = io.BytesIO()
    segno.make(ur_string.upper(), error="l").save(buf, kind="png", scale=scale)
    pixmap = QPixmap()
    pixmap.loadFromData(buf.getvalue())   # PNG auto-detected
    return pixmap


class QRExchangeDialog(Dialog):
    """Modal exchange: our request QR (top) + the live camera (bottom). The
    first scanned ``ur:…`` accepts and is returned by :meth:`scanned_ur`."""

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

        show = QVBoxLayout()
        show.setSpacing(item_spacing(self))
        show.addWidget(QLabel(
            "1. Show this to your wallet's camera "
            "(it animates for a large transaction):"))
        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        show.addWidget(self._qr_label, alignment=Qt.AlignmentFlag.AlignCenter)
        root.addLayout(show)

        self._render_frame()   # first frame
        self._anim: QTimer | None = QTimer(self)
        self._anim.timeout.connect(self._render_frame)
        self._anim.start(self.FRAME_MS)

        scan = QVBoxLayout()
        scan.setSpacing(item_spacing(self))
        scan.addWidget(QLabel(
            "2. Point your camera at the wallet's signature QR:"))
        self._preview = QLabel("Starting camera…")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setMinimumSize(560, 420)      # a usably-large camera view
        self._preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        scan.addWidget(self._preview)
        root.addLayout(scan)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        if self._scanner is not None:
            self._scanner.decoded.connect(self._on_decoded)
            self._scanner.frame.connect(self._on_frame)

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
            self._qr_label.setPixmap(ur_to_pixmap(ur_string))

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
        self._preview.setPixmap(QPixmap.fromImage(image).scaled(
            self._preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))


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
        body = QVBoxLayout()
        body.setSpacing(item_spacing(self))
        body.addWidget(QLabel(prompt))
        self._preview = QLabel("Starting camera…")
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setMinimumSize(560, 420)      # a usably-large camera view
        self._preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body.addWidget(self._preview)
        root.addLayout(body)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        if self._scanner is not None:
            self._scanner.decoded.connect(self._on_decoded)
            self._scanner.frame.connect(self._on_frame)

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
        self._preview.setPixmap(QPixmap.fromImage(image).scaled(
            self._preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))


def _default_scanner() -> Any:
    """The real camera scanner, or ``None`` if the camera can't open (QtMultimedia
    absent / no device). Kept out of import time."""
    try:
        from .qr_scan import CameraScanner
        return CameraScanner()
    except Exception:
        return None
