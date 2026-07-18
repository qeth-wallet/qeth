"""Disk + memory cache for token icons.

Disk layout: ``~/.qeth/icons/<chain_id>/<addr_lower>.<ext>`` (extension from
the URL or ".png" default). Memory cache holds QPixmaps for fast re-paint.

Fetches happen in their own QThread; the UI subscribes to ``icon_ready``
and refreshes the affected row when each one lands.
"""

import logging
import re
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from PySide6.QtCore import (QBuffer, QObject, QRectF, Qt,
                            QThread, Signal)
from PySide6.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPen,
                           QPixmap)

from . import QULONGLONG, USER_AGENT
from .fsatomic import atomic_write_bytes

log = logging.getLogger("qeth.icons")

ICONS_DIR = Path.home() / ".qeth" / "icons"
CHAIN_ICONS_DIR = ICONS_DIR / "chains"
BUNDLED_NATIVE_DIR = Path(__file__).parent / "assets" / "native"
BUNDLED_CHAIN_DIR = Path(__file__).parent / "assets" / "chains"
FETCH_TIMEOUT = 10.0

# Ceiling on an icon response body. Real token/chain logos are a few KB;
# anything past this is either a mistake or a deliberate memory-DoS via a
# poisoned token list's logoURI. Read is capped at the cap+1 byte so an
# oversize body is detected without slurping it whole.
_MAX_ICON_BYTES = 2 * 1024 * 1024   # 2 MiB


