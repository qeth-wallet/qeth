"""USD price discovery for native + ERC-20 assets.

- ``base``: the ``PriceSource`` abstraction + ``Price`` result type.
- ``native``: native-asset (gas coin) CoinGecko id resolution.
- ``defillama``: the free, keyless, multichain primary price source.
- ``onchain``: on-chain vault/LP pricing (ERC-4626, Curve LP, UniV2 LP) +
  ``ChainedPriceSource`` that falls back to it when the primary has no quote.

Result shape everywhere: ``{ key: Price }`` where ``key`` is the lower-case
ERC-20 address, or the empty string ``""`` for the native asset.
"""

from .base import Price, PriceSource, PriceSourceError
from .defillama import DEFILLAMA_CHAIN_SLUGS, DefiLlamaPrices
from .native import (
    NATIVE_COINGECKO_IDS,
    load_native_coin_ids,
    native_coingecko_id,
)
from .onchain import ChainedPriceSource, OnChainVaultPrices

__all__ = [
    "Price",
    "PriceSource",
    "PriceSourceError",
    "DefiLlamaPrices",
    "DEFILLAMA_CHAIN_SLUGS",
    "NATIVE_COINGECKO_IDS",
    "load_native_coin_ids",
    "native_coingecko_id",
    "ChainedPriceSource",
    "OnChainVaultPrices",
]
