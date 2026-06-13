"""Disk + memory cache for token icons.

Disk layout: ``~/.qeth/icons/<chain_id>/<addr_lower>.<ext>`` (extension from
the URL or ".png" default). Memory cache holds QPixmaps for fast re-paint.

Fetches happen in their own QThread; the UI subscribes to ``icon_ready``
and refreshes the affected row when each one lands.
"""

import logging
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, QRect, QRectF, Qt, QThread, Signal
from PySide6.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPen,
                           QPixmap)

from . import QULONGLONG, USER_AGENT

log = logging.getLogger("qeth.icons")

ICONS_DIR = Path.home() / ".qeth" / "icons"
CHAIN_ICONS_DIR = ICONS_DIR / "chains"
BUNDLED_NATIVE_DIR = Path(__file__).parent / "assets" / "native"
BUNDLED_CHAIN_DIR = Path(__file__).parent / "assets" / "chains"
FETCH_TIMEOUT = 10.0


# Chain-id → slug in each upstream icon source. Slug conventions
# differ between sources (Curve uses "xdai" for Gnosis,
# TrustWallet uses "smartchain" for BSC, …) so we keep one map
# per source rather than try to derive. When a chain is missing
# from every source ChainIconCache silently keeps the combo
# textual; the user can drop a bundled PNG into
# qeth/assets/chains/<chain_id>.png to override.
_CURVE_CHAIN_SLUGS: dict[int, str] = {
    1: "ethereum", 10: "optimism", 56: "bsc", 100: "xdai",
    137: "polygon", 239: "tac", 324: "zksync",
    8453: "base", 42161: "arbitrum", 43114: "avalanche",
}
_TRUSTWALLET_CHAIN_SLUGS: dict[int, str] = {
    1: "ethereum", 10: "optimism", 56: "smartchain", 100: "xdai",
    137: "polygon", 324: "zksync",
    8453: "base", 42161: "arbitrum", 43114: "avalanchec",
}


def _chain_icon_urls(chain_id: int) -> list[str]:
    """Ordered list of upstream URLs to try for a chain logo.
    Curve first — their set covers TAC and matches the wallet's
    visual style (Curve also supplies our token-list source).
    TrustWallet second as a long-established backup."""
    urls: list[str] = []
    slug = _CURVE_CHAIN_SLUGS.get(int(chain_id))
    if slug:
        urls.append(
            "https://raw.githubusercontent.com/curvefi/curve-assets/main/"
            f"chains/{slug}.png"
        )
    slug = _TRUSTWALLET_CHAIN_SLUGS.get(int(chain_id))
    if slug:
        urls.append(
            "https://raw.githubusercontent.com/trustwallet/assets/master/"
            f"blockchains/{slug}/info/logo.png"
        )
    return urls


CIRCULAR_RENDER_SIZE = 64   # Rendered once, scaled down by Qt for display.


def to_circular(src: QPixmap, size: int = CIRCULAR_RENDER_SIZE) -> QPixmap:
    """Return a copy of ``src`` cropped to a circle, anti-aliased.

    Source is centre-cropped to a square first (KeepAspectRatioByExpanding)
    so non-square inputs don't get squashed. Rendered at a fixed 64×64 and
    then scaled down by the view at draw time — fine for the 18–20 px
    cells we display in and avoids re-rendering on every paint.
    """
    if src.isNull() or size <= 0:
        return src
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    path = QPainterPath()
    path.addEllipse(0.0, 0.0, float(size), float(size))
    painter.setClipPath(path)
    scaled = src.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    x = (scaled.width() - size) // 2
    y = (scaled.height() - size) // 2
    painter.drawPixmap(-x, -y, scaled)
    painter.end()
    return out


# Direction-badge colours for the notification icon. This is a standalone
# graphic rendered into a freedesktop notification (outside the app's Qt
# palette), so fixed colours are fine here — green = incoming, blue = outgoing.
_RECEIVED_COLOR = QColor(34, 160, 70)
_SENT_COLOR = QColor(40, 110, 210)


