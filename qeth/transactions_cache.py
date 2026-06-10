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

from .fsatomic import atomic_write_text
from .transactions import Transaction


CACHE_DIR = Path.home() / ".qeth" / "transactions"

# ERC-20 selectors whose recipient we can read straight from calldata.
_TRANSFER = "0xa9059cbb"      # transfer(address,uint256)         → arg0
_TRANSFER_FROM = "0x23b872dd"  # transferFrom(address,address,uint256) → arg1


def _erc20_transfer_recipient(data: str) -> Optional[str]:
    """The destination address of an ERC-20 transfer/transferFrom, decoded
    from raw calldata (``0x`` + 8-hex selector + 32-byte-padded args), or
    ``None`` if it isn't one of those calls. Used to tell that a token send
    went *to* an address even though the tx's ``to`` is the token contract."""
    if not data or len(data) < 10:
        return None
    selector = data[:10].lower()
    if selector == _TRANSFER and len(data) >= 74:
        return "0x" + data[34:74].lower()      # arg0, low 20 bytes
    if selector == _TRANSFER_FROM and len(data) >= 138:
        return "0x" + data[98:138].lower()     # arg1 (to), low 20 bytes
    return None


def merge_txs(
    new: list[Transaction], old: list[Transaction],
) -> list[Transaction]:
    """Combine a fresh fetch with older cached entries.

    Dedupes by ``hash``: a transaction the new fetch returned wins
    over its cached counterpart (post-reorg corrections propagate).

    Sorted by ``nonce`` descending. Block number isn't unique within a
    block — multiple sent txs share it — but nonce is monotonic per
    sender, so for the wallet's own outgoing history it gives a true
    most-recent-first ordering. Python's stable sort preserves intra-
    nonce insertion order; ties (e.g. received-from-different-senders
    txs that happen to share a nonce value) follow Blockscout's
    canonical order from the new fetch."""
    new_hashes = {t.hash for t in new}
    merged: list[Transaction] = list(new)
    for t in old:
        if t.hash not in new_hashes:
            merged.append(t)
    merged.sort(key=lambda t: t.nonce, reverse=True)
    return merged


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
        atomic_write_text(p, json.dumps(data, separators=(",", ":")))

    def sent_to_count(self, chain_id: int, recipient: str, addresses) -> int:
        """How many distinct txs the user's accounts *sent value to*
        ``recipient`` — either natively (tx ``to`` == recipient) OR via an
        ERC-20 ``transfer``/``transferFrom`` whose recipient argument is
        ``recipient`` (decoded from calldata). This is the right "have I
        sent here before" signal for the Send dialog, where a token send's
        on-chain ``to`` is the *token contract*, not the destination.
        Cache-only lower bound, deduped by hash."""
        target = (recipient or "").lower()
        if not target:
            return 0
        mine = {a.lower() for a in addresses}
        seen: set[str] = set()
        for addr in mine:
            for t in self.load(chain_id, addr) or []:
                if t.from_addr.lower() not in mine:
                    continue
                if (t.to_addr or "").lower() == target:
                    seen.add(t.hash)
                elif _erc20_transfer_recipient(t.input_data) == target:
                    seen.add(t.hash)
        return len(seen)

    def interaction_count(self, chain_id: int, contract: str,
                          addresses) -> int:
        """How many distinct txs that ``addresses`` *sent* to ``contract``
        appear in the cached history — a familiarity signal for the
        contract-identity row. Cache-only (no network), so it's a LOWER
        BOUND: only as deep as the history that's been loaded. Deduplicated
        by tx hash in case two of the user's accounts both cache the tx."""
        target = (contract or "").lower()
        if not target:
            return 0
        mine = {a.lower() for a in addresses}
        seen: set[str] = set()
        for addr in mine:
            for t in self.load(chain_id, addr) or []:
                if ((t.to_addr or "").lower() == target
                        and t.from_addr.lower() in mine):
                    seen.add(t.hash)
        return len(seen)
