"""Per-transaction "Activity": the decoded verb plus the assets that
actually moved through the wallet, for the Transactions list.

For a page of transactions this fetches the address's ERC-20 transfers
(``tokentx``) and internal ETH (``txlistinternal``) — two batched calls,
not one-per-tx — folds them with each tx's native ``value`` into a
per-hash ``{out, in}`` of legs that *touch the viewer*, and labels the
call by decoding ``method_id`` against the contract's own ABI (disk-
cached; 4byte/selector only as a fallback).

Qt-free: the Transactions plugin wraps :func:`fetch_activities` in a
worker and renders the result with ``tx_summary.paint_summary``, from a
delegate — the Activity cell is drawn, not an icon (an item-view QIcon gets
rescaled per row).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast
from collections.abc import Callable, Iterable, Mapping

from . import USER_AGENT
from .abi import AnyAbiSource, BlockscoutAbiSource, selector_names
from .abi_cache import AbiCache
from .address import tron_from_hex, tron_to_hex
from .chains import DEFAULT_CHAINS, TRON, Chain
from .token_discovery import BLOCKSCOUT_INSTANCES
from .transactions import Transaction
from .tron.client import TronClient, TronError

if TYPE_CHECKING:
    from .chain import EthClient

log = logging.getLogger("qeth.tx_activity")

_APPROVE = "0x095ea7b3"
_TRANSFER = "0xa9059cbb"          # ERC-20 transfer(address,uint256)
_TRANSFERFROM = "0x23b872dd"      # ERC-20 transferFrom(address,address,uint256)


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


@dataclass(frozen=True)
class TransferRow:
    """One ERC-20 Transfer touching the viewer — the notification path's view of
    a receipt/ws log (counterparty + value + where it sits, for dedup)."""
    token: str            # lowercased ERC-20 address
    counterparty: str     # lowercased other party
    outgoing: bool
    value: int
    tx_hash: str          # lowercased 0x hash of the parent tx
    log_index: int | None


def transfers_touching(logs: object, viewer: str) -> list[TransferRow]:
    """Every ERC-20 Transfer in ``logs`` that ``viewer`` sent or received, with
    the counterparty / value / (tx, log-index) — so the notification path can
    surface an arrival from a confirmed tx's receipt and dedup it against the ws
    Transfer-log watcher. Same Mapping shape as ``transfer_legs_from_logs``;
    malformed logs are skipped."""
    viewer = viewer.lower()
    rows: list[TransferRow] = []
    for log in cast(Iterable[Any], logs or []):
        if not hasattr(log, "get"):
            continue
        topics = log.get("topics") or []
        if len(topics) != 3 or _hexstr(topics[0]).lower() != TRANSFER_TOPIC0:
            continue
        raw = log.get("address") or ""
        token = raw.lower() if isinstance(raw, str) else _hexstr(raw).lower()
        if not token or token == "0x":
            continue
        frm = "0x" + _hexstr(topics[1])[-40:]
        to = "0x" + _hexstr(topics[2])[-40:]
        outgoing = frm == viewer
        if not outgoing and to != viewer:
            continue
        try:
            value = int(_hexstr(log.get("data")), 16)
        except (ValueError, TypeError):
            value = 0
        li = log.get("logIndex")
        try:
            log_index = (int(li, 16) if isinstance(li, str)
                         else int(li) if li is not None else None)
        except (ValueError, TypeError):
            log_index = None
        rows.append(TransferRow(
            token, to if outgoing else frm, outgoing, value,
            _hexstr(log.get("transactionHash")).lower(), log_index))
    return rows


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


def _hex_wei(v: object) -> int:
    """A callTracer ``value`` (a 0x-quantity string; some nodes send an int)."""
    if isinstance(v, int):
        return v
    s = str(v or "0")
    try:
        return int(s, 16) if s.startswith("0x") else int(s or "0")
    except ValueError:
        return 0


def native_in_from_trace(node: object, viewer: str) -> int:
    """Sum the wei that internal calls in a ``callTracer`` frame send **to**
    ``viewer`` (recursively). ``viewer`` must be lower-case. A reverted frame
    moved nothing, so its whole subtree is skipped; the top-level call is the
    viewer's own outbound call (``to`` = the contract), so it never matches."""
    if not isinstance(node, Mapping) or node.get("error"):
        return 0
    total = 0
    if (str(node.get("to") or "").lower() == viewer
            and str(node.get("from") or "").lower() != viewer):
        total += _hex_wei(node.get("value"))
    subs = node.get("calls")
    if isinstance(subs, list):
        for sub in subs:
            total += native_in_from_trace(sub, viewer)
    return total