def _arrow_path(cx: float, cy: float, s: float, up: bool) -> QPainterPath:
    """A filled triangle centred at (cx, cy): apex up (sent) or down
    (received), half-extent ``s``."""
    path = QPainterPath()
    if up:
        path.moveTo(cx, cy - s)
        path.lineTo(cx - s, cy + s * 0.7)
        path.lineTo(cx + s, cy + s * 0.7)
    else:
        path.moveTo(cx, cy + s)
        path.lineTo(cx - s, cy - s * 0.7)
        path.lineTo(cx + s, cy - s * 0.7)
    path.closeSubpath()
    return path


def _themed_arrow_white(name: str, px: int) -> "QPixmap | None":
    """The desktop icon theme's ``name`` (e.g. ``go-up``) rendered to exactly
    ``px``×``px`` at device-pixel-ratio 1 and tinted solid white, or ``None``
    when the theme doesn't provide it (then the caller draws a triangle).

    Rendering to an explicit dpr-1 pixmap is load-bearing: on a HiDPI screen
    ``QIcon.pixmap(px)`` hands back a 2× physical pixmap whose ``width()`` is
    ``2*px``, and placing it by that width drops the badge in the wrong spot.
    """
    if px <= 0:
        return None
    icon = QIcon.fromTheme(name)
    if icon.isNull():
        return None
    src = icon.pixmap(px, px)
    if src.isNull():
        return None
    out = QPixmap(px, px)
    out.setDevicePixelRatio(1.0)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.drawPixmap(QRect(0, 0, px, px), src)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(out.rect(), QColor(255, 255, 255))
    p.end()
    return out


def _draw_direction_badge(
    painter: QPainter, cx: float, cy: float, r: float, outgoing: bool,
) -> None:
    """A coloured disc (green = received, blue = sent) with a white arrow,
    centred at (cx, cy), radius ``r``; a thin white ring separates it from
    whatever's behind. The arrow is the desktop theme's own ``go-up``/
    ``go-down`` glyph when available (so it matches the user's icon set),
    falling back to a hand-drawn triangle (portable Flatpak/AppImage runtimes
    may not ship those names)."""
    painter.setPen(QPen(QColor(255, 255, 255), max(1.0, r * 0.12)))
    painter.setBrush(_SENT_COLOR if outgoing else _RECEIVED_COLOR)
    painter.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    painter.setPen(Qt.PenStyle.NoPen)
    asz = int(r * 1.25)
    arrow = _themed_arrow_white("go-up" if outgoing else "go-down", asz)
    if arrow is not None:
        painter.drawPixmap(int(cx - asz / 2), int(cy - asz / 2), arrow)
    else:
        painter.setBrush(QColor(255, 255, 255))
        painter.drawPath(_arrow_path(cx, cy, r * 0.5, up=outgoing))


def notification_icon(
    base: "QPixmap | None", outgoing: bool, size: int = 64,
) -> QIcon:
    """Compose the icon for a sent/received desktop notification: the token /
    coin icon (circular) with a small ↑/↓ direction badge in the lower-right
    corner. When ``base`` is missing (a brand-new inbound token whose logo
    isn't cached yet), the direction badge fills the icon on its own — so the
    notification always carries the direction visually, never the generic
    'i'."""
    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    if base is not None and not base.isNull():
        p.drawPixmap(0, 0, to_circular(base, size))
        # Small badge flush in the bottom-right corner (minimal overlap with
        # the coin/token logo).
        r = size * 0.22
        off = size - r - size * 0.015
        _draw_direction_badge(p, off, off, r, outgoing)
    else:
        _draw_direction_badge(p, size / 2.0, size / 2.0, size * 0.46, outgoing)
    p.end()
    return QIcon(canvas)


def bundled_native_icon(symbol: str) -> QPixmap | None:
    """Return the bundled native-asset icon for a chain symbol (ETH, MATIC,
    …), or None if no file ships for that symbol. Cropped to a circle for
    visual consistency with token icons."""
    p = BUNDLED_NATIVE_DIR / f"{symbol.upper()}.png"
    if not p.exists():
        return None
    pix = QPixmap(str(p))
    return to_circular(pix) if not pix.isNull() else None


