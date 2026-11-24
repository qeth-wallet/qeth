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
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import requests

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


class ChainlistUnavailable(Exception):
    """The registry couldn't be downloaded and nothing is cached. The message
    is a short reason ("timed out", "TLS: …")."""


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
    failure; raises ``ChainlistUnavailable`` (with a short reason)
    if nothing is cached and the download failed, so the dialog can
    say so rather than report the chain as unknown."""
    cache = cache_dir if cache_dir is not None else CACHE_DIR
    cache_file = cache / "chains.json"
    fresh = (
        cache_file.exists()
        and (time.time() - cache_file.stat().st_mtime) < ttl_seconds
    )
    data: bytes | None = None
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
        except Exception as e:
            log.warning("chainlist fetch failed: %s", e)
            if not cache_file.exists():
                raise ChainlistUnavailable(_short_reason(e)) from e
        else:
            # The download is used even if it can't be cached.
            try:
                atomic_write_bytes(cache_file, data)
            except OSError as e:
                log.warning("chainlist cache write failed: %s", e)
    try:
        raw = json.loads(data if data is not None else cache_file.read_bytes())
    except Exception as e:
        log.warning("chainlist parse failed: %s", e)
        raise ChainlistUnavailable("unreadable chain registry") from e
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


def _root_cause(exc: BaseException) -> BaseException:
    """The innermost error behind a failed ``requests`` call. requests wraps
    urllib3's ``MaxRetryError`` (in ``args[0]``), which keeps the urllib3
    error in ``.reason``, which chains the stdlib socket/ssl error."""
    seen: set[int] = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        reason = getattr(exc, "reason", None)
        first_arg = exc.args[0] if exc.args else None
        nxt = next((e for e in (reason, first_arg, exc.__cause__, exc.__context__)
                    if isinstance(e, BaseException)), None)
        if nxt is None:
            break
        exc = nxt
    return exc


def _short_reason(exc: BaseException) -> str:
    """A failed probe in a few words — enough for the user (or a bug report's
    screenshot) to tell a TLS-trust problem from a timeout from a dead host."""
    if isinstance(exc, requests.exceptions.Timeout):
        return "timed out"
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy error"
    root = _root_cause(exc)
    if isinstance(root, ssl.SSLCertVerificationError):
        return f"TLS: {root.verify_message or 'certificate verify failed'}"
    if isinstance(root, ssl.SSLError):
        return "TLS handshake failed"
    if isinstance(root, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(root, ConnectionRefusedError):
        return "connection refused"
    if isinstance(root, ConnectionResetError):
        return "connection reset"
    if isinstance(root, TimeoutError):
        return "timed out"
    return (str(root) or type(root).__name__)[:120]


_CHAIN_ID_BODY = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": [],
})


def _chain_id_call(session: requests.Session, url: str,
                   timeout: float) -> tuple[int | None, str | None]:
    """One ``eth_chainId`` POST → ``(chain_id, None)`` or ``(None, reason)``.
    Transport errors raise (``requests.RequestException``)."""
    r = session.post(url, data=_CHAIN_ID_BODY, timeout=timeout)
    try:
        obj = r.json()
    except ValueError:
        # An HTML stub / auth wall. A JSON-RPC error riding on a 4xx is parsed
        # below instead, so it reports what the node said.
        return None, (f"HTTP {r.status_code}" if r.status_code >= 400
                      else "non-JSON response")
    if not isinstance(obj, dict):
        return None, "non-object response"
    if obj.get("error"):
        err = obj["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return None, f"rpc error: {msg}"[:120]
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}"
    result = obj.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        return None, "bad result"
    try:
        return int(result, 16), None
    except ValueError:
        return None, "bad chainid"


def probe_rpc(
    url: str, expected_chain_id: int, *,
    timeout: float = 5.0,
) -> tuple[bool, float | None, str | None]:
    """Check ``url`` with ``eth_chainId`` and time it. Returns
    ``(ok, latency_ms, reason)``:

    - ``ok=True`` only when the response is a well-formed JSON-RPC
      reply whose ``chainId`` matches ``expected_chain_id``. That
      catches both "requires registration" (auth error / HTML
      stub) and "wrong chain behind this URL".
    - ``ok=False`` returns ``reason`` as a short error string
      (``_short_reason`` for a transport failure: "timed out",
      "TLS: certificate has expired", "DNS lookup failed", …), which
      the dialog shows.
    - ``latency_ms`` is one request on an already-open connection —
      what each of EthClient's calls costs, since its Session keeps
      connections alive. The first call pays DNS + TCP + TLS on top
      (2.5–4× the round trip on a fast link, ~1 s on a 250 ms one),
      which said more about the user's distance than the endpoint. If
      the server won't reuse the connection, the second call pays the
      setup again — and so would every EthClient call. ``None`` when
      the request itself failed.

    Sent with ``requests`` like EthClient's, so the verdict uses the same
    trust store (certifi) and proxy settings as the client that will use
    the URL.

    HTTP only — WebSocket probes need a real WS handshake, so
    ``wss://`` URLs aren't supported here and qeth can't use WS
    as a chain RPC anyway (EthClient is HTTPProvider-only)."""
    if not url.startswith(("http://", "https://")):
        return False, None, "not http"
    with requests.Session() as s:
        s.headers.update({
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        t0 = time.perf_counter()
        try:
            cid, reason = _chain_id_call(s, url, timeout)
        except requests.RequestException as e:
            return False, None, _short_reason(e)
        latency = (time.perf_counter() - t0) * 1000.0
        if cid is None:
            return False, latency, reason
        if cid != int(expected_chain_id):
            return False, latency, f"chain mismatch ({cid})"
        t0 = time.perf_counter()
        try:
            again, _ = _chain_id_call(s, url, timeout)
        except requests.RequestException:
            again = None
        if again == cid:
            latency = (time.perf_counter() - t0) * 1000.0
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
