"""USD price discovery for native + ERC-20 assets.

``PriceSource`` is the abstract base; ``DefiLlamaPrices`` is the only
implementation today — it's free, no API key, multichain, and accepts
large batches in a single HTTP request, which makes it the right
default for the wallet's token-panel use case.

Result shape: ``{ key: Price }`` where ``key`` is the lower-case ERC-20
address, or the empty string ``""`` for the native asset (matching
``TokenListPanel.NATIVE_CONTRACT``).
"""

import json
import logging
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

from . import USER_AGENT
from .chains import Chain

log = logging.getLogger("qeth.prices")

DEFAULT_TIMEOUT = 8.0
BATCH_SIZE = 100  # URL length is the real constraint; 100 keys is well under it.

# chain_id -> DefiLlama chain slug used in coin keys like "ethereum:0x...".
# When a chain is missing here, fetch() silently skips all ERC-20
# price requests for it — the tokens panel then drops every priced
# row at the "price is None" filter, so the wallet looks empty
# even when discovery itself works. Keep this map in step with
# DEFAULT_CHAINS additions.
DEFILLAMA_CHAIN_SLUGS: dict[int, str] = {
    1:     "ethereum",
    10:    "optimism",
    56:    "bsc",
    100:   "xdai",
    137:   "polygon",
    8453:  "base",
    42161: "arbitrum",
}


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


def _batched(items: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


class DefiLlamaPrices(PriceSource):
    """https://coins.llama.fi/prices/current/<key,key,...>"""

    name = "defillama"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout

    def fetch(self, chain, contracts, include_native=False):
        slug = DEFILLAMA_CHAIN_SLUGS.get(chain.chain_id)
        keys: list[str] = []
        if include_native and chain.coingecko_id:
            keys.append(f"coingecko:{chain.coingecko_id}")
        if slug:
            for c in contracts:
                c = c.lower()
                if c.startswith("0x") and len(c) == 42:
                    keys.append(f"{slug}:{c}")
        if not keys:
            return {}

        out: dict[str, Price] = {}
        for chunk in _batched(keys, BATCH_SIZE):
            url = (
                "https://coins.llama.fi/prices/current/"
                + urllib.parse.quote(",".join(chunk), safe=":,")
            )
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
            except Exception as e:
                log.warning("defillama batch failed: %s", e)
                continue
            for k, info in (data.get("coins") or {}).items():
                try:
                    price = Decimal(str(info["price"]))
                except (KeyError, ValueError, InvalidOperation, TypeError):
                    continue
                ts = int(info.get("timestamp") or 0)
                conf = float(info.get("confidence") or 1.0)
                if k.startswith("coingecko:"):
                    out[""] = Price(price, ts, self.name, conf)
                elif ":" in k:
                    _, addr = k.split(":", 1)
                    out[addr.lower()] = Price(price, ts, self.name, conf)
        return out
