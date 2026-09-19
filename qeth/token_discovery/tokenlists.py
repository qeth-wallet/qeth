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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar
from collections.abc import Iterable

from .. import USER_AGENT
from ..fsatomic import atomic_write_bytes

log = logging.getLogger("qeth.token_discovery.tokenlists")

CACHE_DIR = Path.home() / ".qeth" / "tokenlists"
DEFAULT_TIMEOUT = 8.0
DEFAULT_TTL_SECONDS = 24 * 3600

# Heuristic spam-filter inputs. Kept here rather than in the UI module
# because deciding "is this token suspicious" is a data concern that
# leans on the curated whitelist (TokenLists.is_known) for its
# strongest signal.
_SCAM_URL_NEEDLES = (
    "http://", "https://", "www.", ".com", ".io", ".gg", ".xyz",
    ".org", ".net", ".finance", ".app", "t.me/", "t.ly/", "bit.ly/",
)
_SCAM_KEYWORDS = (
    "claim", "visit", "airdrop", "reward", "free $",
    "redeem", "voucher", "winner",
)
_CANONICAL_SYMBOLS = {
    "eth", "usdc", "usdt", "dai", "wbtc", "weth", "wmatic", "matic",
    "bnb", "avax", "frax", "crv", "uni", "aave",
}

