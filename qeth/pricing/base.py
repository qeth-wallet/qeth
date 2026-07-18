"""Price-source abstraction shared by every pricing backend.

Result shape for ``PriceSource.fetch``: ``{ key: Price }`` where ``key`` is
the lower-case ERC-20 address, or the empty string ``""`` for the native
asset (matching ``TokenListPanel.NATIVE_CONTRACT``).
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from ..chains import Chain


@dataclass(frozen=True)
class Price:
    price_usd: Decimal
    timestamp: int        # unix seconds — how fresh the quote is
    source: str
    confidence: float = 1.0
    # For a single-underlying vault priced on-chain (ERC-4626 / Yield Basis):
    # the lower-case address of the asset the vault holds (e.g. WBTC for
    # yb-WBTC). Lets the UI show the underlying's icon with a vault badge.
    # None for market quotes and multi-asset LPs.
    underlying: str | None = None
    # For an LP token priced on-chain (Curve / UniV2): the lower-case addresses
    # of the pooled assets, so the UI can stack their icons. None otherwise.
    pool_tokens: tuple[str, ...] | None = None


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
