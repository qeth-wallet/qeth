"""Every way qeth finds tokens to consider for a wallet.

- ``sources``: per-holder explorer sources (Etherscan v2, Blockscout) that
  list which ERC-20s an address holds, plus the per-chain endpoint maps
  (``BLOCKSCOUT_INSTANCES``, ``ETHERSCAN_V2_*``) reused across the codebase.
- ``tokenlists``: curated whitelists (Uniswap / CoinGecko / Curve / 1inch),
  merged + disk-cached; a token must be recognised to survive discovery.
- ``toptokens``: the top-by-market-cap head, a candidate source of its own.
- ``own_history``: tokens the user obtained through their OWN transactions
  (vault/LP tokens), reconstructed from the local tx + activity caches.
"""

from .sources import (
    BLOCKSCOUT_INSTANCES,
    ETHERSCAN_PAGE_CAP,
    ETHERSCAN_V2_BASE,
    ETHERSCAN_V2_CHAINS,
    BlockscoutSource,
    EtherscanV2Source,
    RateLimited,
    RoutedTokenSource,
    TokenBalance,
    TokenSource,
    TokenSourceError,
    UnsupportedChain,
)
from .tokenlists import (
    CoinGeckoPerChain,
    Curve,
    OneInch,
    TokenListEntry,
    TokenLists,
    TokenListSource,
    UniswapDefault,
)
from .toptokens import COINGECKO_PLATFORMS, TopToken, TopTokens, fetch_top_tokens
# own_history LAST: it pulls in transactions_cache → transactions, which imports
# BLOCKSCOUT_INSTANCES back from this package — so `sources` must be bound first.
from .own_history import discover_own_tokens

__all__ = [
    # sources
    "TokenSource", "TokenBalance", "TokenSourceError", "RateLimited",
    "UnsupportedChain", "RoutedTokenSource", "EtherscanV2Source",
    "BlockscoutSource", "BLOCKSCOUT_INSTANCES", "ETHERSCAN_V2_BASE",
    "ETHERSCAN_V2_CHAINS", "ETHERSCAN_PAGE_CAP",
    # tokenlists
    "TokenLists", "TokenListEntry", "TokenListSource", "CoinGeckoPerChain",
    "Curve", "OneInch", "UniswapDefault",
    # toptokens
    "TopTokens", "TopToken", "COINGECKO_PLATFORMS", "fetch_top_tokens",
    # own-history discovery
    "discover_own_tokens",
]
