"""USD price discovery for native + ERC-20 assets.

``PriceSource`` is the abstract base; ``DefiLlamaPrices`` is the only
implementation today — it's free, no API key, multichain, and accepts
large batches in a single HTTP request, which makes it the right
default for the wallet's token-panel use case.

Result shape: ``{ key: Price }`` where ``key`` is the lower-case ERC-20
address, or the empty string ``""`` for the native asset (matching
``TokenListPanel.NATIVE_CONTRACT``).
"""

import json
import logging
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from collections.abc import Iterable

from . import USER_AGENT
from .chains import Chain
from .fsatomic import atomic_write_bytes

log = logging.getLogger("qeth.prices")

DEFAULT_TIMEOUT = 8.0
# DefiLlama's CDN 404s request URLs beyond ~5 KB (and 414s past ~10 KB), so we
# batch by URL LENGTH, not by a fixed key count. The old fixed 100-key batch
# built ~5.2 KB URLs and every request 404'd — which, since an unpriced token is
# dropped from the panel, silently emptied the whole token list. Keep each URL
# well under the observed threshold.
PRICES_URL = "https://coins.llama.fi/prices/current/"
MAX_URL_LEN = 3000

# chain_id -> DefiLlama chain slug used in coin keys like "ethereum:0x...".
# When a chain is missing here, fetch() silently skips all ERC-20
# price requests for it — the tokens panel then drops every priced
# row at the "price is None" filter, so the wallet looks empty
# even when discovery itself works. Keep this map in step with
# DEFAULT_CHAINS additions.
DEFILLAMA_CHAIN_SLUGS: dict[int, str] = {
    1:     "ethereum",
    10:    "optimism",
    56:    "bsc",
    100:   "xdai",
    137:   "polygon",
    8453:  "base",
    42161: "arbitrum",
    43114: "avax",
}


# Offline fallback: native-asset symbol → CoinGecko id, for the rare run
# where the discovery below (CoinGecko asset_platforms) hasn't loaded yet
# or is unreachable. A chain added via the picker keeps Chain's unsafe
# ``coingecko_id = "ethereum"`` default, so without this a non-ETH native
# (AVAX, BNB, …) would be valued at ETH's price.
NATIVE_COINGECKO_IDS: dict[str, str] = {
    "ETH":   "ethereum",
    "WETH":  "ethereum",
    "AVAX":  "avalanche-2",
    "BNB":   "binancecoin",
    "POL":   "polygon-ecosystem-token",
    "MATIC": "polygon-ecosystem-token",   # MATIC rebranded to POL
    "XDAI":  "xdai",
    "S":     "sonic-3",
    "FTM":   "fantom",
}

# Discovered map (chain_id → native CoinGecko id) from CoinGecko's
# asset_platforms list — covers *every* chain CoinGecko knows (261+), so
# picker-added chains like TAC get a correct native price without a
# hand-maintained entry. Populated by load_native_coin_ids(); None until
# first load.
_DISCOVERED_NATIVE_IDS: dict[int, str] | None = None
_ASSET_PLATFORMS_URL = "https://api.coingecko.com/api/v3/asset_platforms"
_NATIVE_IDS_CACHE_DIR = Path.home() / ".qeth" / "coingecko"
_NATIVE_IDS_TTL = 7 * 24 * 3600.0


