"""Persistent on-disk cache of per-chain ERC-20 metadata.

(name, symbol, decimals) for an ERC-20 contract are immutable after
deployment — once fetched via multicall, they never need to be queried
again. One JSON file per chain at
``~/.qeth/token_metadata/<chain_id>.json``.
"""

import json
import logging
import threading
from pathlib import Path

from .fsatomic import atomic_write_text

log = logging.getLogger("qeth.token_metadata")

CACHE_DIR = Path.home() / ".qeth" / "token_metadata"


class TokenMetadataCache:
    """Two-tier cache: per-chain in-memory dict backed by per-chain JSON.

    Reads are cheap (in-memory); the on-disk file is loaded lazily the
    first time a chain is queried. Writes are batched — call ``put_many``
    after a multicall and the whole chain file is rewritten once.
    """

    def __init__(self, cache_dir: Path | None = None):
        # See RiskCache for why we don't bind the default at def-time.
        self.cache_dir = cache_dir if cache_dir is not None else CACHE_DIR
        self._lock = threading.RLock()
        self._chains: dict[int, dict[str, dict]] = {}

    def _path(self, chain_id: int) -> Path:
        return self.cache_dir / f"{int(chain_id)}.json"

    def _load_chain(self, chain_id: int) -> dict[str, dict]:
        p = self._path(chain_id)
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text())
            return {str(k).lower(): v for k, v in data.items() if isinstance(v, dict)}
        except Exception as e:
            log.warning("metadata cache parse failed %s: %s", p, e)
            return {}

    def _chain_cache(self, chain_id: int) -> dict[str, dict]:
        chain_id = int(chain_id)
        with self._lock:
            cc = self._chains.get(chain_id)
            if cc is None:
                cc = self._load_chain(chain_id)
                self._chains[chain_id] = cc
            return cc

    def get(self, chain_id: int, contract: str) -> dict | None:
        return self._chain_cache(chain_id).get(contract.lower())

    def missing(self, chain_id: int, contracts: list[str]) -> list[str]:
        """Subset of ``contracts`` not present in the cache. Preserves
        original casing (which the caller may need for downstream calls)."""
        cc = self._chain_cache(chain_id)
        return [c for c in contracts if c.lower() not in cc]

    def put_many(self, chain_id: int, items: dict[str, dict]) -> None:
        chain_id = int(chain_id)
        with self._lock:
            cc = self._chain_cache(chain_id)
            for addr, meta in items.items():
                if not isinstance(meta, dict):
                    continue
                cc[addr.lower()] = {
                    "symbol": str(meta.get("symbol") or ""),
                    "name": str(meta.get("name") or ""),
                    "decimals": int(meta.get("decimals") or 18),
                }
            p = self._path(chain_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(p, json.dumps(cc, indent=2, sort_keys=True))
