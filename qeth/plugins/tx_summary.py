"""Transaction-activity rendering helpers for the transactions list.

The "what happened" field is split across **two columns** so each uses
plain, standard cell rendering (no custom delegate, no whole-line image —
both of which fought the view / mangled font rendering):

* the **verb** column is ordinary text (the decoded function name), so it
  picks up the theme font and the row's selection colour for free;
* the **coins** column is a small icons-only image — the assets that
  moved, ``[leaving] → [entering]`` — built by :func:`coins_icon`. It
  holds *no text*, only coin pixmaps and a vector arrow, so there's no
  font in the pixmap to render badly.

Examples (icons shown as ``()``):

    vote
    transfer   (USDC)→
    deposit    (ETH)→(WETH)
    exchange   (USDC)→(WBTC)
    claim      →(USDC)(WBTC)
    approve    (USDC)            # approved token, no arrow

``coins_icon`` returns a QIcon with Normal/Selected pixmaps so the vector
arrow and any logo-less "generic" coins follow the row selection colour.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap

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


def _draw_block(coins: tuple[Coin, ...], p: QPainter, fg: QColor,
                x: int, top: int) -> int:
    if not coins:
        return x
    shown = coins[:_MAX_COINS]
    overflow = len(coins) - len(shown)
    for c in shown:
        if c.icon is not None and not c.icon.isNull():
            p.drawPixmap(x, top, _ICON, _ICON, c.icon)
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


# coins_icon is a pure function of its inputs, and a wallet switch re-renders
# ~200 rows of mostly-identical icons (the generic lettered coin especially) —
# QPainter vector work that profiled at ~210 ms per switch on the MAIN thread,
# stretching to ~0.5 s when something else pegs the CPU. Memoize the composed
# QIcon: QPixmap.cacheKey() identifies a coin pixmap's contents, rgba() the
# theme colours, so repeat renders are a dict hit. Bounded by wholesale clear —
# entries are a few KB and 4096 covers many wallets of distinct activity.
_ICON_CACHE: dict = {}
_ICON_CACHE_MAX = 4096


def _coin_key(c: Coin) -> tuple[int | None, str]:
    icon = c.icon
    if icon is None or icon.isNull():
        return (None, c.symbol)
    return (icon.cacheKey(), c.symbol)


def coins_icon(summary: TxSummary, normal_fg: QColor, selected_fg: QColor,
               dpr: float = 1.0) -> QIcon:
    """Composite the moved-assets row (out → in) into a QIcon — coin icons
    + a vector arrow only, never text. Returns an empty icon when nothing
    moved (e.g. a bare ``vote``). Memoized (see ``_ICON_CACHE`` above)."""
    if not (summary.out or summary.inn):
        return QIcon()
    key = (
        tuple(_coin_key(c) for c in summary.out),
        tuple(_coin_key(c) for c in summary.inn),
        summary.show_arrow, summary.muted,
        normal_fg.rgba(), selected_fg.rgba(), round(dpr * 100),
    )
    hit = _ICON_CACHE.get(key)
    if hit is not None:
        return hit
    icon = _render_coins_icon(summary, normal_fg, selected_fg, dpr)
    if len(_ICON_CACHE) >= _ICON_CACHE_MAX:
        _ICON_CACHE.clear()
    _ICON_CACHE[key] = icon
    return icon


def _render_coins_icon(summary: TxSummary, normal_fg: QColor,
                       selected_fg: QColor, dpr: float) -> QIcon:
    w = _coins_width(summary) + 2 * _PAD
    h = _ICON + 2 * _PAD
    icon = QIcon()
    for mode, fg in ((QIcon.Mode.Normal, normal_fg),
                     (QIcon.Mode.Selected, selected_fg)):
        pm = QPixmap(max(1, round(w * dpr)), max(1, round(h * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if summary.muted:
            p.setOpacity(0.5)
        x = _PAD
        top = _PAD
        cy = top + _ICON / 2
        x = _draw_block(summary.out, p, fg, x, top)
        if summary.show_arrow and (summary.out or summary.inn):
            x = _draw_arrow(p, fg, x, cy)
        if summary.inn and x > _PAD:
            x += _GAP
        _draw_block(summary.inn, p, fg, x, top)
        p.end()
        icon.addPixmap(pm, mode)
    return icon
