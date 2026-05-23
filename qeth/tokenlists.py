"""Curated ERC-20 token lists from multiple sources, merged and cached.

Each source is fetched independently; if one fails (network, 404, parse
error), it's logged and skipped — the others still populate the index.
Each source caches its JSON on disk with a TTL, and a stale cache is
preferred to no data when the network is down.

Usage::

    lists = TokenLists()
    lists.load()                                     # blocking; tolerant of failures
    lists.is_known(chain_id, "0x...") -> bool
    lists.get(chain_id, "0x...") -> TokenListEntry | None
"""

import json
import logging
import threading
import time
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

log = logging.getLogger("qeth.tokenlists")

CACHE_DIR = Path.home() / ".qeth" / "tokenlists"
DEFAULT_TIMEOUT = 8.0
DEFAULT_TTL_SECONDS = 24 * 3600
USER_AGENT = "qeth/0.1"


@dataclass(frozen=True)
class TokenListEntry:
    chain_id: int
    address: str        # lower-case
    symbol: str
    name: str
    decimals: int
    source: str         # name of the source that first contributed this entry
    logo_uri: str | None = None


# ---------------------------------------------------------------------------
# Fetch helper with stale-while-revalidate caching
# ---------------------------------------------------------------------------

def _fetch_json(url: str, cache_path: Path, ttl: float, timeout: float):
    """HTTP GET → parsed JSON, with on-disk caching.

    Order of preference: fresh cache (younger than ``ttl``) > live fetch >
    stale cache (older than ``ttl``). Returns ``None`` when every path
    fails so the caller can carry on with what other sources provide.
    """
    if cache_path.exists() and (time.time() - cache_path.stat().st_mtime < ttl):
        try:
            return json.loads(cache_path.read_bytes())
        except json.JSONDecodeError:
            pass  # corrupted; fall through to refetch

    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        data = json.loads(body)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(body)
        return data
    except Exception as e:
        log.warning("fetch %s failed: %s", url, e)

    if cache_path.exists():
        try:
            log.info("using stale cache for %s", url)
            return json.loads(cache_path.read_bytes())
        except json.JSONDecodeError:
            return None
    return None


def _from_tokenlists_schema(data: dict, source_name: str) -> Iterable[TokenListEntry]:
    """Iterate entries from the tokenlists.org JSON schema."""
    for t in data.get("tokens", []):
        try:
            addr = str(t.get("address", "")).lower()
            cid = int(t.get("chainId") or 0)
            if not addr.startswith("0x") or len(addr) != 42 or cid == 0:
                continue
            yield TokenListEntry(
                chain_id=cid,
                address=addr,
                symbol=str(t.get("symbol") or "?"),
                name=str(t.get("name") or ""),
                decimals=int(t.get("decimals") or 18),
                source=source_name,
                logo_uri=t.get("logoURI") or None,
            )
        except (TypeError, ValueError):
            continue


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

class TokenListSource(ABC):
    name: str

    @abstractmethod
    def fetch_entries(
        self, cache_dir: Path, ttl: float, timeout: float
    ) -> Iterable[TokenListEntry]:
        ...


class UniswapDefault(TokenListSource):
    """Single multichain file. Largest cross-chain breadth in one request."""

    name = "uniswap"
    URL = "https://tokens.uniswap.org/"

    def fetch_entries(self, cache_dir, ttl, timeout):
        data = _fetch_json(self.URL, cache_dir / "uniswap.json", ttl, timeout)
        if data:
            yield from _from_tokenlists_schema(data, self.name)


class CoinGeckoPerChain(TokenListSource):
    """CoinGecko's per-network lists; broadest mainnet coverage."""

    name = "coingecko"
    # chain_id → CoinGecko platform slug (used in their URL path)
    SLUGS: dict[int, str] = {
        1:     "uniswap",
        10:    "optimistic-ethereum",
        137:   "polygon-pos",
        42161: "arbitrum-one",
        8453:  "base",
    }

    def fetch_entries(self, cache_dir, ttl, timeout):
        for cid, slug in self.SLUGS.items():
            url = f"https://tokens.coingecko.com/{slug}/all.json"
            data = _fetch_json(url, cache_dir / f"coingecko-{cid}.json", ttl, timeout)
            if data:
                yield from _from_tokenlists_schema(data, self.name)


