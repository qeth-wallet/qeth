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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Iterable, Optional, cast

from . import USER_AGENT
from .abi import BlockscoutAbiSource, selector_names
from .abi_cache import AbiCache
from .chains import Chain
from .tokens import BLOCKSCOUT_INSTANCES
from .transactions import Transaction

log = logging.getLogger("qeth.tx_activity")

_APPROVE = "0x095ea7b3"


@dataclass(frozen=True)
class AssetLeg:
    symbol: str
    contract: Optional[str]   # lowercased ERC-20 address; None = native coin


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
    and on ``eth_simulate`` / pyrevm logs alike (both Mappings carrying
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


def _account_rows(base: str, action: str, address: str, timeout: float) -> list[dict]:
    q = urllib.parse.urlencode({
        "module": "account", "action": action, "address": address,
        "page": 1, "offset": 300, "sort": "desc",
    })
    req = urllib.request.Request(
        f"{base.rstrip('/')}/api?{q}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.loads(r.read()).get("result")
    return [row for row in res if isinstance(row, dict)] if isinstance(res, list) else []


class _Verbs:
    """selector → name via each contract's own ABI: disk cache → fetch.
    One ABI per distinct contract, cached forever (verified ABIs don't
    change); unverified contracts get a negative sentinel so we don't
    refetch. Returns None when the contract has no usable ABI."""

    def __init__(self, chain_id: int, source: BlockscoutAbiSource, cache: AbiCache):
        self._chain_id = chain_id
        self._source = source
        self._cache = cache
        self._maps: dict[str, dict[str, str]] = {}

    def name(self, to: Optional[str], selector: str) -> Optional[str]:
        if not to:
            return None
        key = to.lower()
        m = self._maps.get(key)
        if m is None:
            m = self._maps[key] = self._build(key)
        return m.get(selector)

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

    def prewarm(self, contracts: Iterable[Optional[str]]) -> None:
        """Resolve the distinct contracts' ABIs concurrently so the per-tx
        verb lookups don't serialize one slow Blockscout round-trip per
        contract — the difference between ~1 s and tens of seconds before
        any activity shows on a chain's first visit. AbiCache is per-
        contract-file disk I/O with no shared state, so parallel cold
        builds for distinct contracts are safe; maps are stored back on
        this thread after the pool drains."""
        todo = list(dict.fromkeys(
            c.lower() for c in contracts
            if c and c.lower() not in self._maps))
        if len(todo) < 2:
            return
        with ThreadPoolExecutor(max_workers=8) as ex:
            built = list(ex.map(self._build, todo))
        for contract, m in zip(todo, built):
            self._maps[contract] = m


def fetch_activities(
    chain: Chain,
    address: str,
    txs: list[Transaction],
    *,
    timeout: float = 25.0,
    abi_source: Optional[BlockscoutAbiSource] = None,
    abi_cache: Optional[AbiCache] = None,
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

    try:
        transfers = _account_rows(base, "tokentx", address, timeout)
        internals = _account_rows(base, "txlistinternal", address, timeout)
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
    # Warm every distinct callee's ABI in parallel up front, so the verb
    # lookups in the loop below are cache hits instead of one slow
    # Blockscout round-trip each (the "no activities for ages on a chain's
    # first visit" lag).
    verbs.prewarm(tx.to_addr for tx in txs)
    out: dict[str, Activity] = {}

    for tx in txs:
        h = tx.hash.lower()
        out_legs: list[AssetLeg] = []
        in_legs: list[AssetLeg] = []
        seen_out: set[str] = set()
        seen_in: set[str] = set()

        def add(legs: list[AssetLeg], seen: set[str], sym: str, contract: Optional[str]) -> None:
            k = contract or f"native:{sym}"
            if k in seen:
                return
            seen.add(k)
            legs.append(AssetLeg(sym, contract))

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

        sel = (tx.method_id or "").lower()
        if sel in ("", "0x"):           # no calldata → a plain value send
            verb = "send"
        elif tx.to_addr is None:
            verb = "deploy"
        else:
            verb = verbs.name(tx.to_addr, sel) or sel
        muted = not tx.success

        if sel == _APPROVE and tx.to_addr:
            token = tx.to_addr.lower()
            out[tx.hash] = Activity(
                verb, (AssetLeg(sym_of.get(token, "?"), token),), (),
                show_arrow=False, muted=muted)
        else:
            out[tx.hash] = Activity(verb, tuple(out_legs), tuple(in_legs), muted=muted)

    return out
