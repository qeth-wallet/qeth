"""Per-wallet cache of the last-displayed tokens, balances, and prices.

Eliminates the flash-of-pre-price tokens by letting the panel render
immediately from cache while background workers refresh in place. Also
keeps the wallet useful when Blockscout or DefiLlama are down: stale
data is preferable to an empty panel.

Layout: ``~/.qeth/wallets/<chain_id>/<address_lower>.json``. One file
per ``(chain, address)`` so writes don't contend and an individual
cache is easy to invalidate.
"""

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .fsatomic import atomic_write_text

log = logging.getLogger("qeth.wallet_cache")

CACHE_DIR = Path.home() / ".qeth" / "wallets"


@dataclass
class CachedToken:
    contract: str         # lower-case
    symbol: str
    name: str
    decimals: int
    logo_uri: str | None = None
    balance_raw: int = 0
    # Decimal-as-string so JSON round-trips don't lose precision.
    price_usd: str | None = None
    balance_updated: int = 0
    price_updated: int = 0


@dataclass
class CachedWallet:
    chain_id: int
    address: str          # lower-case
    tokens: list[CachedToken] = field(default_factory=list)
    native_balance_wei: int = 0
    native_price_usd: str | None = None
    native_balance_updated: int = 0
    native_price_updated: int = 0


class WalletCache:
    def __init__(self, cache_dir: Path | None = None):
        # See RiskCache for why we don't bind the default at def-time.
        self.cache_dir = cache_dir if cache_dir is not None else CACHE_DIR
        self._lock = threading.Lock()

    def _path(self, chain_id: int, address: str) -> Path:
        return self.cache_dir / str(chain_id) / f"{address.lower()}.json"

    def load(self, chain_id: int, address: str) -> CachedWallet | None:
        p = self._path(chain_id, address)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("wallet cache parse failed %s: %s", p, e)
            return None
        try:
            tokens = [
                CachedToken(
                    contract=str(t["contract"]).lower(),
                    symbol=str(t.get("symbol") or "?"),
                    name=str(t.get("name") or ""),
                    decimals=int(t.get("decimals") or 18),
                    logo_uri=t.get("logo_uri"),
                    balance_raw=int(t.get("balance_raw") or 0),
                    price_usd=t.get("price_usd"),
                    balance_updated=int(t.get("balance_updated") or 0),
                    price_updated=int(t.get("price_updated") or 0),
                )
                for t in data.get("tokens", []) if t.get("contract")
            ]
        except (TypeError, ValueError) as e:
            log.warning("wallet cache bad token entry in %s: %s", p, e)
            return None
        return CachedWallet(
            chain_id=int(data.get("chain_id", chain_id)),
            address=str(data.get("address", address)).lower(),
            tokens=tokens,
            native_balance_wei=int(data.get("native_balance_wei") or 0),
            native_price_usd=data.get("native_price_usd"),
            native_balance_updated=int(data.get("native_balance_updated") or 0),
            native_price_updated=int(data.get("native_price_updated") or 0),
        )

    def save(self, cached: CachedWallet) -> None:
        with self._lock:
            p = self._path(cached.chain_id, cached.address)
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "chain_id": cached.chain_id,
                "address": cached.address.lower(),
                "tokens": [asdict(t) for t in cached.tokens],
                "native_balance_wei": cached.native_balance_wei,
                "native_price_usd": cached.native_price_usd,
                "native_balance_updated": cached.native_balance_updated,
                "native_price_updated": cached.native_price_updated,
            }
            atomic_write_text(p, json.dumps(data, indent=2))
