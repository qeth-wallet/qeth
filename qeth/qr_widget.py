"""Responsive QR rendering shared by signing and receive-address dialogs."""

from __future__ import annotations

import io

import segno
from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QImage, QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget


def ur_animation_version(content: str) -> int | None:
    """Reserve one version per message for the fixed-framing trial baseline.

    Multipart sequence numbers appear both in the URI and in CBOR. Reserve ten
    decimal digits and four additional CBOR bytes (eight Bytewords characters)
    for a uint32 sequence number. This covers years of continuous animation.
    This stabilizes geometry but can reduce pixels per module. An optical
    advantage over choosing each frame's smallest version is not established.
    Single-part URs need no reservation.
    """
    fields = content.upper().split("/")
    if len(fields) != 3:
        return None
    head, sequence, body = fields
    number, separator, count = sequence.partition("-")
    if not separator or not number.isdecimal() or not count.isdecimal():
        return None
    largest = f"{head}/4294967295-{count}/{body}{'A' * 8}"
    return int(segno.make_qr(largest, error="l").version)


def qr_to_image(
    content: str,
    *,
    error: str = "m",
    scale: int = 1,
    version: int | None = None,
    mask_shift: int = 0,
) -> QImage:
    """Encode without changing the content's case; callers normalize URs only."""
    buf = io.BytesIO()
    qr = segno.make_qr(content, error=error, version=version)
    if mask_shift % 8:
        # Change the optical pattern on retries without changing a single UR
        # byte. The first attempt retains Segno's lowest-penalty standard mask.
        qr = segno.make_qr(
            content,
            error=qr.error,
            version=qr.version,
            mask=(qr.mask + mask_shift) % 8,
            boost_error=False,
        )
    qr.save(buf, kind="png", scale=scale, border=4, dark="#000", light="#fff")
    return QImage.fromData(buf.getvalue())


def qr_to_pixmap(
    content: str,
    *,
    error: str = "m",
    scale: int = 1,
    version: int | None = None,
    mask_shift: int = 0,
) -> QPixmap:
    return QPixmap.fromImage(
        qr_to_image(
            content,
            error=error,
            scale=scale,
            version=version,
            mask_shift=mask_shift,
        )
    )


class QRWidget(QWidget):
    """Fit a QR into the available space using whole physical pixels per module.

    The one-pixel-per-module source is encoded once per new payload. Resizing
    only rescales that source, independently of the signing frame generator.
    Size hints never depend on the rendered image, so enlarging the window
    cannot raise its minimum size and prevent it from shrinking again.
    """

    def __init__(self, parent: QWidget | None = None, *, preferred_side: int = 320):
        super().__init__(parent)
        self._preferred_side = preferred_side
        self._source = QPixmap()
        self._rendered = QPixmap()
        self._render_key: tuple[int, float] | None = None
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(self.minimumSizeHint())

    def sizeHint(self) -> QSize:  # noqa: N802 — Qt override
        return QSize(self._preferred_side, self._preferred_side)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 — Qt override
        # Even a version-40 QR plus its quiet zone fits at one pixel/module.
        return QSize(192, 192)

    def set_content(
        self,
        content: str,
        *,
        error: str = "m",
        version: int | None = None,
        mask_shift: int = 0,
    ) -> None:
        self._source = qr_to_pixmap(
            content, error=error, version=version, mask_shift=mask_shift
        )
        self._render_key = None
        self.update()

    def set_image(self, image: QImage) -> None:
        """Install a worker-prepared image; QPixmap stays on the GUI thread."""
        self._source = QPixmap.fromImage(image)
        self._render_key = None
        self.update()

    def pixmap(self) -> QPixmap:
        """The fitted square, including its quiet zone and display pixel ratio."""
        if self._source.isNull():
            return QPixmap()
        dpr = self.devicePixelRatioF()
        physical_size = self.size() * dpr  # Match Qt's backing-store rounding.
        side = min(physical_size.width(), physical_size.height())
        scale = side // self._source.width()
        if scale < 1:
            return QPixmap()  # Never crop or downsample away entire modules.
        key = (scale, dpr)
        if key != self._render_key:
            pixels = self._source.width() * scale
            self._rendered = self._source.scaled(
                pixels,
                pixels,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            self._rendered.setDevicePixelRatio(dpr)
            self._render_key = key
        return QPixmap(self._rendered)

    def event(self, event: QEvent) -> bool:
        # Supported system Qt 6.4 builds do not expose this newer event type.
        if event.type() == getattr(QEvent.Type, "DevicePixelRatioChange", None):
            self.update()
        return super().event(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 — Qt override
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.white)
        pixmap = self.pixmap()
        if pixmap.isNull():
            return
        # Paint in physical pixels, including the origin. Centering in logical
        # pixels can otherwise land on a half-pixel at fractional display scales.
        dpr = self.devicePixelRatioF()
        pixmap.setDevicePixelRatio(1)
        painter.scale(1 / dpr, 1 / dpr)
        physical_size = self.size() * dpr
        x = (physical_size.width() - pixmap.width()) // 2
        y = (physical_size.height() - pixmap.height()) // 2
        painter.drawPixmap(x, y, pixmap)
