"""Disk-backed cache for past transactions, keyed by (chain, address).

Confirmed transactions are immutable: a hash always points to the same
data. That lets us cache aggressively across runs — the plugin loads
the cached page immediately on selection so the user never sees an
empty → populated flicker while the background fetch runs.

Layout mirrors qeth.plugins.tokens.wallet_cache:
    CACHE_DIR / <chain_id> / <address_lower>.json
each file holds a JSON list of Transaction dicts (newest-first).
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .fsatomic import atomic_write_text
from .transactions import Transaction

log = logging.getLogger(__name__)

# Busy wallets cache 10k+ txs (a 0x7a-style account is ~13 MB of JSON, 2/3 of it
# raw calldata). Serializing that on the MAIN thread while scrolling stuttered
# the UI. ``ujson`` (C) roughly halves the encode/decode vs stdlib; it's
# arbitrary-precision-int correct (verified on 2**256-1), which orjson is NOT —
# orjson rejects any int past 64 bits, and ``value_wei`` is a uint256. Optional:
# a from-source / distro build may lack it, so fall back to stdlib json (same
# on-disk bytes either way, so caches stay cross-compatible).
try:
    import ujson
except ImportError:  # pragma: no cover - forced by the fallback test
    ujson = None  # type: ignore[assignment]


def _dumps(rows: list[dict]) -> str:
    if ujson is not None:
        return ujson.dumps(rows)                          # compact by default
    return json.dumps(rows, separators=(",", ":"))


def _loads(raw: bytes):
    return (ujson or json).loads(raw)


CACHE_DIR = Path.home() / ".qeth" / "transactions"

# ERC-20 selectors whose recipient we can read straight from calldata.
_TRANSFER = "0xa9059cbb"      # transfer(address,uint256)         → arg0
_TRANSFER_FROM = "0x23b872dd"  # transferFrom(address,address,uint256) → arg1
# approve(address,uint256) / increaseAllowance(address,uint256): arg0 = spender.
_APPROVE_SELECTORS = ("0x095ea7b3", "0x39509351")


def _erc20_transfer_recipient(data: str) -> str | None:
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
    merged.sort(key=lambda t: t.order_key, reverse=True)
    return merged


class TransactionCache:
    """Tiny key-value store over the filesystem. Replace-on-write
    (no merging) is fine for now — each fetch returns the top N
    newest txs, so the saved file always represents the most recent
    window. A paginated-history feature can add a merge step later."""

    def __init__(self, root: Path | None = None):
        # Look up CACHE_DIR at instantiation so tests that monkeypatch
        # the module-level constant (via the tmp_qeth fixture) see the
        # redirected path without having to construct with an explicit root.
        self.root = root if root is not None else CACHE_DIR

    def _path(self, chain_id: int, address: str) -> Path:
        return self.root / str(chain_id) / f"{address.lower()}.json"

    def load(self, chain_id: int, address: str) -> list[Transaction] | None:
        p = self._path(chain_id, address)
        if not p.exists():
            return None
        try:
            data = _loads(p.read_bytes())
        except (OSError, ValueError):     # ValueError covers json + ujson decode errors
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
        # ``vars(tx)`` not ``asdict(tx)``: Transaction is a flat frozen dataclass
        # (scalar fields only), so its ``__dict__`` IS the serializable form.
        # asdict() recursively DEEP-COPIES every row (~45 ms vs ~1 ms on a 10k-tx
        # cache) for no benefit — that copy was most of the on-scroll UI stutter.
        data = [vars(tx) for tx in txs]
        # No indent — these files can hold 10k+ rows and the on-disk bytes don't
        # need to be human-readable.
        atomic_write_text(p, _dumps(data))

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

    def approvals_to_count(self, chain_id: int, spender: str,
                           addresses) -> int:
        """How many distinct ``approve``/``increaseAllowance`` txs ``addresses``
        sent that granted an allowance to ``spender`` (arg0 of the call) — the
        "have I approved to this spender before" familiarity on an approve's
        Spender: row. An approve's ``to`` is the TOKEN, so this reads the spender
        out of the calldata, not ``to``. Cache-only lower bound, deduped by
        hash."""
        target = (spender or "").lower()
        if not target.startswith("0x") or len(target) != 42:
            return 0
        arg0 = target[2:]                                   # 40 hex, low 20 bytes
        mine = {a.lower() for a in addresses}
        seen: set[str] = set()
        for addr in mine:
            for t in self.load(chain_id, addr) or []:
                data = (t.input_data or "").lower()
                if (t.from_addr.lower() in mine
                        and data[:10] in _APPROVE_SELECTORS
                        and len(data) >= 74
                        and data[34:74] == arg0):
                    seen.add(t.hash)
        return len(seen)


class AsyncTransactionSaver:
    """Persist tx-cache writes OFF the main thread so a big-cache save never
    stalls the UI — even at ~38 ms (ujson) that's a scroll stutter when a
    history walk fetches page after page. Coalesces per (chain, address): a
    burst that touches one view N times does ONE write of the latest snapshot,
    not N. A single worker thread keeps writes ordered — the last snapshot
    submitted is the last written (frozen-dataclass rows are immutable, so the
    snapshot the worker serializes can't change under it)."""

    def __init__(self, write) -> None:
        # write(chain_id, address, txs) — the actual blocking persist. A callable
        # (not a TransactionCache) so a swapped disk cache is read at call time.
        self._write = write
        self._latest: dict[tuple[int, str], list] = {}
        self._lock = threading.Lock()
        self._exec = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="qeth-txsave")
        self._closed = False

    def submit(self, chain_id: int, address: str, txs: list) -> None:
        if self._closed:
            return
        key = (chain_id, address.lower())
        with self._lock:
            scheduled = key in self._latest       # a drain is already queued
            self._latest[key] = txs               # coalesce → newest wins
        if not scheduled:
            try:
                self._exec.submit(self._drain, key)
            except RuntimeError:                  # executor already shutting down
                pass

    def _drain(self, key: tuple[int, str]) -> None:
        with self._lock:
            txs = self._latest.pop(key, None)
        if txs is not None:
            try:
                self._write(key[0], key[1], txs)
            except Exception:
                log.debug("async tx-cache save failed", exc_info=True)

    def flush(self) -> None:
        """Block until every queued write has completed (tests / callers that
        must read the file back). Safe to call repeatedly."""
        try:
            self._exec.submit(lambda: None).result()   # FIFO → all prior done
        except RuntimeError:
            pass

    def close(self) -> None:
        """Drain and stop — call on plugin shutdown so the last write lands."""
        self._closed = True
        self._exec.shutdown(wait=True)
