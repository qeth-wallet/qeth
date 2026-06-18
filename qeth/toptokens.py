"""Top-tokens-by-market-cap lists for the balance multicall sweep.

Token-discovery indexers (Blockscout — even Etherscan) can have gaps: a
wallet's real USDC simply absent from the indexed token list, so it never
shows until the user adds it by hand. To make held *majors* always
surface, qeth multicalls ``balanceOf`` over a bounded set of the top
tokens by market cap, unioned with the indexer's per-holder list (which
still covers the long tail). The top is short — most top-mcap coins are
L1s (BTC/SOL) or live on other chains, so even "top 1000 by mcap"
collapses to a few hundred contracts per chain — well under the ~5k
curated-list load that once aborted the balance QThread.

The list is *seeded* from a snapshot shipped with qeth
(``qeth/data/top_tokens.json``) and *refreshed* from CoinGecko into
``~/.qeth/toptokens/`` on a TTL. So the runtime hot path stays keyless
and offline (pure multicall), touching CoinGecko only on the occasional
background refresh. The same ``fetch_top_tokens`` builds both the bundled
seed (via ``scripts/gen_top_tokens.py``) and the runtime cache.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from . import USER_AGENT
from .fsatomic import atomic_write_text

log = logging.getLogger("qeth.toptokens")

# chain_id -> CoinGecko platform slug (the keys in coins/list `platforms`).
COINGECKO_PLATFORMS: dict[int, str] = {
    1:     "ethereum",
    10:    "optimistic-ethereum",
    137:   "polygon-pos",
    42161: "arbitrum-one",
    8453:  "base",
    100:   "xdai",
    56:    "binance-smart-chain",
}

_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
_LIST_URL = "https://api.coingecko.com/api/v3/coins/list?include_platform=true"
_DEFAULT_TTL_S = 7 * 86400.0  # weekly; the top of the mcap table barely churns

FetchFn = Callable[[list[int], int, float], "dict[int, list[TopToken]]"]


@dataclass(frozen=True)
class TopToken:
    address: str   # lowercased token contract
    symbol: str


def _get_json(url: str, timeout: float) -> object:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def fetch_top_tokens(
    chain_ids: list[int], top_n: int = 1000, timeout: float = 30.0,
) -> dict[int, list[TopToken]]:
    """Build per-chain top-by-market-cap token lists from CoinGecko.

    Pulls the market-cap ranking (``coins/markets``, paged at 250) and
    the contract map (``coins/list?include_platform``), then for each
    chain collects the contracts of the highest-ranked coins present on
    it, in rank order. Keyless; a handful of requests. Raises on a failed
    fetch — the runtime refresh catches and keeps the prior list; the
    generator script lets it propagate so a bad regen is loud."""
    slugs = {cid: COINGECKO_PLATFORMS[cid]
             for cid in chain_ids if cid in COINGECKO_PLATFORMS}

    # 1) ranked coin ids (market_cap_desc). CoinGecko pages markets at
    #    <=250/page; walk pages until we have top_n or run dry.
    ranked: list[str] = []
    per_page = 250
    page = 1
    while len(ranked) < top_n:
        q = urllib.parse.urlencode({
            "vs_currency": "usd", "order": "market_cap_desc",
            "per_page": per_page, "page": page})
        batch = _get_json(f"{_MARKETS_URL}?{q}", timeout)
        if not isinstance(batch, list) or not batch:
            break
        for coin in batch:
            if not isinstance(coin, dict):
                continue
            c = cast(dict[str, object], coin)
            coin_id = c.get("id")
            if coin_id:
                ranked.append(str(coin_id))
        if len(batch) < per_page:
            break
        page += 1
    ranked = ranked[:top_n]

    # 2) id -> {slug: contract}, id -> symbol
    listing = _get_json(_LIST_URL, timeout)
    platforms: dict[str, dict[str, object]] = {}
    symbols: dict[str, str] = {}
    listing_items = listing if isinstance(listing, list) else []
    for coin in listing_items:
        if not isinstance(coin, dict):
            continue
        c = cast(dict[str, object], coin)
        coin_id = c.get("id")
        if not coin_id:
            continue
        platforms_raw = c.get("platforms")
        platforms[str(coin_id)] = (
            cast(dict[str, object], platforms_raw)
            if isinstance(platforms_raw, dict) else {}
        )
        symbols[str(coin_id)] = str(c.get("symbol") or "").upper()

    # 3) per chain, in rank order
    out: dict[int, list[TopToken]] = {cid: [] for cid in slugs}
    for coin_id in ranked:
        plats = platforms.get(coin_id) or {}
        sym = symbols.get(coin_id, "")
        for cid, slug in slugs.items():
            addr = plats.get(slug)
            if isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42:
                out[cid].append(TopToken(address=addr.lower(), symbol=sym))
    return out


def build_payload(
    fetched: dict[int, list[TopToken]], fetched_at: float, top_n: int,
) -> dict:
    """Serialize fetched lists to the on-disk JSON shape shared by the
    bundled seed (gen_top_tokens.py) and the runtime cache (refresh)."""
    return {
        "fetched_at": fetched_at,
        "source": f"coingecko markets top-{top_n} by market cap",
        "chains": {
            str(cid): [{"address": t.address, "symbol": t.symbol} for t in toks]
            for cid, toks in fetched.items()
        },
    }


class TopTokens:
    """Per-chain top-token contract lists for the balance sweep, seeded
    from the bundled snapshot and refreshed from CoinGecko on a TTL.

    ``contracts(chain_id)`` always returns the best list available
    (cache if present, else the shipped seed) regardless of age — a
    slightly stale top list still catches the majors. ``is_stale()``
    only governs whether a background ``refresh()`` is worthwhile."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        seed_path: Path | None = None,
        ttl_seconds: float = _DEFAULT_TTL_S,
        clock: Callable[[], float] = time.time,
        fetch: FetchFn = fetch_top_tokens,
    ):
        self._cache_dir = cache_dir or (Path.home() / ".qeth" / "toptokens")
        self._seed_path = seed_path or (
            Path(__file__).resolve().parent / "data" / "top_tokens.json")
        self._ttl = ttl_seconds
        self._clock = clock
        self._fetch = fetch
        self._by_chain: dict[int, list[str]] = {}
        self._fetched_at: float = 0.0
        self._load()

    def _cache_path(self) -> Path:
        return self._cache_dir / "top_tokens.json"

    def _load(self) -> None:
        # Prefer the runtime cache; fall back to the bundled seed.
        for path in (self._cache_path(), self._seed_path):
            parsed = self._read(path)
            if parsed is not None:
                self._by_chain, self._fetched_at = parsed
                return

    @staticmethod
    def _read(path: Path) -> tuple[dict[int, list[str]], float] | None:
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("chains"), dict):
            return None
        by_chain: dict[int, list[str]] = {}
        for cid_s, toks in raw["chains"].items():
            try:
                cid = int(cid_s)
            except (TypeError, ValueError):
                continue
            addrs: list[str] = []
            for t in toks or []:
                a = t.get("address") if isinstance(t, dict) else None
                if isinstance(a, str) and a.startswith("0x"):
                    addrs.append(a.lower())
            by_chain[cid] = addrs
        fetched_at = raw.get("fetched_at") or 0.0
        return by_chain, float(fetched_at)

    def contracts(self, chain_id: int) -> list[str]:
        """Top-by-market-cap token contracts for the chain (lowercased)."""
        return self._by_chain.get(chain_id, [])

    def is_stale(self) -> bool:
        return (self._clock() - self._fetched_at) > self._ttl

    def refresh(self, chain_ids: list[int], top_n: int = 1000) -> bool:
        """Fetch a fresh list and write the cache. Best-effort: on any
        fetch/write failure it keeps the current list and returns False,
        so a flaky CoinGecko never breaks discovery — the seed/old cache
        still serves ``contracts()``."""
        try:
            fetched = self._fetch(chain_ids, top_n, 30.0)
        except (urllib.error.URLError, OSError, ValueError,
                KeyError, TypeError) as e:
            log.warning("top-tokens refresh failed: %s", e)
            return False
        now = self._clock()
        payload = build_payload(fetched, now, top_n)
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._cache_path(), json.dumps(payload, indent=1))
        except OSError as e:
            log.warning("top-tokens cache write failed: %s", e)  # in-memory still updates
        self._by_chain = {cid: [t.address.lower() for t in toks]
                          for cid, toks in fetched.items()}
        self._fetched_at = now
        return True