class Curve(TokenListSource):
    """Curve API ``/v1/getTokens/all/{chain_slug}``.

    Important for surfacing Curve LP tokens, gauge tokens, and assets in
    Curve pools — these are often missing from the generic lists.
    Response shape: ``{"data": {"tokens": [{"address","symbol","name","decimals"}, ...]}}``;
    chainId is implicit in the URL slug.
    """

    name = "curve"
    # chain_id → Curve blockchainId slug
    SLUGS: dict[int, str] = {
        1:     "ethereum",
        10:    "optimism",
        137:   "polygon",
        42161: "arbitrum",
        8453:  "base",
    }

    def fetch_entries(self, cache_dir, ttl, timeout):
        for cid, slug in self.SLUGS.items():
            url = f"https://api.curve.finance/v1/getTokens/all/{slug}"
            data = _fetch_json(url, cache_dir / f"curve-{cid}.json", ttl, timeout)
            if not isinstance(data, dict):
                continue
            tokens = ((data.get("data") or {}).get("tokens")) or []
            for t in tokens:
                try:
                    addr = str(t.get("address", "")).lower()
                    if not addr.startswith("0x") or len(addr) != 42:
                        continue
                    # Curve doesn't return logoURI; their curve-assets repo
                    # serves icons by lower-case address (verified for
                    # mainnet; per-chain folders don't follow a slug pattern,
                    # so this 404s gracefully on non-mainnet entries).
                    logo = (
                        "https://raw.githubusercontent.com/curvefi/curve-assets"
                        f"/main/images/assets/{addr}.png"
                    )
                    yield TokenListEntry(
                        chain_id=cid,
                        address=addr,
                        symbol=str(t.get("symbol") or "?"),
                        name=str(t.get("name") or ""),
                        decimals=int(t.get("decimals") or 18),
                        source=self.name,
                        logo_uri=logo,
                    )
                except (TypeError, ValueError):
                    continue


class OneInch(TokenListSource):
    """1inch publishes per-chain dicts at tokens.1inch.io/v1.2/<chainId>.

    Schema differs from tokenlists.org: ``{address: {symbol, name, ...}}``
    keyed by address with no chainId field (it's implicit in the URL).
    """

    name = "1inch"
    CHAINS: list[int] = [1, 10, 137, 42161, 8453]

    def fetch_entries(self, cache_dir, ttl, timeout):
        for cid in self.CHAINS:
            url = f"https://tokens.1inch.io/v1.2/{cid}"
            data = _fetch_json(url, cache_dir / f"1inch-{cid}.json", ttl, timeout)
            if not isinstance(data, dict):
                continue
            for addr, info in data.items():
                try:
                    addr = str(addr).lower()
                    if not addr.startswith("0x") or len(addr) != 42 or not isinstance(info, dict):
                        continue
                    yield TokenListEntry(
                        chain_id=cid,
                        address=addr,
                        symbol=str(info.get("symbol") or "?"),
                        name=str(info.get("name") or ""),
                        decimals=int(info.get("decimals") or 18),
                        source=self.name,
                        logo_uri=info.get("logoURI") or None,
                    )
                except (TypeError, ValueError):
                    continue


# Source ordering implies trust ordering: first to claim a (chain, address)
# wins. Uniswap's curated list is most conservative, then CoinGecko, then
# 1inch.
DEFAULT_SOURCES: list[TokenListSource] = [
    UniswapDefault(),
    CoinGeckoPerChain(),
    Curve(),
    OneInch(),
]


# ---------------------------------------------------------------------------
# Merged index
# ---------------------------------------------------------------------------

class TokenLists:
    """Loads multiple TokenListSource implementations and merges them.

    Thread-safe for reads. ``load()`` rebuilds the index; per-source
    failures are caught and logged so a single bad source doesn't take
    everything down.
    """

    def __init__(
        self,
        sources: list[TokenListSource] | None = None,
        cache_dir: Path = CACHE_DIR,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.sources = sources if sources is not None else list(DEFAULT_SOURCES)
        self.cache_dir = cache_dir
        self.ttl_seconds = ttl_seconds
        self.timeout = timeout
        self._lock = threading.RLock()
        self._index: dict[tuple[int, str], TokenListEntry] = {}
        self._loaded = False

    def load(self) -> None:
        with self._lock:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            new_index: dict[tuple[int, str], TokenListEntry] = {}
            for src in self.sources:
                start = time.time()
                added = 0
                try:
                    for entry in src.fetch_entries(
                        self.cache_dir, self.ttl_seconds, self.timeout
                    ):
                        key = (entry.chain_id, entry.address)
                        if key not in new_index:
                            new_index[key] = entry
                            added += 1
                except Exception as e:
                    log.warning("source %s failed entirely: %s", src.name, e)
                    continue
                log.info(
                    "source %s: %d new entries in %.2fs",
                    src.name, added, time.time() - start,
                )
            self._index = new_index
            self._loaded = True

    def is_known(self, chain_id: int, address: str) -> bool:
        return (chain_id, address.lower()) in self._index

    def get(self, chain_id: int, address: str) -> TokenListEntry | None:
        return self._index.get((chain_id, address.lower()))

    def count(self, chain_id: int | None = None) -> int:
        if chain_id is None:
            return len(self._index)
        return sum(1 for (cid, _) in self._index if cid == chain_id)

    @property
    def loaded(self) -> bool:
        return self._loaded
