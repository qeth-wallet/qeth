"""Per-transaction "Activity": the decoded verb plus the assets that
actually moved through the wallet, for the Transactions list.

For a page of transactions this fetches the address's ERC-20 transfers
(``tokentx``) and internal ETH (``txlistinternal``) — two batched calls,
not one-per-tx — folds them with each tx's native ``value`` into a
per-hash ``{out, in}`` of legs that *touch the viewer*, and labels the
call by decoding ``method_id`` against the contract's own ABI (disk-
cached; 4byte/selector only as a fallback).

Qt-free: the Transactions plugin wraps :func:`fetch_activities` in a
worker and renders the result via ``tx_summary.activity_icon`` (the
Activity cell's composited icon).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, cast
from collections.abc import Callable, Iterable

from . import USER_AGENT
from .abi import AnyAbiSource, BlockscoutAbiSource, selector_names
from .abi_cache import AbiCache
from .chains import Chain
from .tokens import BLOCKSCOUT_INSTANCES
from .transactions import Transaction

log = logging.getLogger("qeth.tx_activity")

_APPROVE = "0x095ea7b3"


@dataclass(frozen=True)
class AssetLeg:
    symbol: str
    contract: str | None   # lowercased ERC-20 address; None = native coin


@dataclass(frozen=True)
class Activity:
    verb: str
    out: tuple[AssetLeg, ...] = ()    # assets leaving the wallet
    inn: tuple[AssetLeg, ...] = ()    # assets entering the wallet
    show_arrow: bool = True           # approvals: the approved token, no arrow
    muted: bool = False               # reverted / dropped


# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC0 = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)


def _hexstr(v: object) -> str:
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    s = str(v)
    return s if s.startswith("0x") else "0x" + s


def transfer_legs_from_logs(
    logs: object, viewer: str
) -> tuple[list[str], list[str]]:
    """The ERC-20 contracts the viewer **sent** (out) and **received** (in)
    according to a tx's event logs — works on a confirmed receipt's logs
    and on ``eth_simulate`` / fork-simulation logs alike (both Mappings carrying
    ``topics`` / ``data`` / ``address``). First-seen order, deduped. Native
    ETH is handled separately from the tx value, not here."""
    viewer = viewer.lower()
    out: list[str] = []
    inn: list[str] = []
    seen_out: set[str] = set()
    seen_in: set[str] = set()
    for log in cast(Iterable[Any], logs or []):
        if not hasattr(log, "get"):
            continue
        topics = log.get("topics") or []
        if len(topics) != 3:
            continue
        if _hexstr(topics[0]).lower() != TRANSFER_TOPIC0:
            continue
        raw = log.get("address") or ""
        token = raw.lower() if isinstance(raw, str) else _hexstr(raw).lower()
        if not token or token == "0x":
            continue
        frm = "0x" + _hexstr(topics[1])[-40:]
        to = "0x" + _hexstr(topics[2])[-40:]
        if frm == viewer and token not in seen_out:
            seen_out.add(token)
            out.append(token)
        if to == viewer and token not in seen_in:
            seen_in.add(token)
            inn.append(token)
    return out, inn


_PAGE = 300


def _account_rows(base: str, action: str, address: str, timeout: float, *,
                  startblock: int | None = None,
                  endblock: int | None = None,
                  max_pages: int = 1) -> list[dict]:
    """One Etherscan-style account list (tokentx / txlistinternal). When a
    block range is given, walk pages (newest-first) until the range is
    exhausted or ``max_pages`` is hit — the displayed window can span far
    more than one page of transfers on a busy address, and a single page
    would leave its older txs with no coins."""
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        params: dict[str, object] = {
            "module": "account", "action": action, "address": address,
            "page": page, "offset": _PAGE, "sort": "desc",
        }
        if startblock is not None:
            params["startblock"] = startblock
        if endblock is not None:
            params["endblock"] = endblock
        q = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"{base.rstrip('/')}/api?{q}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read()).get("result")
        batch = ([row for row in res if isinstance(row, dict)]
                 if isinstance(res, list) else [])
        rows.extend(batch)
        if len(batch) < _PAGE:          # last page of the range
            break
    return rows


class _Verbs:
    """selector → name via each contract's own ABI: disk cache → fetch.
    One ABI per distinct contract, cached forever (verified ABIs don't
    change); unverified contracts get a negative sentinel so we don't
    refetch. Returns None when the contract has no usable ABI."""

    def __init__(self, chain_id: int, source: AnyAbiSource, cache: AbiCache):
        self._chain_id = chain_id
        self._source = source
        self._cache = cache
        self._maps: dict[str, dict[str, str]] = {}

    def name(self, to: str | None, selector: str, *,
             fetch: bool = True) -> str | None:
        if not to:
            return None
        key = to.lower()
        m = self._maps.get(key)
        if m is None:
            if fetch:
                m = self._maps[key] = self._build(key)
            else:
                # Cache-only: warm from disk if already known, but never hit
                # the network here — cold callees are resolved in parallel
                # afterwards. Returns None so the caller can mark it cold.
                cached = self._cache.load(self._chain_id, key)
                if cached is None:
                    return None
                m = self._maps[key] = (
                    selector_names(cached) if isinstance(cached, list) else {})
        return m.get(selector)

    def resolve(self, contract: str) -> dict[str, str]:
        """Force-build (network) and store a single contract's selector map.
        Used as the unit of work for the parallel cold-callee resolve."""
        m = self._build(contract)
        self._maps[contract] = m
        return m

    def _build(self, contract: str) -> dict[str, str]:
        abi = self._cache.load(self._chain_id, contract)
        if abi is None:                                  # cold: fetch once
            try:
                fetched = self._source.fetch(self._chain_id, contract)
            except Exception as e:                       # transient — don't poison the cache
                log.debug("abi fetch failed for %s: %s", contract, e)
                return {}
            self._cache.save(self._chain_id, contract, fetched)
            abi = fetched
        return selector_names(abi) if isinstance(abi, list) else {}


def _coins(tx: Transaction, viewer: str, native: str,
           tok_by_hash: dict, int_by_hash: dict
           ) -> tuple[list[AssetLeg], list[AssetLeg]]:
    """The viewer-touching assets a tx moved: native value + ERC-20
    transfers (tokentx) + internal native (txlistinternal), deduped."""
    out_legs: list[AssetLeg] = []
    in_legs: list[AssetLeg] = []
    seen_out: set[str] = set()
    seen_in: set[str] = set()

    def add(legs: list[AssetLeg], seen: set[str], sym: str,
            contract: str | None) -> None:
        k = contract or f"native:{sym}"
        if k in seen:
            return
        seen.add(k)
        legs.append(AssetLeg(sym, contract))

    h = tx.hash.lower()
    if tx.value_wei > 0:
        if tx.from_addr == viewer:
            add(out_legs, seen_out, native, None)
        elif (tx.to_addr or "") == viewer:
            add(in_legs, seen_in, native, None)
    for t in tok_by_hash.get(h, []):
        c = str(t.get("contractAddress") or "").lower()
        sym = str(t.get("tokenSymbol") or "?")
        if str(t.get("from") or "").lower() == viewer:
            add(out_legs, seen_out, sym, c)
        if str(t.get("to") or "").lower() == viewer:
            add(in_legs, seen_in, sym, c)
    for it in int_by_hash.get(h, []):
        try:
            if int(it.get("value") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        if str(it.get("from") or "").lower() == viewer:
            add(out_legs, seen_out, native, None)
        if str(it.get("to") or "").lower() == viewer:
            add(in_legs, seen_in, native, None)
    return out_legs, in_legs


def _make_activity(verb: str, out_legs: list[AssetLeg],
                   in_legs: list[AssetLeg], sel: str, tx: Transaction,
                   sym_of: dict[str, str]) -> Activity:
    muted = not tx.success
    if sel == _APPROVE and tx.to_addr:           # show only the approved token
        token = tx.to_addr.lower()
        return Activity(verb, (AssetLeg(sym_of.get(token, "?"), token),),
                        (), show_arrow=False, muted=muted)
    return Activity(verb, tuple(out_legs), tuple(in_legs), muted=muted)


def fetch_activities(
    chain: Chain,
    address: str,
    txs: list[Transaction],
    *,
    timeout: float = 25.0,
    abi_source: AnyAbiSource | None = None,
    abi_cache: AbiCache | None = None,
    on_batch: Callable[[dict[str, Activity]], None] | None = None,
) -> dict[str, Activity]:
    """Build ``{tx_hash: Activity}`` for ``txs``. Best-effort: a failed
    transfers/internal fetch yields verb-only activities (still useful);
    chains without a Blockscout instance yield ``{}`` (the list falls back
    to showing the hash)."""
    base = BLOCKSCOUT_INSTANCES.get(chain.chain_id)
    if base is None:
        return {}
    viewer = address.lower()
    native = chain.symbol or "ETH"

    # Scope the transfer/internal fetch to the block span of the txs we're
    # actually showing (paged), not just the most-recent 300 transfers for
    # the whole address — on a busy wallet the displayed window reaches well
    # past one page, leaving its older txs coinless.
    blocks = [tx.block_number for tx in txs if tx.block_number]
    sb = min(blocks) if blocks else None
    eb = max(blocks) if blocks else None
    try:
        transfers = _account_rows(base, "tokentx", address, timeout,
                                  startblock=sb, endblock=eb, max_pages=12)
        internals = _account_rows(base, "txlistinternal", address, timeout,
                                  startblock=sb, endblock=eb, max_pages=12)
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.debug("activity fetch failed on %s: %s", chain.name, e)
        transfers, internals = [], []

    tok_by_hash: dict[str, list[dict]] = defaultdict(list)
    sym_of: dict[str, str] = {}
    for t in transfers:
        h = str(t.get("hash") or "").lower()
        if h:
            tok_by_hash[h].append(t)
        c = str(t.get("contractAddress") or "").lower()
        if c:
            sym_of[c] = str(t.get("tokenSymbol") or "?")
    int_by_hash: dict[str, list[dict]] = defaultdict(list)
    for it in internals:
        h = str(it.get("transactionHash") or it.get("hash") or "").lower()
        if h:
            int_by_hash[h].append(it)

    verbs = _Verbs(chain.chain_id, abi_source or BlockscoutAbiSource(),
                   abi_cache if abi_cache is not None else AbiCache())
    out: dict[str, Activity] = {}
    by_hash = {tx.hash.lower(): tx for tx in txs}
    coins: dict[str, tuple[list[AssetLeg], list[AssetLeg], str]] = {}
    cold: dict[str, list[str]] = defaultdict(list)   # cold callee → tx hashes

    # Pass 1 — coins (one fast tokentx/internal batch) plus the verbs we
    # already know: send / deploy / approve, or a callee whose ABI is
    # cached. A callee whose ABI isn't cached yet shows its bare selector
    # as a placeholder and is queued for the parallel resolve below.
    for tx in txs:
        h = tx.hash.lower()
        out_legs, in_legs = _coins(tx, viewer, native, tok_by_hash, int_by_hash)
        sel = (tx.method_id or "").lower()
        coins[h] = (out_legs, in_legs, sel)
        if sel in ("", "0x"):           # no calldata → a plain value send
            verb = "send"
        elif tx.to_addr is None:
            verb = "deploy"
        else:
            name = verbs.name(tx.to_addr, sel, fetch=False)
            if name is None:            # cold ABI — placeholder + queue
                verb = sel
                cold[tx.to_addr.lower()].append(h)
            else:
                verb = name
        out[tx.hash] = _make_activity(verb, out_legs, in_legs, sel, tx, sym_of)

    if on_batch:
        # Emit only the already-final rows now; the cold ones follow once
        # their ABI lands (below). This keeps every emitted/cached activity
        # final — a placeholder-selector row is never persisted, so a reload
        # mid-resolve can't freeze a verified contract on its bare selector.
        cold_hashes = {h for hs in cold.values() for h in hs}
        first = {tx.hash: out[tx.hash] for tx in txs
                 if tx.hash.lower() not in cold_hashes}
        if first:
            on_batch(first)

    # Pass 2 — fetch the cold callees' ABIs 8-wide; as each lands, refine
    # its txs' verbs and emit just those rows, so the column fills in
    # progressively on a chain's first visit instead of after one long
    # blank wait. (Runs even without on_batch so the returned dict is
    # always fully resolved — that path just doesn't emit.)
    if cold:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(verbs.resolve, c): c for c in cold}
            for fut in as_completed(futs):
                m = fut.result()
                batch: dict[str, Activity] = {}
                for h in cold[futs[fut]]:
                    tx = by_hash[h]
                    ol, il, sel = coins[h]
                    act = _make_activity(m.get(sel) or sel, ol, il, sel, tx, sym_of)
                    out[tx.hash] = act
                    batch[tx.hash] = act
                if on_batch:
                    on_batch(batch)

    return out
