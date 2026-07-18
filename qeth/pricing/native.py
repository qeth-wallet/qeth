"""Native-asset (gas coin) CoinGecko id resolution.

A chain added via the picker keeps ``Chain``'s unsafe ``coingecko_id =
"ethereum"`` default, so without this a non-ETH native (AVAX, BNB, …) would
be valued at ETH's price. Discovery (CoinGecko asset_platforms, all chains)
first, then the offline symbol map, then the chain's own id — but never the
unsafe "ethereum" default on a non-ETH chain.
"""

import json
import logging
import time
import urllib.request
from pathlib import Path

from .. import USER_AGENT
from ..fsatomic import atomic_write_bytes

log = logging.getLogger("qeth.pricing.native")

# Offline fallback: native-asset symbol → CoinGecko id, for the rare run
# where discovery (CoinGecko asset_platforms) hasn't loaded yet or is
# unreachable.
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
