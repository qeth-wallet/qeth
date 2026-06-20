"""Chainlist registry lookup (chainid.network).

Backs the chain-RPC dialog with a list of public RPC endpoints
for any given chain. The data comes from the open-source
``ethereum-lists/chains`` repo via ``https://chainid.network/chains.json``
(also what chainlist.org renders); each entry carries the chainId,
canonical name, native currency, and an ``rpc`` array of public
endpoints with ``tracking`` / ``isOpenSource`` annotations the
user can use to pick a privacy-respecting endpoint.

Disk-cached at ``~/.qeth/chainlist/chains.json`` with a 7-day TTL
— the registry changes slowly and a stale entry is far better
than blocking the UI on a 3 MB JSON download every time the
dialog opens.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import USER_AGENT
from .fsatomic import atomic_write_bytes


log = logging.getLogger("qeth.chainlist")


CHAINS_URL = "https://chainid.network/chains.json"
CACHE_DIR = Path.home() / ".qeth" / "chainlist"
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_TIMEOUT = 20.0


@dataclass
class RpcEntry:
    """One public endpoint for a chain. ``tracking`` is the
    schema-documented value: ``"none"``, ``"limited"``, ``"yes"``,
    or ``"unspecified"``. ``is_open_source`` flags whether the
    provider's full implementation is open. Both come from
    chainlist's per-RPC metadata; consumers can present these
    however they like (we render tracking as a coloured chip)."""
    url: str
    tracking: str
    is_open_source: bool


@dataclass
class ChainEntry:
    chain_id: int
    name: str
    short_name: str
    native_symbol: str
    rpcs: list[RpcEntry]


def fetch_chains(
    *, force: bool = False,
    cache_dir: Path | None = None,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[ChainEntry]:
    """Return every chain in the registry. Reads from disk cache
    when fresh; refetches from chainid.network when stale or
    ``force=True``. Falls back to the cached copy on network
    failure; returns ``[]`` if nothing is cached and the network
    is also unreachable."""
    cache = cache_dir if cache_dir is not None else CACHE_DIR
    cache_file = cache / "chains.json"
    fresh = (
        cache_file.exists()
        and (time.time() - cache_file.stat().st_mtime) < ttl_seconds
    )
    if not fresh or force:
        try:
            req = urllib.request.Request(
                CHAINS_URL,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            atomic_write_bytes(cache_file, data)
        except Exception as e:
            log.warning("chainlist fetch failed: %s", e)
            if not cache_file.exists():
                return []
    try:
        raw = json.loads(cache_file.read_text())
    except Exception as e:
        log.warning("chainlist parse failed: %s", e)
        return []
    out: list[ChainEntry] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        cid = c.get("chainId")
        if not isinstance(cid, int):
            continue
        rpcs: list[RpcEntry] = []
        for r in c.get("rpc") or []:
            url: str | None
            tracking = "unspecified"
            is_open_source = False
            if isinstance(r, str):
                # Most chainlist entries today are bare URL
                # strings — annotations aren't part of the
                # current chains.json schema.
                url = r
            elif isinstance(r, dict):
                url = r.get("url")
                tracking = str(r.get("tracking") or "unspecified")
                is_open_source = bool(r.get("isOpenSource") or False)
            else:
                continue
            if not isinstance(url, str) or not url:
                continue
            # Skip ${VARIABLE}-templated URLs (Infura, Alchemy
            # API-key placeholders, …) — they're not usable
            # without a key, and the user wouldn't get one by
            # accident from this picker.
            if "${" in url:
                continue
            rpcs.append(RpcEntry(
                url=url, tracking=tracking,
                is_open_source=is_open_source,
            ))
        out.append(ChainEntry(
            chain_id=cid,
            name=str(c.get("name") or ""),
            short_name=str(c.get("shortName") or ""),
            native_symbol=str((c.get("nativeCurrency") or {}).get("symbol", "")),
            rpcs=rpcs,
        ))
    return out


def probe_rpc(
    url: str, expected_chain_id: int, *,
    timeout: float = 5.0,
) -> tuple[bool, float | None, str | None]:
    """Send a single ``eth_chainId`` JSON-RPC and time the
    round-trip. Returns ``(ok, latency_ms, reason)``:

    - ``ok=True`` only when the response is a well-formed JSON-RPC
      reply whose ``chainId`` matches ``expected_chain_id``. That
      catches both "requires registration" (auth error / HTML
      stub) and "wrong chain behind this URL".
    - ``ok=False`` returns ``reason`` as a short error string for
      debug logging; the dialog discards the row either way.
    - ``latency_ms`` is the wall-clock time from request send to
      response receipt, ``None`` when the request itself failed
      (DNS, TCP, TLS, timeout).

    HTTP only — WebSocket probes need a real WS handshake, so
    ``wss://`` URLs aren't supported here and qeth can't use WS
    as a chain RPC anyway (EthClient is HTTPProvider-only)."""
    if not url.startswith(("http://", "https://")):
        return False, None, "not http"
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_chainId", "params": [],
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except Exception as e:
        return False, None, str(e)[:120]
    latency = (time.perf_counter() - t0) * 1000.0
    try:
        obj = json.loads(data)
    except Exception:
        return False, latency, "non-JSON response"
    if not isinstance(obj, dict):
        return False, latency, "non-object response"
    if obj.get("error"):
        err = obj["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return False, latency, f"rpc error: {msg}"[:120]
    result = obj.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        return False, latency, "bad result"
    try:
        cid = int(result, 16)
    except ValueError:
        return False, latency, "bad chainid"
    if cid != int(expected_chain_id):
        return False, latency, f"chain mismatch ({cid})"
    return True, latency, None


_DEAD = "0x000000000000000000000000000000000000dEaD"

# Error-message needles that mean "this endpoint doesn't implement the
# method" (vs. a transient rate-limit/odd error, which stays inconclusive).
_METHOD_MISSING_NEEDLES = (
    "not found", "not support", "not available", "unsupport",
    "not whitelisted", "does not exist", "disabled",
)


def _probe_method(url: str, method: str, params: list,
                  timeout: float) -> bool | None:
    """POST one JSON-RPC request and classify the endpoint's support for
    ``method``:

    - ``True``  — the endpoint ran the call and returned a result.
    - ``False`` — a definitive ``-32601`` / method-not-found.
    - ``None``  — inconclusive (unreachable, rate-limited, odd error):
      shown as 'unknown' rather than a wrong ✗.
    """
    if not url.startswith(("http://", "https://")):
        return None
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            obj = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # A JSON-RPC -32601 often rides on an HTTP 4xx; read the body.
        try:
            obj = json.loads(e.read())
        except Exception:
            return None
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if "result" in obj:
        return True
    err = obj.get("error") or {}
    code = err.get("code") if isinstance(err, dict) else None
    msg = (err.get("message") if isinstance(err, dict) else "") or ""
    m = msg.lower()
    if code == -32601 or any(n in m for n in _METHOD_MISSING_NEEDLES):
        return False
    return None   # rate-limit / other error → unknown


def probe_simulate_v1(url: str, *, timeout: float = 5.0) -> bool | None:
    """Probe whether ``url`` implements ``eth_simulateV1`` — the one-call
    simulation method qeth's event preview prefers (much faster than the
    local fork). Tri-state, see ``_probe_method`` (definitive ✗ examples:
    cloudflare-eth, DRPC's Arbitrum, the zkSync gateways).

    A throwaway read-only simulation (a 1-wei self-send), so it's safe to
    fire at arbitrary public endpoints."""
    return _probe_method(url, "eth_simulateV1", [{
        "blockStateCalls": [{"calls": [
            {"from": _DEAD, "to": _DEAD, "value": "0x1"}]}],
        "traceTransfers": False, "validation": False,
    }, "latest"], timeout)


def probe_access_list(url: str, *, timeout: float = 5.0) -> bool | None:
    """Probe whether ``url`` implements ``eth_createAccessList`` — the
    "which slots will this call touch" hint a verifying client (Helios)
    needs from an untrusted RPC. Tri-state, see ``_probe_method``.

    The probe shape matters (learned sweeping the chainid.network mainnet
    list, 2026-06-12): send **no fee fields** — an explicit ``gasPrice``
    of ``0x0`` is rejected by geth-style nodes ("gasprice must be non-zero
    after london fork"), and a *priced* probe makes balance validation
    bite on some nodes (publicnode) — both read as false negatives. A
    zero-value self-send from the burn address with a small explicit gas
    cap (so nodes that default the cap to the block limit don't price the
    probe beyond the burn address's balance) answers cleanly on every
    live mainnet endpoint."""
    return _probe_method(url, "eth_createAccessList", [
        {"from": _DEAD, "to": _DEAD, "gas": "0x186a0"}, "latest",
    ], timeout)


def lookup(
    chain_id: int, *,
    force_refresh: bool = False,
    cache_dir: Path | None = None,
) -> ChainEntry | None:
    """Convenience: return the ``ChainEntry`` for a specific
    chain id (or ``None``). Caches the full registry on the
    first call."""
    for entry in fetch_chains(force=force_refresh, cache_dir=cache_dir):
        if entry.chain_id == int(chain_id):
            return entry
    return None