def bundled_chain_icon(chain_id: int) -> QPixmap | None:
    """Return the bundled chain logo by chain id, or None if no file ships
    for that chain. Distinct from the native-asset icon: e.g. Optimism's
    chain logo is the red O, not the ETH diamond. Circular-cropped."""
    p = BUNDLED_CHAIN_DIR / f"{int(chain_id)}.png"
    if not p.exists():
        return None
    pix = QPixmap(str(p))
    return to_circular(pix) if not pix.isNull() else None


class _ChainIconFetchWorker(QThread):
    """Walk a list of upstream URLs and return the first usable
    image. Failures (404, network error, file too small) skip
    quietly to the next URL; only when all sources miss do we
    emit ``None`` so the caller can drop the row entirely."""

    fetched = Signal(QULONGLONG, object)  # (chain_id, bytes | None)

    def __init__(self, chain_id: int, urls: list[str], parent=None):
        super().__init__(parent)
        self.chain_id = chain_id
        self.urls = urls

    def run(self) -> None:
        for url in self.urls:
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": USER_AGENT},
                )
                with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                    data = r.read()
                if len(data) < 32:
                    continue
                self.fetched.emit(self.chain_id, data)
                return
            except Exception as e:
                log.debug("chain icon fetch failed: %s — %s", url, e)
                continue
        self.fetched.emit(self.chain_id, None)


class ChainIconCache(QObject):
    """Three-tier resolver for chain logos: in-memory QPixmap →
    bundled asset → on-disk download cache. Misses kick a
    background fetch from Curve curve-assets / TrustWallet
    assets; subscribers refresh the affected widget when
    ``icon_ready`` fires.

    Disk layout: ``~/.qeth/icons/chains/<chain_id>.png``."""

    icon_ready = Signal(QULONGLONG)  # chain_id (dapp-added ids can exceed qint32)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mem: dict[int, QPixmap] = {}
        self._inflight: set[int] = set()
        self._lock = threading.Lock()
        self._workers: list[_ChainIconFetchWorker] = []
        CHAIN_ICONS_DIR.mkdir(parents=True, exist_ok=True)

    def get(self, chain_id: int) -> QPixmap | None:
        cid = int(chain_id)
        pix = self._mem.get(cid)
        if pix is not None:
            return pix
        # Bundled wins — ships with the wheel, no network.
        pix = bundled_chain_icon(cid)
        if pix is not None:
            self._mem[cid] = pix
            return pix
        p = CHAIN_ICONS_DIR / f"{cid}.png"
        if p.exists():
            raw = QPixmap(str(p))
            if not raw.isNull():
                pix = to_circular(raw)
                self._mem[cid] = pix
                return pix
        return None

    def request(self, chain_id: int) -> None:
        cid = int(chain_id)
        if cid in self._mem:
            return
        urls = _chain_icon_urls(cid)
        if not urls:
            return
        with self._lock:
            if cid in self._inflight:
                return
            self._inflight.add(cid)
        worker = _ChainIconFetchWorker(cid, urls, self)
        worker.fetched.connect(self._on_fetched)
        worker.finished.connect(
            lambda w=worker:
                self._workers.remove(w) if w in self._workers else None
        )
        self._workers.append(worker)
        worker.start()

    def _on_fetched(self, chain_id: int, data) -> None:
        with self._lock:
            self._inflight.discard(int(chain_id))
        if data is None:
            return
        raw = QPixmap()
        if not raw.loadFromData(bytes(data)):
            return
        self._mem[int(chain_id)] = to_circular(raw)
        try:
            (CHAIN_ICONS_DIR / f"{int(chain_id)}.png").write_bytes(bytes(data))
        except Exception as e:
            log.debug("chain icon save failed: %s", e)
        self.icon_ready.emit(int(chain_id))