def load_native_coin_ids(*, cache_dir: Path | None = None,
                         ttl: float = _NATIVE_IDS_TTL,
                         timeout: float = 10.0,
                         force: bool = False) -> dict[int, str]:
    """Discover chain_id → native CoinGecko id from CoinGecko's
    asset_platforms list, disk-cached ~7 days (mirrors chainlist). Same
    idea as discovering chain icons: rather than hand-maintain a map, ask
    the upstream that already knows. Falls back to the cached/empty map on
    network failure — never raises."""
    global _DISCOVERED_NATIVE_IDS
    cache = cache_dir if cache_dir is not None else _NATIVE_IDS_CACHE_DIR
    cache_file = cache / "asset_platforms.json"
    fresh = (cache_file.exists()
             and (time.time() - cache_file.stat().st_mtime) < ttl)
    if not fresh or force:
        try:
            req = urllib.request.Request(
                _ASSET_PLATFORMS_URL,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            atomic_write_bytes(cache_file, raw)
        except Exception as e:
            log.warning("asset_platforms fetch failed: %s", e)
            if not cache_file.exists():
                return _DISCOVERED_NATIVE_IDS or {}
    try:
        data = json.loads(cache_file.read_text())
    except Exception:
        return _DISCOVERED_NATIVE_IDS or {}
    out: dict[int, str] = {}
    for p in data if isinstance(data, list) else []:
        cid = p.get("chain_identifier")
        nid = p.get("native_coin_id")
        if isinstance(cid, int) and isinstance(nid, str) and nid:
            out[cid] = nid
    _DISCOVERED_NATIVE_IDS = out
    return out


def native_coingecko_id(chain) -> str | None:
    """CoinGecko id for a chain's *native* asset. Discovery first
    (CoinGecko asset_platforms, all chains), then the offline symbol map,
    then the chain's own id — but never the unsafe "ethereum" default on a
    non-ETH chain (better no native value than a wildly wrong one)."""
    cid = getattr(chain, "chain_id", None)
    if _DISCOVERED_NATIVE_IDS and cid is not None:
        nid = _DISCOVERED_NATIVE_IDS.get(cid)
        if nid:
            return nid
    sym = (getattr(chain, "symbol", "") or "").upper()
    mapped = NATIVE_COINGECKO_IDS.get(sym)
    if mapped:
        return mapped
    cid = getattr(chain, "coingecko_id", "") or ""
    if cid == "ethereum" and sym != "ETH":
        return None
    return cid or None


@dataclass(frozen=True)
class Price:
    price_usd: Decimal
    timestamp: int        # unix seconds — how fresh the quote is
    source: str
    confidence: float = 1.0


class PriceSourceError(Exception):
    pass


class PriceSource(ABC):
    name: str

    @abstractmethod
    def fetch(
        self,
        chain: Chain,
        contracts: Iterable[str],
        include_native: bool = False,
    ) -> dict[str, Price]:
        ...


def _price_url(keys: list[str]) -> str:
    return PRICES_URL + urllib.parse.quote(",".join(keys), safe=":,")


def _url_batches(keys: list[str], max_url_len: int = MAX_URL_LEN) -> Iterable[list[str]]:
    """Group ``keys`` so each request URL stays under ``max_url_len`` — bounding
    by URL length (not key count), since that is what DefiLlama's CDN rejects."""
    chunk: list[str] = []
    for k in keys:
        if chunk and len(_price_url([*chunk, k])) > max_url_len:
            yield chunk
            chunk = []
        chunk.append(k)
    if chunk:
        yield chunk


class DefiLlamaPrices(PriceSource):
    """https://coins.llama.fi/prices/current/<key,key,...>"""

    name = "defillama"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout

    def fetch(self, chain, contracts, include_native=False):
        slug = DEFILLAMA_CHAIN_SLUGS.get(chain.chain_id)
        keys: list[str] = []
        if include_native:
            load_native_coin_ids()   # discover (disk-cached ~7d) the native id
            native_id = native_coingecko_id(chain)
            if native_id:
                keys.append(f"coingecko:{native_id}")
        if slug:
            for c in contracts:
                c = c.lower()
                if c.startswith("0x") and len(c) == 42:
                    keys.append(f"{slug}:{c}")
        if not keys:
            return {}

        out: dict[str, Price] = {}
        for chunk in _url_batches(keys):
            url = _price_url(chunk)
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
            except Exception as e:
                log.warning("defillama batch failed: %s", e)
                continue
            for k, info in (data.get("coins") or {}).items():
                try:
                    price = Decimal(str(info["price"]))
                except (KeyError, ValueError, InvalidOperation, TypeError):
                    continue
                ts = int(info.get("timestamp") or 0)
                conf = float(info.get("confidence") or 1.0)
                if k.startswith("coingecko:"):
                    out[""] = Price(price, ts, self.name, conf)
                elif ":" in k:
                    _, addr = k.split(":", 1)
                    out[addr.lower()] = Price(price, ts, self.name, conf)
        return out
