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
