"""DefiLlama USD price source — free, keyless, multichain, large batches.

``DefiLlamaPrices`` is the wallet's default primary source. Result keys are
lower-case ERC-20 addresses, or ``""`` for the native asset.
"""

import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation

from .. import USER_AGENT
from .base import Price, PriceSource
from .native import load_native_coin_ids, native_coingecko_id

log = logging.getLogger("qeth.pricing.defillama")

DEFAULT_TIMEOUT = 8.0
# DefiLlama's CDN 404s request URLs beyond ~5 KB (and 414s past ~10 KB), so we
# batch by URL LENGTH, not by a fixed key count. The old fixed 100-key batch
# built ~5.2 KB URLs and every request 404'd — which, since an unpriced token is
# dropped from the panel, silently emptied the whole token list. Keep each URL
# well under the observed threshold.
PRICES_URL = "https://coins.llama.fi/prices/current/"
MAX_URL_LEN = 3000

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
    43114: "avax",
}


def _price_url(keys: list[str]) -> str:
    return PRICES_URL + urllib.parse.quote(",".join(keys), safe=":,")


def _url_batches(keys: list[str], max_url_len: int = MAX_URL_LEN) -> Iterable[list[str]]:
    """Group ``keys`` so each request URL stays under ``max_url_len`` — bounding
    by URL length (not key count), since that is what DefiLlama's CDN rejects."""
    chunk: list[str] = []
    for k in keys:
        if chunk and len(_price_url([*chunk, k])) > max_url_len:
            yield chunk
            chunk = []
        chunk.append(k)
    if chunk:
        yield chunk


class DefiLlamaPrices(PriceSource):
    """https://coins.llama.fi/prices/current/<key,key,...>"""

    name = "defillama"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout

    def fetch(self, chain, contracts, include_native=False):
        slug = DEFILLAMA_CHAIN_SLUGS.get(chain.chain_id)
        keys: list[str] = []
        if include_native:
            load_native_coin_ids()   # discover (disk-cached ~7d) the native id
            native_id = native_coingecko_id(chain)
            if native_id:
                keys.append(f"coingecko:{native_id}")
        if slug:
            for c in contracts:
                c = c.lower()
                if c.startswith("0x") and len(c) == 42:
                    keys.append(f"{slug}:{c}")
        if not keys:
            return {}

        out: dict[str, Price] = {}
        for chunk in _url_batches(keys):
            url = _price_url(chunk)
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
