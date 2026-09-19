"""Token balance discovery — abstract source + first implementation.

The wallet asks a ``TokenSource`` to enumerate the ERC-20 tokens an
address holds (with current balances). Sources are pluggable so we can
mix providers per chain or stack them with fallbacks: Etherscan v2 (keyed)
in front of Blockscout's REST v2 API (keyless).

Future sources to add: Alchemy ``alchemy_getTokenBalances``, Covalent.
"""

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal

from .. import USER_AGENT
from ..chains import Chain

log = logging.getLogger("qeth.token_discovery.sources")


@dataclass(frozen=True)
class TokenBalance:
    contract: str          # token contract address (checksummed if source provides)
    symbol: str
    name: str
    decimals: int
    balance_raw: int       # integer in token's smallest unit

    @property
    def balance(self) -> Decimal:
        if self.decimals <= 0:
            return Decimal(self.balance_raw)
        return Decimal(self.balance_raw) / (Decimal(10) ** self.decimals)


class TokenSourceError(Exception):
    pass


class RateLimited(TokenSourceError):
    """The source rejected the call with a rate-limit response. A router
    can catch this and immediately try a backup source for this call."""


class UnsupportedChain(TokenSourceError):
    pass


class TokenSource(ABC):
    """A backend that, given a chain and address, returns the held tokens."""

    @abstractmethod
    def list_balances(self, chain: Chain, address: str) -> list[TokenBalance]:
        ...

    def supports(self, chain: Chain) -> bool:
        return True


# Public Blockscout instances per chain. Override via BlockscoutSource(instances=).
# Keyed by chain id, so an entry lights up a chain whether it's one of
# DEFAULT_CHAINS or user-added via chainlist (e.g. TAC). All three
# Blockscout-backed sources — tokens, tx history, ABI/contract-identity —
# read this same map.
BLOCKSCOUT_INSTANCES: dict[int, str] = {
    1:     "https://eth.blockscout.com",
    10:    "https://optimism.blockscout.com",
    137:   "https://polygon.blockscout.com",
    42161: "https://arbitrum.blockscout.com",
    8453:  "https://base.blockscout.com",
    100:   "https://gnosis.blockscout.com",
    # TAC (TON↔EVM hybrid; not a DEFAULT_CHAIN, added via chainlist).
    # Verified to speak the Blockscout v1 schema (tokenlist / txlist /
    # getabi) at this base, 2026-06-12.
    239:   "https://explorer.tac.build",
}

# Keyless, Blockscout's Etherscan-compatible v1 ``/api`` allows 10 requests per
# clock hour per IP per instance (probed 2026-09-13) — too few for even one
# discovery sweep — while REST v2 ``/api/v2/...`` allows ~180 per 30 s. So the
# recurring keyless reads (token list, tx history) go through v2.


def blockscout_v2_items(fetch: Callable[[str], bytes], url: str,
                        params: dict[str, object], *,
                        error: type[Exception],
                        max_pages: int | None = None) -> Iterator[dict]:
    """Yield the rows of a Blockscout REST v2 list endpoint, page after page.

    v2 pages are a fixed 50 rows chained by a ``next_page_params`` keyset
    cursor, sent back alongside the caller's own ``params`` — minus its null
    fields, which the API rejects as a literal "None" (``fiat_value`` on
    ``/tokens``). Pages are fetched lazily, so a caller that stops pulling
    fetches no further page. Stops at the last page or after ``max_pages``; a
    body without an ``items`` list raises ``error``."""
    query = dict(params)
    pages = 0
    while True:
        data = json.loads(fetch(url + "?" + urllib.parse.urlencode(query)))
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            detail = data.get("message") if isinstance(data, dict) else None
            raise error(detail or "blockscout error")
        yield from (row for row in items if isinstance(row, dict))
        pages += 1
        cursor = data.get("next_page_params")
        if not isinstance(cursor, dict) or (
                max_pages is not None and pages >= max_pages):
            return
        query = {**params, **{k: v for k, v in cursor.items() if v is not None}}


# Chains whose tokenlist endpoint Etherscan v2 services. The
# unified API covers ~50 chains under one key
# (``api.etherscan.io/v2/api?chainid=<chainid>``); we deliberately
# enumerate the ones qeth ships so the picker / source-routing
# logic doesn't claim support for chains we haven't actually
# checked. Extend as new defaults land.
ETHERSCAN_V2_CHAINS: frozenset[int] = frozenset({
    1, 10, 56, 100, 137, 8453, 42161, 43114,
    # Robinhood Chain (Arbitrum Orbit, ETH for gas). Not a DEFAULT_CHAIN —
    # added via chainlist — but Etherscan v2 routes it (its /v2/chainlist
    # reports status 1, and a keyless call answers "Missing/Invalid API Key"
    # rather than "unsupported chainid"), verified 2026-09-20. It has NO usable
    # Blockscout: robinhoodchain.blockscout.com serves every API path behind a
    # Cloudflare JS challenge (403 cf-mitigated: challenge), so a key is the
    # only way to get history / discovery / approvals on this chain.
    4663,
})
ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"

