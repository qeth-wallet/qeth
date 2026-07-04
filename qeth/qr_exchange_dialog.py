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
    QGridLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .dialog import Dialog, group_spacing, item_spacing


def ur_to_pixmap(ur_string: str, *, scale: int = 8) -> QPixmap:
    """Render a UR string as a QR ``QPixmap``. UR is uppercased so the QR uses
    the compact alphanumeric mode (``ur:``/``/``/``-`` and digits are all in
    that charset). ``scale`` is the source px-per-module — high enough that the
    dialog can scale the result down to its pane crisply (the on-screen size is
    the dialog's ``PANE``, not this)."""
    buf = io.BytesIO()
    segno.make(ur_string.upper(), error="l").save(buf, kind="png", scale=scale)
    pixmap = QPixmap()
    pixmap.loadFromData(buf.getvalue())   # PNG auto-detected
    return pixmap


class QRExchangeDialog(Dialog):
    """Modal exchange: our request QR (left) and the live camera (right), side by
    side to suit a wide desktop screen. The first scanned ``ur:…`` accepts and is
    returned by :meth:`scanned_ur`."""

    # Animated-QR frame cadence (ms). Slow enough for a device camera to lock
    # onto each fragment, fast enough to cycle a few-part request quickly.
    FRAME_MS = 200

    # Side of each square pane (QR and camera), px. Both panes are the same size
    # so they read as a matched pair; tune here if the QR wants to be larger.
    PANE = 320

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
        top = Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft

        show_caption = QLabel("1. Show this to your wallet's camera:")
        show_caption.setWordWrap(True)
        self._qr_label = QLabel()
        self._qr_label.setFixedSize(self.PANE, self.PANE)
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(show_caption, 0, 0, top)
        grid.addWidget(self._qr_label, 1, 0)

        scan_caption = QLabel("2. Point your camera at the wallet's signature QR:")
        scan_caption.setWordWrap(True)
        self._preview = QLabel("Starting camera…")
        self._preview.setFixedSize(self.PANE, self.PANE)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(scan_caption, 0, 1, top)
        grid.addWidget(self._preview, 1, 1)
        root.addLayout(grid)

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
            # Fit the QR to the square pane. FastTransformation (nearest) keeps
            # the module edges hard black/white — best for the device's scan —
            # rather than the grey-fringed edges smooth scaling would give.
            pixmap = ur_to_pixmap(ur_string).scaled(
                self.PANE, self.PANE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation)
            self._qr_label.setPixmap(pixmap)

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
