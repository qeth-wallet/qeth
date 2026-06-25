"""Transaction-activity rendering helpers for the transactions list.

The "what happened" field is split across **two columns**:

* the **verb** column is ordinary text (the decoded function name), so it
  picks up the theme font and the row's selection colour for free;
* the **coins** column shows the assets that moved, ``[leaving] → [entering]``
  — coin logos and a flow arrow, no text.

The coins column is drawn by a custom item delegate that calls
:func:`paint_summary` straight onto the view's painter: logos are blitted
(memoised explicit-smooth downscale) and the arrow is *vector*. Drawing at the
view's own device resolution — rather than rasterising into a QIcon the style
then rescales per row — keeps the thin arrow crisp and pixel-identical in every
row at any DPI.

Examples (icons shown as ``()``):

    vote
    transfer   (USDC)→
    deposit    (ETH)→(WETH)
    exchange   (USDC)→(WBTC)
    claim      →(USDC)(WBTC)
    approve    (USDC)            # approved token, no arrow

The arrow and any logo-less "generic" coins are drawn in the row's foreground
colour (``Text`` / ``HighlightedText``) so they follow the selection.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap

from ..icons import smooth_scaled

_ICON = 16        # coin diameter, logical px
_STEP = 18        # x-advance per coin
_ARROW = 16       # width reserved for the flow arrow (incl. side gaps)
_GAP = 4          # gap between the out-group and the arrow / in-group
_MAX_COINS = 4    # per side before a "+N" overflow dot
_PAD = 1          # tiny inset so antialiased edges aren't clipped


@dataclass(frozen=True)
class Coin:
    """One asset leg. ``icon`` is a (preferably circular) pixmap; when it
    is None or null a neutral lettered coin is drawn so a logo-less token
    still reads."""
    symbol: str
    icon: QPixmap | None = None


@dataclass(frozen=True)
class TxSummary:
    verb: str
    out: tuple[Coin, ...] = ()         # assets leaving the wallet
    inn: tuple[Coin, ...] = ()         # assets entering the wallet
    # Approvals set this False: the coin is the *approved* token, shown
    # without a flow arrow (nothing actually moved).
    show_arrow: bool = True
    # Reverted / dropped tx → render dimmed.
    muted: bool = False


def _block_width(coins: tuple[Coin, ...]) -> int:
    if not coins:
        return 0
    shown = min(len(coins), _MAX_COINS)
    w = shown * _STEP
    if len(coins) > shown:
        w += _ICON  # an overflow dot, roughly a coin wide
    return w


def _coins_width(summary: TxSummary) -> int:
    w = _block_width(summary.out)
    if summary.show_arrow and (summary.out or summary.inn):
        w += _ARROW
    inn = _block_width(summary.inn)
    if inn and w:
        w += _GAP
    w += inn
    return w


def _generic_coin(p: QPainter, x: int, top: int, symbol: str, fg: QColor) -> None:
    """Neutral coin for a logo-less token: an outlined ring + the symbol's
    initial, in the foreground colour (theme-safe)."""
    p.save()
    pen = QPen(fg)
    pen.setWidthF(1.2)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QRectF(x + 1, top + 1, _ICON - 2, _ICON - 2))
    f = QFont()
    f.setPixelSize(max(7, int(_ICON * 0.6)))
    f.setBold(True)
    p.setFont(f)
    p.drawText(QRectF(x, top, _ICON, _ICON),
               int(Qt.AlignmentFlag.AlignCenter), (symbol or "?")[:1].upper())
    p.restore()


# Logos are 64 px; the cells display at _ICON. Memoise the explicit
# bilinear downscale (keyed by content + size + dpr) so per-paint redraws in
# the item delegate don't re-scale the same logo every frame.
_SCALED_CACHE: dict = {}
_SCALED_CACHE_MAX = 2048


def _scaled_coin(src: QPixmap, dpr: float) -> QPixmap:
    key = (src.cacheKey(), _ICON, round(dpr * 100))
    hit = _SCALED_CACHE.get(key)
    if hit is None:
        if len(_SCALED_CACHE) >= _SCALED_CACHE_MAX:
            _SCALED_CACHE.clear()
        hit = smooth_scaled(src, _ICON, dpr)
        _SCALED_CACHE[key] = hit
    return hit


def _draw_block(coins: tuple[Coin, ...], p: QPainter, fg: QColor,
                x: int, top: int) -> int:
    if not coins:
        return x
    shown = coins[:_MAX_COINS]
    overflow = len(coins) - len(shown)
    # dpr of the surface we're painting into, so the explicit downscale below
    # targets real device pixels.
    dpr = p.device().devicePixelRatioF()
    for c in shown:
        if c.icon is not None and not c.icon.isNull():
            # Scale the (64px) logo down with an EXPLICIT bilinear filter and
            # blit it 1:1, rather than letting p.drawPixmap(...,w,h,...) scale
            # it via the SmoothPixmapTransform *hint* — some Qt builds ignore
            # that hint and fall back to nearest, which renders the coin blocky.
            p.drawPixmap(x, top, _scaled_coin(c.icon, dpr))
        else:
            _generic_coin(p, x, top, c.symbol, fg)
        x += _STEP
    if overflow > 0:
        # small filled dot standing in for "+N more" — no text, no font.
        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fg)
        d = _ICON // 3
        p.drawEllipse(QRectF(x + (_ICON - d) / 2, top + (_ICON - d) / 2, d, d))
        p.restore()
        x += _ICON
    return x


def _draw_arrow(p: QPainter, fg: QColor, x: int, cy: float) -> int:
    p.save()
    pen = QPen(fg)
    pen.setWidthF(1.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    x0 = float(x + 2)
    x1 = float(x + _ARROW - 4)
    p.drawLine(QPointF(x0, cy), QPointF(x1, cy))
    p.drawLine(QPointF(x1, cy), QPointF(x1 - 4, cy - 3.5))
    p.drawLine(QPointF(x1, cy), QPointF(x1 - 4, cy + 3.5))
    p.restore()
    return x + _ARROW


def paint_summary(p: QPainter, summary: TxSummary, fg: QColor,
                  x: int, top: int) -> int:
    """Draw the moved-assets row (out coins → in coins) straight onto ``p``
    starting at (x, top), and return the x past the last element.

    The arrow is *vector* — rendered onto the destination surface at its real
    device resolution — so it is crisp and pixel-identical in every row at any
    DPI, with no rasterise-then-rescale step (the item delegate calls this
    directly on the view's painter). Coin logos are blitted via the memoised
    explicit-smooth downscale in :func:`_draw_block`.
    """
    start = x
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    if summary.muted:
        p.setOpacity(0.5)
    cy = top + _ICON / 2
    x = _draw_block(summary.out, p, fg, x, top)
    if summary.show_arrow and (summary.out or summary.inn):
        x = _draw_arrow(p, fg, x, cy)
    if summary.inn and x > start:
        x += _GAP
    x = _draw_block(summary.inn, p, fg, x, top)
    p.restore()
    return x


def coins_content_width(summary: TxSummary) -> int:
    """Logical px the drawn coins row occupies — the delegate's sizeHint."""
    return _coins_width(summary) + 2 * _PAD