# Etherscan v2's per-page hard cap for addresstokenbalance. We request
# exactly this many rows in one shot (no pagination loop) — see the note
# in EtherscanV2Source.list_balances. If a wallet ever holds this many
# distinct token contracts the result is silently truncated, which is
# the failure mode that once hid a real holding behind offset=100, so we
# log when we land exactly on the cap rather than guessing it's complete.
ETHERSCAN_PAGE_CAP = 10000


class EtherscanV2Source(TokenSource):
    """Etherscan v2 unified-multichain tokenlist endpoint.

    Requires a global API key
    (one key covers every chain Etherscan v2 supports); the key is
    fetched dynamically via ``get_api_key`` so the user can paste
    it at runtime without re-instantiating the plugin.

    ``supports`` returns True only when the key is set *and* the
    chain is in the enumerated v2-supported set, so the routing
    layer can cleanly choose between this and the Blockscout
    fallback without trapping exceptions.
    """

    def __init__(
        self,
        get_api_key,
        timeout: float = 20.0,
        supported_chains: frozenset[int] | None = None,
    ):
        self._get_api_key = get_api_key
        self.timeout = timeout
        self._supported = (
            supported_chains
            if supported_chains is not None
            else ETHERSCAN_V2_CHAINS
        )

    def supports(self, chain: Chain) -> bool:
        if chain.chain_id not in self._supported:
            return False
        return bool(self._get_api_key())

    def list_balances(self, chain: Chain, address: str) -> list[TokenBalance]:
        key = self._get_api_key()
        if not key:
            raise UnsupportedChain("No Etherscan API key configured")
        # offset=ETHERSCAN_PAGE_CAP is Etherscan v2's per-page cap.
        # Anything smaller silently truncates wallets with long-tail
        # holdings — the wallet held a curated, priced, ~$9k YB
        # position that sat at index >100 and was invisible until
        # we asked for the full page. Pagination beyond the cap would
        # need a loop, but no real holding pattern hits that (and we
        # log below if a wallet ever lands exactly on the cap).
        params = [
            ("chainid", str(chain.chain_id)),
            ("module", "account"),
            ("action", "addresstokenbalance"),
            ("address", address),
            ("page", "1"),
            ("offset", str(ETHERSCAN_PAGE_CAP)),
            ("apikey", key),
        ]
        url = f"{ETHERSCAN_V2_BASE}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read())

        if data.get("status") != "1":
            msg = (data.get("message") or "").lower()
            res = data.get("result")
            # Etherscan puts the rate-limit text in `result` ("Max calls
            # per sec rate limit reached"), with message just "NOTOK" —
            # so check both before classifying.
            res_text = res.lower() if isinstance(res, str) else ""
            if "no token" in msg or "not found" in msg:
                return []
            blob = f"{msg} {res_text}"
            if "rate limit" in blob or "max calls" in blob or "max rate" in blob:
                raise RateLimited(
                    res if isinstance(res, str)
                    else (data.get("message") or "etherscan rate limit")
                )
            raise TokenSourceError(data.get("message") or "etherscan error")

        result = data.get("result") or []
        if len(result) >= ETHERSCAN_PAGE_CAP:
            # Landed exactly on (or past) the single-page cap: the
            # wallet almost certainly has more tokens than we fetched
            # and the tail is truncated. Surface it instead of
            # silently returning a partial list (this is the offset=100
            # bug class — a missing holding looks identical to "no such
            # token"). Fixing it would mean paginating page=2,3,…
            log.warning(
                "etherscan tokenlist hit the %d-row page cap for %s on "
                "chain %d — holdings beyond that are truncated",
                ETHERSCAN_PAGE_CAP, address, chain.chain_id,
            )

        out: list[TokenBalance] = []
        for entry in result:
            try:
                decimals_raw = entry.get("TokenDivisor") or entry.get("decimals") or "18"
                out.append(TokenBalance(
                    contract=entry.get("TokenAddress") or entry.get("contractAddress"),
                    symbol=entry.get("TokenSymbol") or entry.get("symbol") or "?",
                    name=entry.get("TokenName") or entry.get("name") or "",
                    decimals=int(decimals_raw) if decimals_raw != "" else 18,
                    balance_raw=int(entry.get("TokenQuantity") or entry.get("balance") or 0),
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return out


# Within this window after a primary (Etherscan) call, route to the
# secondary (Blockscout) instead — when it can serve the chain. Fast
# keyboard navigation through wallets fires a discovery per selection;
# without this, a burst hammers Etherscan's ~5 req/s free tier and trips
# its rate limit. The source only supplies the *contract list* (balances
# and metadata come from on-chain multicall, see TokensPlugin), so
# Blockscout's list is a fine stand-in during a burst; the settled
# wallet's periodic 60 s refresh lands outside the window and uses
# Etherscan again.
_PRIMARY_COOLDOWN_S = 2.0


class RoutedTokenSource(TokenSource):
    """Prefer ``primary`` when it supports the chain, fall back to
    ``secondary`` otherwise. Lets the wallet wire Etherscan-when-
    key-is-set in front of Blockscout without either source knowing
    about the other.

    Adds burst protection for the rate-limited primary: within
    ``cooldown`` seconds of a primary call, route to the secondary when
    it supports the chain; and if the primary still returns a rate-limit
    response, fall back to the secondary for that call."""

    def __init__(self, primary: TokenSource, secondary: TokenSource, *,
                 cooldown: float = _PRIMARY_COOLDOWN_S, clock=time.monotonic):
        self._primary = primary
        self._secondary = secondary
        self._cooldown = cooldown
        self._clock = clock
        self._last_primary: float | None = None
        self._lock = threading.Lock()

    def supports(self, chain: Chain) -> bool:
        return self._primary.supports(chain) or self._secondary.supports(chain)

    def _in_cooldown(self) -> bool:
        with self._lock:
            return (self._last_primary is not None
                    and (self._clock() - self._last_primary) < self._cooldown)

    def _mark_primary(self) -> None:
        with self._lock:
            self._last_primary = self._clock()

    def list_balances(self, chain: Chain, address: str) -> list[TokenBalance]:
        primary_ok = self._primary.supports(chain)
        secondary_ok = self._secondary.supports(chain)
        # Burst: spread off the rate-limited primary onto the secondary
        # (only when the secondary can actually serve this chain — for
        # chains it can't, e.g. BNB, we still use the primary).
        if primary_ok and secondary_ok and self._in_cooldown():
            return self._secondary.list_balances(chain, address)
        if primary_ok:
            self._mark_primary()
            try:
                return self._primary.list_balances(chain, address)
            except Exception as e:      # noqa: BLE001 — any primary failure → fall back
                # Fall back to the secondary on ANY primary failure, not just a
                # rate limit: Etherscan's per-holder `addresstokenbalance` is a
                # PRO-only endpoint that errors (TokenSourceError) on a free key,
                # and a transport hiccup shouldn't collapse discovery either.
                # Without this, the keyed path propagated the error → the Tokens
                # tab fell back to the top-N-only pass and dropped real holdings.
                if secondary_ok:
                    if not isinstance(e, RateLimited):
                        log.warning(
                            "primary token source failed (%s); falling back to "
                            "the secondary for %s on chain %d",
                            e, address, chain.chain_id)
                    return self._secondary.list_balances(chain, address)
                raise
        if secondary_ok:
            return self._secondary.list_balances(chain, address)
        raise UnsupportedChain(
            f"No token source supports chain {chain.chain_id}"
        )


class BlockscoutSource(TokenSource):
    """Blockscout REST v2 ``/api/v2/addresses/{addr}/tokens?type=ERC-20`` —
    the ERC-20s an address holds (NFTs are filtered out server-side for the
    wallet's fungible-balances view).

    v2, not v1 ``tokenlist``: see ``blockscout_v2_items``. It pages 50 rows at a
    time (a ~470-token account is 10 requests, ~6 s), priced holdings first by
    USD value, then the rest by raw balance — so ``MAX_PAGES`` only ever cuts an
    unpriced, spam-flooded tail, and says so in the log."""

    MAX_PAGES = 50

    def __init__(
        self,
        instances: dict[int, str] | None = None,
        timeout: float = 20.0,
    ):
        self.instances = instances if instances is not None else BLOCKSCOUT_INSTANCES
        self.timeout = timeout

    def supports(self, chain: Chain) -> bool:
        return chain.chain_id in self.instances

    def list_balances(self, chain: Chain, address: str) -> list[TokenBalance]:
        base = self.instances.get(chain.chain_id)
        if not base:
            raise UnsupportedChain(
                f"No Blockscout instance configured for chain {chain.chain_id}"
            )
        def fetch(url: str) -> bytes:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read()

        out: list[TokenBalance] = []
        rows = 0
        for entry in blockscout_v2_items(
                fetch,
                f"{base.rstrip('/')}/api/v2/addresses/"
                f"{urllib.parse.quote(address)}/tokens",
                {"type": "ERC-20"},
                error=TokenSourceError, max_pages=self.MAX_PAGES):
            rows += 1
            token = entry.get("token")
            if not isinstance(token, dict):
                continue
            t_type = (token.get("type") or "").upper().replace("-", "")
            if t_type and t_type != "ERC20":
                continue  # belt-and-braces: the query already asks for ERC-20
            try:
                decimals_raw = token.get("decimals") or "18"
                out.append(TokenBalance(
                    contract=token["address_hash"],
                    symbol=token.get("symbol") or "?",
                    name=token.get("name") or "",
                    decimals=int(decimals_raw),
                    balance_raw=int(entry.get("value") or 0),
                ))
            except (KeyError, ValueError, TypeError):
                continue
        if rows >= self.MAX_PAGES * 50:
            log.warning(
                "blockscout token list hit the %d-page cap for %s on chain %d "
                "— the least valuable holdings beyond it are truncated",
                self.MAX_PAGES, address, chain.chain_id,
            )
        return out