# Legitimate non-transferable governance locks (vote-escrow / vote-locked):
# you lock a token for voting weight and can't move the receipt. GoPlus's
# simulation can't tell that from a real trap and false-flags them
# INCONSISTENTLY — vlSDT reads is_honeypot, vlAURA is_blacklisted, vlCVX is
# clean (all the same Convex-style lock), and batch vs single fetches disagree
# — which is why a deterministic allowlist beats reinterpreting GoPlus. Keyed
# (chain_id, lower-address). EVERY entry was identity-verified on-chain
# (name()/symbol()) first; extend the same way — never add an unverified
# address to a scam bypass. Scoped to the scam check only (not is_known), so
# it can't change visibility or pricing.
_TRUSTED_LOCKS: frozenset[tuple[int, str]] = frozenset(
    (cid, addr.lower()) for cid, addr in (
        # --- Ethereum mainnet ---
        (1, "0x72a19342e8F1838460eBFCCEf09F6585e32db86E"),  # vlCVX  — Convex
        (1, "0x3Fa73f1E5d8A792C80F426fc8F84FBF7Ce9bBCAC"),  # vlAURA — Aura
        (1, "0x94818A7baa7e9F5dC62ce4da1B52ef9a760b80B8"),  # vlSDT  — Stake DAO
        (1, "0x5f3b5DfEb7B28CDbD7FAba78963EE202a494e2A2"),  # veCRV  — Curve
        (1, "0xC128a9954e6c874eA3d62ce62B468bA073093F25"),  # veBAL  — Balancer
        (1, "0xc8418aF6358FFddA74e09Ca9CC3Fe03Ca6aDC5b0"),  # veFXS  — Frax
        (1, "0x90c1f9220d90d3966FbeE24045EDd73E1d588aD5"),  # veYFI  — Yearn
        (1, "0x0C462Dbb9EC8cD1630f1728B2CFD2769d09f0dd5"),  # veANGLE — Angle
        (1, "0x0c93675719ad43648a1ab5f735dcaaa08e130be4"),  # veMAV  — Maverick
        (1, "0xbF82a3212e13b2d407D10f5107b5C8404dE7F403"),  # veSPA  — Sperax
        (1, "0x0C30476f66034E11782938DF8e4384970B6c9e8a"),  # veSDT  — Stake DAO
        # --- Arbitrum ---
        (42161, "0x2e2071180682Ce6C247B1eF93d382D509F5F6A17"),  # veSPA — Sperax
        # --- Polygon ---
        (137, "0x880DeCADe22aD9c58A8A4202EF143c4F305100B3"),  # eQi  — QiDao
    )
)


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
        atomic_write_bytes(cache_path, body)
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
    SLUGS: ClassVar[dict[int, str]] = {
        1:     "uniswap",
        10:    "optimistic-ethereum",
        137:   "polygon-pos",
        42161: "arbitrum-one",
        8453:  "base",
        43114: "avalanche",
        # Robinhood Chain. This map is what feeds ``is_known`` — the gate a
        # token must pass to be shown at all — so without an entry the chain's
        # tokens stay invisible even once discovery returns them.
        4663:  "robinhood",
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
    # chain_id → Curve blockchainId slug (Gnosis is "xdai", its old name)
    SLUGS: ClassVar[dict[int, str]] = {
        1:     "ethereum",
        10:    "optimism",
        137:   "polygon",
        42161: "arbitrum",
        8453:  "base",
        100:   "xdai",
        43114: "avalanche",
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
                    # Curve doesn't return logoURI, but curve-assets serves
                    # icons by lower-case address under a per-chain folder:
                    # images/assets/ for mainnet, images/assets-<slug>/ for
                    # every other chain (assets-xdai, assets-polygon, …). The
                    # old code always used images/assets/, which 404'd for
                    # every non-mainnet token (e.g. Gnosis EURe).
                    asset_dir = "assets" if slug == "ethereum" else f"assets-{slug}"
                    logo = (
                        "https://raw.githubusercontent.com/curvefi/curve-assets"
                        f"/main/images/{asset_dir}/{addr}.png"
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
    CHAINS: ClassVar[list[int]] = [1, 10, 137, 42161, 8453, 43114]

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

def _is_http_logo(uri: str | None) -> bool:
    """True for a logo URI qeth can fetch directly (``http(s)``). A non-http
    URI (``ipfs://``) or ``None`` is worth replacing with a later source's
    http logo — belt-and-suspenders behind ``icons._normalize_icon_url``."""
    return uri is not None and uri.startswith(("http://", "https://"))


class TokenLists:
    """Loads multiple TokenListSource implementations and merges them.

    Thread-safe for reads. ``load()`` rebuilds the index; per-source
    failures are caught and logged so a single bad source doesn't take
    everything down.
    """

    def __init__(
        self,
        sources: list[TokenListSource] | None = None,
        cache_dir: Path | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.sources = sources if sources is not None else list(DEFAULT_SOURCES)
        # See RiskCache for why we don't bind the default at def-time.
        self.cache_dir = cache_dir if cache_dir is not None else CACHE_DIR
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
                        existing = new_index.get(key)
                        if existing is None:
                            new_index[key] = entry
                            added += 1
                        elif (not _is_http_logo(existing.logo_uri)
                              and _is_http_logo(entry.logo_uri)):
                            # First (most-trusted) source keeps the entry's
                            # identity, but its non-http logo (ipfs://, or
                            # none) is shadowing a fetchable one — borrow the
                            # later source's http(s) logo so the icon loads.
                            new_index[key] = replace(
                                existing, logo_uri=entry.logo_uri)
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

    def is_likely_scam(self, chain_id: int, contract: str,
                       symbol: str, name: str,
                       risk=None) -> bool:
        """Loose heuristic for "shitcoin pretending to be something".

        Presence on any curated whitelist short-circuits to False — if
        a token's contract is in Uniswap/CoinGecko/Curve/1inch we trust
        it regardless of how scammy the on-chain name reads. A curated set
        of non-transferable governance locks (``_TRUSTED_LOCKS``) short-
        circuits the same way — GoPlus false-flags those as honeypots.

        Otherwise, in order of confidence:
        - ``risk.is_high_risk()`` returns True (GoPlus reported a
          honeypot / hidden owner / blacklist / >50% sell tax / etc.)
        - URL fragments (``http``, ``.com``, ``t.me/`` …) or
          claim/visit keywords in the name or symbol.
        - Symbol matches a canonical major (ETH/USDC/…) but the
          contract isn't whitelisted (= impersonation).

        ``risk`` is a ``qeth.plugins.tokens.risk.RiskReport`` or None; passing None is
        the same as having no GoPlus data yet — the heuristic still
        catches the obvious URL/keyword/impersonation cases.
        """
        if self.is_known(chain_id, contract):
            return False
        if (chain_id, contract.lower()) in _TRUSTED_LOCKS:
            # A known non-transferable governance lock — GoPlus false-flags
            # these as honeypots/blacklisted. See _TRUSTED_LOCKS.
            return False
        if risk is not None and risk.is_high_risk():
            return True
        blob = f" {(name or '').lower()} {(symbol or '').lower()} "
        return (
            any(n in blob for n in _SCAM_URL_NEEDLES)
            or any(k in blob for k in _SCAM_KEYWORDS)
            or (symbol or "").strip().lower() in _CANONICAL_SYMBOLS
        )

    def get(self, chain_id: int, address: str) -> TokenListEntry | None:
        return self._index.get((chain_id, address.lower()))

    def count(self, chain_id: int | None = None) -> int:
        if chain_id is None:
            return len(self._index)
        return sum(1 for (cid, _) in self._index if cid == chain_id)

    def addresses_for_chain(self, chain_id: int) -> list[str]:
        """Every curated contract on ``chain_id``, lowercase hex.

        Used as a discovery source in ``TokensPlugin._refresh``:
        the multicall set is built as ``blockscout ∪ forced ∪
        siblings ∪ curated`` so a wallet that holds e.g. USDC will
        have it discovered immediately, regardless of whether
        Blockscout has indexed it for that holder yet.

        Cheap (single dict scan); returns a fresh list so the
        caller can sort/mutate safely."""
        with self._lock:
            return [addr for (cid, addr) in self._index if cid == chain_id]

    @property
    def loaded(self) -> bool:
        return self._loaded