def _trace_capable_chain(chain: Chain) -> Chain:
    """``chain`` with a trace-capable RPC preferred. The user's configured RPC
    may not whitelist ``debug_traceTransaction`` — the official
    ``mainnet.optimism.io`` answers ``-32601 "rpc method is not whitelisted"``,
    and the failover provider treats that as a real answer (it doesn't rotate),
    so the internal-ETH leg would silently never resolve. Prepend the bundled
    default endpoints for this chain (DRPC, which does expose the tracer) ahead
    of the user's, so the trace lands on a capable node first and only falls
    back to the user's RPC on a transport failure. Chains with no bundled
    default (added via chainlist) keep the user's RPC — best-effort there."""
    default = next((c for c in DEFAULT_CHAINS if c.chain_id == chain.chain_id), None)
    urls: list[str] = []
    if default is not None:
        urls += [default.rpc_url, *default.fallback_rpcs]
    urls += [chain.rpc_url, *chain.fallback_rpcs]
    ordered = list(dict.fromkeys(u for u in urls if u))
    if not ordered:
        return chain
    return replace(chain, rpc_url=ordered[0], fallback_rpcs=tuple(ordered[1:]))


def _trace_native_in(client: EthClient, tx_hash: str, viewer: str) -> int:
    """Native wei ``viewer`` received via internal calls in ``tx_hash``, read
    from the node's ``callTracer``. Best-effort: any RPC/parse failure (the
    endpoint doesn't expose ``debug_traceTransaction``, a transient error, an
    unexpected shape) returns 0, so the native-in leg simply isn't added."""
    try:
        trace = client.rpc("debug_traceTransaction",
                           [tx_hash, {"tracer": "callTracer"}])
    except Exception as e:            # unsupported / transient — best-effort
        log.debug("trace fallback failed for %s: %s", tx_hash, e)
        return 0
    return native_in_from_trace(trace, viewer)


def _wants_native_trace(out_legs: list[AssetLeg], in_legs: list[AssetLeg],
                        sel: str, tx: Transaction) -> bool:
    """A one-sided contract call where the viewer handed over an ERC-20 and
    got nothing back — very likely a TOKEN->native swap whose received ETH is
    missing (a WETH unwrap credits native ETH by an internal tx, and
    Blockscout's internal-tx index lags: minutes on mainnet, hours-to-never on
    L2s). Worth a node trace to recover the native-in leg. A tx that already
    shows an incoming leg (token->token, ETH->token), a plain transfer, or an
    approve is skipped, so token->token swaps and sends never trigger a trace."""
    if not tx.success or in_legs:
        return False
    if sel in (_APPROVE, _TRANSFER, _TRANSFERFROM):
        return False
    return any(leg.contract is not None for leg in out_legs)


def _make_activity(verb: str, out_legs: list[AssetLeg],
                   in_legs: list[AssetLeg], sel: str, tx: Transaction,
                   sym_of: dict[str, str]) -> Activity:
    muted = not tx.success
    if sel == _APPROVE and tx.to_addr:           # show only the approved token
        token = tx.to_addr.lower()
        return Activity(verb, (AssetLeg(sym_of.get(token, "?"), token),),
                        (), show_arrow=False, muted=muted)
    return Activity(verb, tuple(out_legs), tuple(in_legs), muted=muted)


# Tron has no ABI source, so contract calls get these few names; anything
# else shows its selector.
_TRON_VERBS = {"0xa9059cbb": "transfer", "0x095ea7b3": "approve",
               "0x23b872dd": "transferFrom"}
# Pages (≤200 rows each) of TRC-20 transfers read per window.
_TRON_TRC20_PAGES = 5