class _IconFetchWorker(QThread):
    fetched = Signal(QULONGLONG, str, object)  # (chain_id, contract_lower, bytes|None)

    def __init__(self, chain_id: int, contract: str, url: str, parent=None):
        super().__init__(parent)
        self.chain_id = chain_id
        self.contract = contract.lower()
        self.url = url

    def run(self) -> None:
        try:
            req = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                data = r.read()
            if len(data) < 32:
                self.fetched.emit(self.chain_id, self.contract, None)
                return
            self.fetched.emit(self.chain_id, self.contract, data)
        except Exception as e:
            log.debug("icon fetch failed: %s — %s", self.url, e)
            self.fetched.emit(self.chain_id, self.contract, None)


class IconCache(QObject):
    """Two-tier cache: in-memory QPixmap + on-disk file.

    Use ``get(chain_id, contract)`` for the synchronous fast path (returns
    None on miss). Use ``request(chain_id, contract, url)`` to kick off a
    background fetch; subscribe to ``icon_ready`` to update the UI when the
    icon arrives.
    """

    icon_ready = Signal(QULONGLONG, str)  # (chain_id, contract_lower)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._mem: dict[tuple[int, str], QPixmap] = {}
        self._inflight: set[tuple[int, str]] = set()
        self._lock = threading.Lock()
        self._workers: list[_IconFetchWorker] = []
        ICONS_DIR.mkdir(parents=True, exist_ok=True)

    # ---- public ----------------------------------------------------------

    def get(self, chain_id: int, contract: str) -> QPixmap | None:
        key = (chain_id, contract.lower())
        pix = self._mem.get(key)
        if pix is not None:
            return pix
        for p in self._candidate_paths(chain_id, contract):
            if p.exists():
                raw = QPixmap(str(p))
                if not raw.isNull():
                    pix = to_circular(raw)
                    self._mem[key] = pix
                    return pix
        return None

    def request(self, chain_id: int, contract: str, url: str | None) -> None:
        if not url:
            return
        key = (chain_id, contract.lower())
        if key in self._mem:
            return
        with self._lock:
            if key in self._inflight:
                return
            self._inflight.add(key)
        worker = _IconFetchWorker(chain_id, contract, url, self)
        worker.fetched.connect(self._on_fetched)
        worker.finished.connect(lambda w=worker: self._workers.remove(w) if w in self._workers else None)
        self._workers.append(worker)
        worker.start()

    # ---- internal --------------------------------------------------------

    def _dir(self, chain_id: int) -> Path:
        return ICONS_DIR / str(chain_id)

    def _candidate_paths(self, chain_id: int, contract: str) -> list[Path]:
        d = self._dir(chain_id)
        c = contract.lower()
        # Most icons are PNG; some lists serve SVG/JPG. Try all common extensions.
        return [d / f"{c}.png", d / f"{c}.jpg", d / f"{c}.svg", d / f"{c}.webp"]

    def _ext_from_url(self, url: str) -> str:
        path = urllib.parse.urlparse(url).path.lower()
        for ext in (".png", ".jpg", ".jpeg", ".svg", ".webp"):
            if path.endswith(ext):
                return ".jpg" if ext == ".jpeg" else ext
        return ".png"

    def _on_fetched(self, chain_id: int, contract: str, data) -> None:
        key = (chain_id, contract.lower())
        with self._lock:
            self._inflight.discard(key)
        if data is None:
            return
        raw = QPixmap()
        if not raw.loadFromData(bytes(data)):
            return  # unsupported format (e.g. SVG) — could add QSvgRenderer later
        # Memory cache stores the circular-cropped version that will be
        # handed to QIcon directly; the original square image is what we
        # write to disk so a future code change can re-process it.
        self._mem[key] = to_circular(raw)
        path = self._dir(chain_id) / f"{contract.lower()}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw.save(str(path), "PNG")
        except Exception as e:
            log.debug("icon save failed: %s — %s", path, e)
        self.icon_ready.emit(chain_id, contract.lower())
