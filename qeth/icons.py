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

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QPixmap

log = logging.getLogger("qeth.icons")

ICONS_DIR = Path.home() / ".qeth" / "icons"
BUNDLED_NATIVE_DIR = Path(__file__).parent / "assets" / "native"
USER_AGENT = "qeth/0.1"
FETCH_TIMEOUT = 10.0


def bundled_native_icon(symbol: str) -> QPixmap | None:
    """Return the bundled native-asset icon for a chain symbol (ETH, MATIC,
    …), or None if no file ships for that symbol."""
    p = BUNDLED_NATIVE_DIR / f"{symbol.upper()}.png"
    if not p.exists():
        return None
    pix = QPixmap(str(p))
    return pix if not pix.isNull() else None


class _IconFetchWorker(QThread):
    fetched = Signal(int, str, object)  # (chain_id, contract_lower, bytes|None)

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

    icon_ready = Signal(int, str)  # (chain_id, contract_lower)

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
                pix = QPixmap(str(p))
                if not pix.isNull():
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
        pix = QPixmap()
        if not pix.loadFromData(bytes(data)):
            return  # unsupported format (e.g. SVG) — could add QSvgRenderer later
        self._mem[key] = pix
        # Persist using the URL's extension so re-loads work.
        # (We don't know the original URL here; saving as .png is safe since
        #  QPixmap can re-encode.)
        path = self._dir(chain_id) / f"{contract.lower()}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            pix.save(str(path), "PNG")
        except Exception as e:
            log.debug("icon save failed: %s — %s", path, e)
        self.icon_ready.emit(chain_id, contract.lower())