def _tron_transfer_rows(chain: Chain, address: str, txs: list[Transaction],
                        timeout: float) -> list[dict]:
    """TRC-20 transfers touching ``address`` within the time span of ``txs``
    (TronGrid's ``/transactions/trc20`` — Tron's ``tokentx``), reshaped into
    the explorers' tokentx rows (hex addresses) so ``_coins`` reads them."""
    stamps = [t.timestamp for t in txs if t.timestamp]
    if not stamps:
        return []
    client = TronClient(chain, timeout=timeout)
    path = f"/v1/accounts/{tron_from_hex(address)}/transactions/trc20"
    params: dict[str, Any] = {"limit": 200, "min_timestamp": min(stamps) * 1000,
                              "max_timestamp": max(stamps) * 1000 + 999}
    rows: list[dict] = []
    for _ in range(_TRON_TRC20_PAGES):
        resp = client.index_get(path, params)
        for r in resp.get("data") or []:
            info = r.get("token_info") or {}
            contract = tron_to_hex(str(info.get("address") or ""))
            if contract is None:
                continue
            rows.append({
                "hash": "0x" + str(r.get("transaction_id") or "").lower(),
                "contractAddress": contract.lower(),
                "tokenSymbol": str(info.get("symbol") or "?"),
                "from": (tron_to_hex(str(r.get("from") or "")) or "").lower(),
                "to": (tron_to_hex(str(r.get("to") or "")) or "").lower(),
            })
        fingerprint = (resp.get("meta") or {}).get("fingerprint")
        if not fingerprint:
            break
        params = {**params, "fingerprint": fingerprint}
    return rows


def _tron_activities(chain: Chain, address: str, txs: list[Transaction], *,
                     timeout: float,
                     on_batch: Callable[[dict[str, Activity]], None] | None,
                     ) -> dict[str, Activity]:
    """``fetch_activities`` for Tron: verbs from the method selector, coins
    from the tx's own TRX value + TronGrid's TRC-20 transfer index."""
    viewer = address.lower()
    try:
        transfers = _tron_transfer_rows(chain, address, txs, timeout)
    except TronError as e:
        log.debug("tron activity fetch failed: %s", e)
        transfers = []
    tok_by_hash: dict[str, list[dict]] = defaultdict(list)
    sym_of: dict[str, str] = {}
    for t in transfers:
        tok_by_hash[t["hash"]].append(t)
        sym_of[t["contractAddress"]] = t["tokenSymbol"]
    out: dict[str, Activity] = {}
    for tx in txs:
        sel = (tx.method_id or "").lower()
        verb = "send" if sel in ("", "0x") else _TRON_VERBS.get(sel, sel)
        out_legs, in_legs = _coins(tx, viewer, chain.symbol or "TRX", tok_by_hash, {})
        out[tx.hash] = _make_activity(verb, out_legs, in_legs, sel, tx, sym_of)
    if on_batch and out:
        on_batch(out)
    return out


