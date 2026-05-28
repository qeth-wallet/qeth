"""Token balance discovery — abstract source + first implementation.

The wallet asks a ``TokenSource`` to enumerate the ERC-20 tokens an
address holds (with current balances). Sources are pluggable so we can
mix providers per chain or stack them with fallbacks; today the only
implementation is Blockscout's Etherscan-compatible v1 API.

Future sources to add: Etherscan V2 multichain (account/tokentx +
multicall3 balanceOf), Alchemy ``alchemy_getTokenBalances``, Covalent.
"""

import json
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from . import USER_AGENT
from .chains import Chain


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
BLOCKSCOUT_INSTANCES: dict[int, str] = {
    1:     "https://eth.blockscout.com",
    10:    "https://optimism.blockscout.com",
    137:   "https://polygon.blockscout.com",
    42161: "https://arbitrum.blockscout.com",
    8453:  "https://base.blockscout.com",
}


# Chains whose tokenlist endpoint Etherscan v2 services. The
# unified API covers ~50 chains under one key
# (``api.etherscan.io/v2/api?chainid=<chainid>``); we deliberately
# enumerate the ones qeth ships so the picker / source-routing
# logic doesn't claim support for chains we haven't actually
# checked. Extend as new defaults land.
ETHERSCAN_V2_CHAINS: frozenset[int] = frozenset({
    1, 10, 56, 100, 137, 8453, 42161,
})
ETHERSCAN_V2_BASE = "https://api.etherscan.io/v2/api"


class EtherscanV2Source(TokenSource):
    """Etherscan v2 unified-multichain tokenlist endpoint.

    Same Etherscan v1 response schema as Blockscout, so the parsing
    block below mirrors ``BlockscoutSource.list_balances`` line for
    line — only the URL shape differs. Requires a global API key
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
        params = [
            ("chainid", str(chain.chain_id)),
            ("module", "account"),
            ("action", "addresstokenbalance"),
            ("address", address),
            ("page", "1"),
            ("offset", "100"),
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
            if "no token" in msg or "not found" in msg:
                return []
            raise TokenSourceError(data.get("message") or "etherscan error")

        out: list[TokenBalance] = []
        for entry in data.get("result") or []:
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


class RoutedTokenSource(TokenSource):
    """Prefer ``primary`` when it supports the chain, fall back to
    ``secondary`` otherwise. Lets the wallet wire Etherscan-when-
    key-is-set in front of Blockscout without either source
    knowing about the other."""

    def __init__(self, primary: TokenSource, secondary: TokenSource):
        self._primary = primary
        self._secondary = secondary

    def supports(self, chain: Chain) -> bool:
        return self._primary.supports(chain) or self._secondary.supports(chain)

    def list_balances(self, chain: Chain, address: str) -> list[TokenBalance]:
        if self._primary.supports(chain):
            return self._primary.list_balances(chain, address)
        if self._secondary.supports(chain):
            return self._secondary.list_balances(chain, address)
        raise UnsupportedChain(
            f"No token source supports chain {chain.chain_id}"
        )


class BlockscoutSource(TokenSource):
    """Etherscan-compatible ``/api?module=account&action=tokenlist`` endpoint.

    Filters the result to ERC-20 tokens (NFTs/ERC-721/1155 are skipped for
    the wallet's fungible-balances view)."""

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
        url = (
            f"{base.rstrip('/')}/api?module=account&action=tokenlist"
            f"&address={urllib.parse.quote(address)}"
        )
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read())

        # Etherscan-compatible: status "0" / "No tokens found" is a valid
        # empty result, not an error.
        if data.get("status") != "1":
            msg = (data.get("message") or "").lower()
            if "no tokens" in msg or "not found" in msg:
                return []
            raise TokenSourceError(data.get("message") or "blockscout error")

        out: list[TokenBalance] = []
        for entry in data.get("result") or []:
            t_type = (entry.get("type") or "").upper().replace("-", "")
            if t_type and t_type != "ERC20":
                continue  # skip ERC-721 / ERC-1155 for the fungible view
            try:
                decimals_raw = entry.get("decimals") or "18"
                out.append(TokenBalance(
                    contract=entry["contractAddress"],
                    symbol=entry.get("symbol") or "?",
                    name=entry.get("name") or "",
                    decimals=int(decimals_raw) if decimals_raw != "" else 18,
                    balance_raw=int(entry.get("balance") or 0),
                ))
            except (KeyError, ValueError):
                continue
        return out