def _safe_icon_fetch(url: str) -> bytes:
    """Fetch an icon URL defensively and return its body (capped).

    A token list's ``logoURI`` is third-party data, so the URL is untrusted:
    restrict it to ``http(s)`` (no ``file://`` local reads, no ``ftp://`` …),
    refuse obvious loopback/private/link-local hosts (a poisoned list shouldn't
    turn the wallet into an SSRF probe of the user's LAN or its own
    127.0.0.1:1248 RPC), and cap the read so a huge body can't exhaust memory.
    Raises ``ValueError`` on a rejected URL, propagates transport errors."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"refusing non-http(s) icon URL: {url!r}")
    host = (parsed.hostname or "").lower()
    if _is_private_host(host):
        raise ValueError(f"refusing private/loopback icon host: {host!r}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        data = r.read(_MAX_ICON_BYTES + 1)
    if len(data) > _MAX_ICON_BYTES:
        raise ValueError(f"icon body exceeds {_MAX_ICON_BYTES} bytes: {url!r}")
    return data


def _is_private_host(host: str) -> bool:
    """True for a host we won't fetch icons from: localhost, a loopback /
    private / link-local IP literal, or an unqualified name (``.local`` etc.).
    Best-effort literal check — DNS rebinding can still resolve a public name
    to a private IP, but this blocks the cheap, direct SSRF a poisoned token
    list would attempt."""
    import ipaddress
    if not host or host == "localhost" or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False   # a normal DNS name — allowed
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_unspecified)


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


def _name_slug_candidates(name: str) -> list[str]:
    """Plausible Curve-assets filename slugs derived from a chain's
    display name. Curve names most files by the chain name lowercased
    with separators removed or hyphenated ("Hyperliquid" → hyperliquid,
    "X Layer" → x-layer), so try both forms. The id→slug map still wins
    for chains Curve names by an alias the display name can't yield
    (Gnosis → xdai, BNB Smart Chain → bsc). A wrong guess just 404s and
    is swallowed by the fetch worker."""
    base = name.strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", base)
    hyphen = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    out: list[str] = []
    for s in (compact, hyphen):
        if s and s not in out:
            out.append(s)
    return out


def _chain_icon_urls(chain_id: int, name: str | None = None) -> list[str]:
    """Ordered list of upstream URLs to try for a chain logo.
    Curve first — their set covers TAC and matches the wallet's
    visual style (Curve also supplies our token-list source).
    TrustWallet second as a long-established backup.

    ``name`` (the chain's display name, when known) lets a chain that
    isn't in the hardcoded id→slug map still resolve a Curve logo by
    deriving the slug from its name — that's how dapp-added chains
    (Fraxtal, Hyperliquid, …) get an icon without a code change."""
    urls: list[str] = []
    curve_slugs: list[str] = []
    mapped = _CURVE_CHAIN_SLUGS.get(int(chain_id))
    if mapped:
        curve_slugs.append(mapped)
    if name:
        for s in _name_slug_candidates(name):
            if s not in curve_slugs:
                curve_slugs.append(s)
    for slug in curve_slugs:
        urls.append(
            "https://raw.githubusercontent.com/curvefi/curve-assets/main/"
            f"chains/{slug}.png"
        )
    tw_slug = _TRUSTWALLET_CHAIN_SLUGS.get(int(chain_id))
    if tw_slug:
        urls.append(
            "https://raw.githubusercontent.com/trustwallet/assets/master/"
            f"blockchains/{tw_slug}/info/logo.png"
        )
    return urls


CIRCULAR_RENDER_SIZE = 64   # Rendered once, scaled down by Qt for display.


_VIEW_ICON_SIZES = (16, 18, 20, 24, 32, 36, 40, 48)


def smooth_icon(src: QPixmap) -> QIcon:
    """Build a QIcon that holds ``src`` pre-scaled (bilinear) to every size the
    UI actually displays at — 16/18/20 px and their 2× HiDPI variants.

    Why not just ``QIcon(src)``: an icon built from a single 64 px pixmap is
    downscaled by Qt's icon engine at paint time, and some Qt builds do that
    with nearest-neighbour — blocky logos on low-res screens. By baking the
    real display sizes ourselves with an explicit smooth filter, the engine
    finds an exact-size match and blits it 1:1, so it never rescales (nearest
    or otherwise). Falls back to the source for any larger request.
    """
    icon = QIcon()
    if src.isNull():
        return icon
    for s in _VIEW_ICON_SIZES:
        if s < src.width():
            icon.addPixmap(src.scaled(
                s, s,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
    icon.addPixmap(src)   # largest available, for requests beyond the list
    return icon


def smooth_scaled(src: QPixmap, size: int, dpr: float = 1.0) -> QPixmap:
    """Scale ``src`` to ``size``×``size`` *logical* px with a smooth (bilinear)
    filter, tagged at ``dpr`` so it stays crisp on HiDPI.

    Use this for any on-screen icon instead of ``QLabel.setScaledContents``
    (which scales with nearest-neighbour) or a bare ``QPixmap.scaled`` (whose
    default ``TransformationMode`` is also nearest). Keeps aspect ratio.
    """
    if src.isNull() or size <= 0:
        return src
    dev = max(1, round(size * dpr))
    out = src.scaled(
        dev, dev,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    out.setDevicePixelRatio(dpr)
    return out


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


def _draw_white_arrow(
    painter: QPainter, cx: float, cy: float, r: float, up: bool,
) -> None:
    """A white arrow (vertical shaft + chevron head) centred at (cx, cy),
    pointing up for sent / down for received, sized to radius ``r``.

    Drawn as *vector* rather than pulling the desktop theme's ``go-up`` /
    ``go-down`` glyph: that glyph is absent on minimal icon themes and portable
    runtimes, where QIcon.fromTheme returned null and the badge silently
    degraded to a bare triangle. A vector arrow renders identically on every
    machine."""
    ay = r * 0.6          # half the arrow's vertical extent
    hw = r * 0.46         # chevron half-width
    hl = r * 0.62         # chevron length along the shaft
    pen = QPen(QColor(255, 255, 255), max(1.0, r * 0.3))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if up:
        tip_y, tail_y, head_y = cy - ay, cy + ay, cy - ay + hl
    else:
        tip_y, tail_y, head_y = cy + ay, cy - ay, cy + ay - hl
    path = QPainterPath()
    path.moveTo(cx, tail_y)            # shaft
    path.lineTo(cx, tip_y)
    path.moveTo(cx - hw, head_y)       # chevron head, joined at the tip
    path.lineTo(cx, tip_y)
    path.lineTo(cx + hw, head_y)
    painter.drawPath(path)


def _draw_direction_badge(
    painter: QPainter, cx: float, cy: float, r: float, outgoing: bool,
) -> None:
    """A coloured disc (blue = sent, green = received) with a white arrow,
    centred at (cx, cy), radius ``r``; a thin white ring separates it from
    whatever's behind. The arrow is drawn as vector (see
    :func:`_draw_white_arrow`) so it renders identically on every machine,
    including minimal icon themes / portable runtimes that ship no
    ``go-up`` / ``go-down``."""
    painter.setPen(QPen(QColor(255, 255, 255), max(1.0, r * 0.12)))
    painter.setBrush(_SENT_COLOR if outgoing else _RECEIVED_COLOR)
    painter.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    _draw_white_arrow(painter, cx, cy, r, up=outgoing)


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


# Vault badge: a "magical sparkle" — a bare four-point star (no disc), gold-
# yellow with a dark outline. The bright fill carries it on dark backgrounds,
# the dark outline gives it definition on light ones. Points are unit
# coordinates (outer tips at N/E/S/W radius 1, inner points on the diagonals)
# scaled at draw time.
_VAULT_BADGE_COLOR = QColor(255, 209, 59)      # gold-yellow fill
_VAULT_BADGE_OUTLINE = QColor(122, 74, 10)     # brown border (defines the star)
_SPARKLE_POINTS = (
    (0.0, -1.0), (0.24, -0.24), (1.0, 0.0), (0.24, 0.24),
    (0.0, 1.0), (-0.24, 0.24), (-1.0, 0.0), (-0.24, -0.24),
)


def _draw_sparkle_badge(
    painter: QPainter, cx: float, cy: float, r: float,
) -> None:
    """A four-point sparkle — gold-yellow fill, dark outline, no disc — centred
    at (cx, cy) with outer radius ``r``. Vector-drawn (like the direction
    badge) so it renders identically everywhere."""
    path = QPainterPath()
    x0, y0 = _SPARKLE_POINTS[0]
    path.moveTo(cx + x0 * r, cy + y0 * r)
    for px, py in _SPARKLE_POINTS[1:]:
        path.lineTo(cx + px * r, cy + py * r)
    path.closeSubpath()
    pen = QPen(_VAULT_BADGE_OUTLINE, max(1.0, r * 0.16))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(_VAULT_BADGE_COLOR)
    painter.drawPath(path)


def vault_icon(base: "QPixmap | None", size: int = 64) -> QPixmap:
    """The underlying asset's icon (circular) with a sparkle badge in the
    lower-right corner — marks a token as a vault whose value derives from that
    underlying (yb-WBTC → the WBTC icon + a sparkle). ``base`` is the
    underlying's pixmap; when it's missing the sparkle fills the icon centred,
    so a vault is always visually flagged. Returns a QPixmap (pass through
    ``smooth_icon`` for the table)."""
    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    if base is not None and not base.isNull():
        p.drawPixmap(0, 0, to_circular(base, size))
        r = size * 0.36                       # a bigger sparkle than the old disc
        # Push the centre toward the bottom-right corner so the sparkle sits in
        # the corner (partly off the round icon) rather than over its middle.
        off = size - r * 0.8
        _draw_sparkle_badge(p, off, off, r)
    else:
        _draw_sparkle_badge(p, size / 2.0, size / 2.0, size * 0.44)
    p.end()
    return canvas


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
                data = _safe_icon_fetch(url)
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

    def request(self, chain_id: int, name: str | None = None) -> None:
        cid = int(chain_id)
        if cid in self._mem:
            return
        urls = _chain_icon_urls(cid, name)
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
            atomic_write_bytes(
                CHAIN_ICONS_DIR / f"{int(chain_id)}.png", bytes(data))
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
            data = _safe_icon_fetch(self.url)
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
        try:
            # Serialize the re-encoded PNG to bytes so it lands owner-only
            # (0600) and atomically — a token icon's filename is its contract
            # address, so the file is privacy-bearing (issue #3). raw.save()
            # would write at the umask (~0644).
            buf = QBuffer()
            buf.open(QBuffer.OpenModeFlag.WriteOnly)
            if raw.save(buf, "PNG"):
                atomic_write_bytes(path, bytes(buf.data().data()))
        except Exception as e:
            log.debug("icon save failed: %s — %s", path, e)
        self.icon_ready.emit(chain_id, contract.lower())
