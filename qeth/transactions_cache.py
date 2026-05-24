"""Disk-backed cache for past transactions, keyed by (chain, address).

Confirmed transactions are immutable: a hash always points to the same
data. That lets us cache aggressively across runs — the plugin loads
the cached page immediately on selection so the user never sees an
empty → populated flicker while the background fetch runs.

Layout mirrors qeth.wallet_cache:
    CACHE_DIR / <chain_id> / <address_lower>.json
each file holds a JSON list of Transaction dicts (newest-first).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .transactions import Transaction


CACHE_DIR = Path.home() / ".qeth" / "transactions"


class TransactionCache:
    """Tiny key-value store over the filesystem. Replace-on-write
    (no merging) is fine for now — each fetch returns the top N
    newest txs, so the saved file always represents the most recent
    window. A paginated-history feature can add a merge step later."""

    def __init__(self, root: Optional[Path] = None):
        # Look up CACHE_DIR at instantiation so tests that monkeypatch
        # the module-level constant (via the tmp_qeth fixture) see the
        # redirected path without having to construct with an explicit root.
        self.root = root if root is not None else CACHE_DIR

    def _path(self, chain_id: int, address: str) -> Path:
        return self.root / str(chain_id) / f"{address.lower()}.json"

    def load(self, chain_id: int, address: str) -> Optional[list[Transaction]]:
        p = self._path(chain_id, address)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        out: list[Transaction] = []
        for entry in data if isinstance(data, list) else ():
            try:
                out.append(Transaction(**entry))
            except (TypeError, ValueError):
                # Schema drift between versions: drop unparseable rows
                # rather than failing the whole load — the background
                # refresh will repopulate the file shortly.
                continue
        return out

    def save(self, chain_id: int, address: str, txs: list[Transaction]) -> None:
        p = self._path(chain_id, address)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(tx) for tx in txs]
        # No indent — these files can hold 50+ rows and the on-disk
        # bytes don't need to be human-readable.
        p.write_text(json.dumps(data, separators=(",", ":")))