def fetch_activities(
    chain: Chain,
    address: str,
    txs: list[Transaction],
    *,
    timeout: float = 25.0,
    abi_source: AnyAbiSource | None = None,
    abi_cache: AbiCache | None = None,
    on_batch: Callable[[dict[str, Activity]], None] | None = None,
    trace_native_in: Callable[[str], int] | None = None,
) -> dict[str, Activity]:
    """Build ``{tx_hash: Activity}`` for ``txs``. Best-effort: a failed
    transfers/internal fetch yields verb-only activities (still useful);
    chains without a Blockscout instance yield ``{}`` (the list falls back
    to showing the hash).

    ``trace_native_in(tx_hash) -> wei`` recovers the received-ETH leg of a
    one-sided TOKEN->native swap that Blockscout's internal-tx index hasn't
    indexed (see :func:`_wants_native_trace`); it defaults to a node
    ``callTracer`` read over the chain's RPC and is injectable for tests."""
    if chain.family == TRON:
        return _tron_activities(chain, address, txs, timeout=timeout,
                                on_batch=on_batch)
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

    verbs = _Verbs(chain.chain_id, abi_source or BlockscoutAbiSource(),
                   abi_cache if abi_cache is not None else AbiCache())

    def quick_verb(tx: Transaction) -> tuple[str, bool]:
        """(verb, is_cold) from no-network sources only: send / deploy, or a
        callee whose ABI is already on disk. is_cold → the bare selector, ABI
        fetched in pass 2."""
        sel = (tx.method_id or "").lower()
        if sel in ("", "0x"):
            return "send", False
        if tx.to_addr is None:
            return "deploy", False
        name = verbs.name(tx.to_addr, sel, fetch=False)
        return (sel, True) if name is None else (name, False)

    # Pass 0 — verbs ONLY, before the (possibly slow) tokentx/internal fetch.
    # The method label needs just the contract's ABI, which a freshly-signed tx
    # already has cached, so emit it immediately — otherwise a just-created
    # pending tx shows a blank method until the coins network round-trip
    # returns (minutes on a flaky link). Coins fill in via pass 1 below.
    if on_batch:
        early = {tx.hash: _make_activity(v, [], [], (tx.method_id or "").lower(),
                                         tx, {})
                 for tx in txs
                 for v, is_cold in [quick_verb(tx)] if not is_cold}
        if early:
            on_batch(early)

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

    out: dict[str, Activity] = {}
    by_hash = {tx.hash.lower(): tx for tx in txs}
    coins: dict[str, tuple[list[AssetLeg], list[AssetLeg], str]] = {}
    cold: dict[str, list[str]] = defaultdict(list)   # cold callee → tx hashes

    # Pass 1 — coins (one fast tokentx/internal batch) plus the verbs we
    # already know (pass 0 re-derives the same way). A callee whose ABI isn't
    # cached yet shows its bare selector as a placeholder and is queued for the
    # parallel resolve below.
    for tx in txs:
        h = tx.hash.lower()
        out_legs, in_legs = _coins(tx, viewer, native, tok_by_hash, int_by_hash)
        sel = (tx.method_id or "").lower()
        coins[h] = (out_legs, in_legs, sel)
        verb, is_cold = quick_verb(tx)
        if is_cold and tx.to_addr is not None:    # placeholder + queue the fetch
            cold[tx.to_addr.lower()].append(h)
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

    # Pass 3 — native-in fallback via node trace. A TOKEN->native swap unwraps
    # WETH and receives the ETH by an internal tx; Blockscout's internal-tx
    # index lags that (minutes on mainnet, and on L2s the row can stay
    # one-sided indefinitely — status 2 "not yet processed"), so the swap shows
    # "OP ->" with nothing in. For those rows read the internal ETH transfers
    # to the viewer straight from the node's callTracer and add the native-in
    # leg, so it reads "OP -> ETH". Bounded: only one-sided ERC-20-out swaps.
    targets = [tx for tx in txs if _wants_native_trace(*coins[tx.hash.lower()], tx)]
    if targets:
        fetch = trace_native_in
        if fetch is None:
            try:
                from .chain import EthClient
                client = EthClient(_trace_capable_chain(chain), timeout=timeout)
                fetch = lambda h: _trace_native_in(client, h, viewer)  # a closure
            except Exception as e:            # web3 missing / bad RPC — skip
                log.debug("trace client unavailable on %s: %s", chain.name, e)
                fetch = None
        if fetch is not None:
            do_fetch = fetch
            with ThreadPoolExecutor(max_workers=8) as ex:
                tfuts = {ex.submit(do_fetch, tgt.hash): tgt for tgt in targets}
                tbatch: dict[str, Activity] = {}
                for tf in as_completed(tfuts):
                    tgt = tfuts[tf]
                    try:
                        wei = tf.result()
                    except Exception:         # best-effort — no leg on failure
                        wei = 0
                    if wei > 0:
                        a = out[tgt.hash]
                        a = Activity(a.verb, a.out, a.inn + (AssetLeg(native, None),),
                                     show_arrow=a.show_arrow, muted=a.muted)
                        out[tgt.hash] = a
                        tbatch[tgt.hash] = a
                if tbatch and on_batch:
                    on_batch(tbatch)

    return out
