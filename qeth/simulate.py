"""Local transaction simulation for the event preview.

Run a not-yet-broadcast transaction and return the event logs it would
emit — so the Send / dapp-Sign dialogs can preview "what will happen" the
same way the confirmed-tx details view shows past events.

Two routes, picked per-endpoint (``simulate_logs`` orchestrates):

1. **Fast path — ``eth_simulateV1``.** One RPC request: the node runs the
   call against latest state and returns the logs directly. No per-slot
   round-trips, no rate-limit burst, no block-env gotcha. Supported by
   DRPC (most chains), mevblocker, publicnode, … but *not* universal —
   e.g. cloudflare-eth.com rejects it with ``-32601``. So support is
   probed by *using* it and cached per RPC URL (``_SIMV1_SUPPORT``).

2. **Fallback — local revm fork (``pyrevm``).** Universal: forking only
   uses standard state reads (``getStorageAt`` / ``getCode`` / …) that
   every endpoint exposes. The cost is one request per cold storage slot,
   fetched serially on demand, so it's slow and can trip a throttled
   endpoint's rate limit — hence it's the fallback, not the default. Also
   needs the block-env fix below.

   **Block environment.** pyrevm forks *state* at latest but leaves the
   block env zeroed (``timestamp == 1``, ``number == 0``). Contracts that
   do time math (oracle staleness, deadlines, TWAP) then revert on a
   checked-math underflow, so a valid tx simulates as reverting. We fetch
   the real latest block and ``set_block_env`` from it before the call.

``pyrevm`` is an **optional** dependency and ``eth_simulateV1`` isn't
everywhere, so simulation may be unavailable; ``simulation_available``
reports whether *either* route can work, and the helpers return ``None``
when neither can (or the tx reverts) — callers then skip the preview.
Simulation is slow-ish, so callers run it off the main thread.
"""

import logging
import re
from typing import Any

from eth_utils import to_checksum_address

log = logging.getLogger("qeth.simulate")

# Standard Solidity revert envelopes: Error(string) and Panic(uint256).
_ERROR_SELECTOR = "08c379a0"
_PANIC_SELECTOR = "4e487b71"
# pyrevm raises RuntimeError("Revert { gas_used: N, output: 0x.. }") on a
# reverting call; pull the output payload back out to decode the reason.
_REVERT_OUTPUT_RE = re.compile(r"output:\s*(0x[0-9a-fA-F]*)")

# Per-RPC-URL eth_simulateV1 capability, learned by using it: True =
# supported, False = the endpoint returned method-not-found (skip it
# next time), absent = not yet probed (try it). In-memory for the
# session — re-probing costs one request per endpoint per run.
_SIMV1_SUPPORT: dict = {}


class _SimV1Unsupported(Exception):
    """The endpoint doesn't implement ``eth_simulateV1`` — fall back to
    the local fork."""


def pyrevm_available() -> bool:
    """True if the optional ``pyrevm`` dependency can be imported."""
    try:
        import pyrevm  # noqa: F401
        return True
    except Exception:
        return False


def simulation_available(chain) -> bool:
    """True if *some* route can simulate on ``chain``'s current RPC: the
    local fork (pyrevm installed), or ``eth_simulateV1`` unless we've
    already learned this endpoint lacks it. Lets the UI show an accurate
    'no preview available' note only when nothing can work."""
    if pyrevm_available():
        return True
    return _SIMV1_SUPPORT.get(chain.rpc_url) is not False


# --- revert-reason decoding --------------------------------------------------

def _decode_revert_output(out_hex: str) -> str:
    """Human reason from raw revert output bytes: the standard
    ``Error(string)`` / ``Panic(uint256)`` envelopes, else the selector."""
    out = out_hex[2:] if out_hex[:2] in ("0x", "0X") else out_hex
    if not out:
        return "reverted without a reason string"
    selector, payload = out[:8], out[8:]
    if selector == _ERROR_SELECTOR:
        try:
            from eth_abi import decode
            (reason,) = decode(["string"], bytes.fromhex(payload))
            return reason
        except Exception:
            pass
    elif selector == _PANIC_SELECTOR:
        try:
            return f"panic 0x{int(payload, 16):02x}"
        except Exception:
            pass
    return f"reverted (selector 0x{selector})"


def _decode_revert(msg: str) -> str:
    """Reason from pyrevm's RuntimeError text, which embeds ``output: 0x..``."""
    m = _REVERT_OUTPUT_RE.search(msg)
    if not m:
        return msg.strip()
    return _decode_revert_output(m.group(1))


def _is_rate_limited(e) -> bool:
    """True when an exception looks like an RPC rate-limit response (DRPC
    free tier: ``code 15 "Too many request"`` / HTTP 429). Transient —
    worth a backoff + retry, unlike a genuine revert or a missing method."""
    s = str(e).lower()
    return ("too many request" in s or "code: 15" in s
            or "429" in s or "rate limit" in s)


def _is_method_unsupported(e) -> bool:
    """True when the endpoint doesn't implement the method: a JSON-RPC
    ``-32601`` or an HTTP 400/404 from a gateway that doesn't route it."""
    if getattr(e, "code", None) == -32601:
        return True
    s = str(e).lower()
    return ("-32601" in s or "method not found" in s or "not supported" in s
            or "not available" in s or "unsupported" in s
            or "does not exist" in s or "http error 404" in s
            or "http error 400" in s or "bad request" in s)


# --- fast path: eth_simulateV1 ----------------------------------------------

def _normalize_rpc_log(lg) -> dict:
    """One JSON-RPC log → the ``decode_event``-ready dict shape (matches
    LogsFetchWorker), tolerating HexBytes or plain hex strings."""
    return {
        "address": lg.get("address"),
        "topics": [t.hex() if hasattr(t, "hex") else t
                   for t in (lg.get("topics") or [])],
        "data": (lg.get("data").hex() if hasattr(lg.get("data"), "hex")
                 else lg.get("data")) or "0x",
    }


def _logs_from_simv1(res):
    """Pull logs out of an ``eth_simulateV1`` result. Returns ``None`` (and
    logs the reason) if any call reverted — a definitive answer, not a
    reason to fall back to the fork."""
    out = []
    for blk in res or []:
        for c in (blk.get("calls") or []):
            status = c.get("status")
            if status not in ("0x1", "0x01", 1):
                log.warning("eth_simulateV1 call reverted (status %s): %s",
                            status, _decode_simv1_revert(c))
                return None
            for lg in (c.get("logs") or []):
                out.append(_normalize_rpc_log(lg))
    return out


def _decode_simv1_revert(call) -> str:
    err: Any = call.get("error") if hasattr(call, "get") else None
    data = None
    if hasattr(err, "get"):
        data = err.get("data")
    data = data or (call.get("returnData") if hasattr(call, "get") else None)
    if isinstance(data, str) and data[:2] in ("0x", "0X"):
        return _decode_revert_output(data)
    if hasattr(err, "get") and err.get("message"):
        return err["message"]
    return "reverted"


def _past(deadline) -> bool:
    import time as _time
    return deadline is not None and _time.monotonic() >= deadline


def _simulate_via_rpc(chain, from_addr, to_addr, data, value,
                      *, retries=4, sleep=None, deadline=None):
    """Run the tx through ``eth_simulateV1`` (one request) and return its
    logs. Raises ``_SimV1Unsupported`` if the endpoint lacks the method;
    retries transient rate-limits with backoff (cheap — it's one call)
    until ``deadline`` (monotonic seconds), then gives up."""
    import time as _time
    sleep = sleep or _time.sleep
    from .chain import EthClient
    call: dict[str, str] = {"from": to_checksum_address(from_addr),
                            "to": to_checksum_address(to_addr)}
    if data and data not in ("0x", "0X"):
        call["data"] = data
    if value:
        call["value"] = hex(int(value))
    params = [{
        "blockStateCalls": [{"calls": [call]}],
        # Only real contract logs (consistent with the fork path); don't
        # synthesise ETH-movement Transfer logs.
        "traceTransfers": False,
        "validation": False,
    }, "latest"]
    for attempt in range(retries):
        try:
            res = EthClient(chain).rpc("eth_simulateV1", params)
        except Exception as e:
            if _is_method_unsupported(e):
                raise _SimV1Unsupported() from e
            if (_is_rate_limited(e) and attempt < retries - 1
                    and not _past(deadline)):
                sleep(min(0.75 * (2 ** attempt), 4.0))
                continue
            raise
        return _logs_from_simv1(res)
    return None


# --- fallback: local revm fork ----------------------------------------------

def _hexint(v):
    if v is None:
        return None
    if isinstance(v, int):
        return v
    return int(v, 16)


def _latest_block(chain):
    """Latest block's env-relevant fields (ints / address) so the fork
    runs in a realistic block context. Raises on RPC failure — the retry
    loop handles transient rate-limits; other errors abort the preview
    (an env-less fork reintroduces zeroed-timestamp false reverts)."""
    from .chain import EthClient
    blk = EthClient(chain).rpc("eth_getBlockByNumber", ["latest", False])
    if not blk:
        return None
    return {
        "number": _hexint(blk["number"]),
        "timestamp": _hexint(blk["timestamp"]),
        "basefee": _hexint(blk.get("baseFeePerGas")) or 0,
        "gas_limit": _hexint(blk.get("gasLimit")) or 0,
        "coinbase": blk.get("miner"),
    }


def _apply_block_env(evm, block) -> None:
    from pyrevm import BlockEnv
    kwargs = {
        "number": block["number"],
        "timestamp": block["timestamp"],
        "basefee": block["basefee"],
    }
    if block.get("gas_limit"):
        kwargs["gas_limit"] = block["gas_limit"]
    if block.get("coinbase"):
        kwargs["coinbase"] = to_checksum_address(block["coinbase"])
    evm.set_block_env(BlockEnv(**kwargs))


def _run_fork(EVM, chain, from_addr, to_addr, data, value, block):
    """Fork, set the block env, run the call, return its logs."""
    if block is not None:
        evm = EVM(fork_url=chain.rpc_url, fork_block=hex(block["number"]))
        _apply_block_env(evm, block)
        log.debug("forking at block %s (ts=%s)",
                  block["number"], block["timestamp"])
    else:
        evm = EVM(fork_url=chain.rpc_url)
    calldata = b""
    if data and data not in ("0x", "0X"):
        calldata = bytes.fromhex(data[2:] if data.startswith("0x") else data)
    kwargs: dict[str, Any] = {
        "caller": to_checksum_address(from_addr),
        "to": to_checksum_address(to_addr),
        "calldata": calldata,
    }
    if value:
        kwargs["value"] = int(value)
    evm.message_call(**kwargs)
    out = []
    for lg in evm.result.logs:
        # pyrevm Log: .address (str), .topics (list[str]), .data is a
        # (topics, data_bytes) tuple — the payload is [1].
        out.append({
            "address": lg.address,
            "topics": list(lg.topics),
            "data": "0x" + lg.data[1].hex(),
        })
    return out


def _simulate_via_fork(chain, from_addr, to_addr, data, value,
                       *, evm_cls=None, retries=4, sleep=None, deadline=None):
    """Local revm fork. ``evm_cls`` is a test seam; when set we skip the
    (networked) block-env fetch so tests stay hermetic. Retries transient
    rate-limits with backoff up to ``deadline`` then gives up; a genuine
    revert fails fast (logged).

    A single ``message_call`` can't be interrupted (pyrevm fetches cold
    slots one-by-one through its own HTTP client, with no callback), so on
    a slow/throttled endpoint *one* attempt can still take tens of seconds
    — the deadline only stops the retry loop from compounding that. The UI
    caps how long it *waits* separately (see ``_EventPreviewMixin``)."""
    import time as _time
    sleep = sleep or _time.sleep
    injected = evm_cls is not None
    EVM = evm_cls
    if EVM is None:
        try:
            from pyrevm import EVM
        except Exception:
            return None
    fork_no = None
    for attempt in range(retries):
        try:
            block = None if injected else _latest_block(chain)
            fork_no = block["number"] if block else None
            return _run_fork(EVM, chain, from_addr, to_addr, data, value, block)
        except Exception as e:
            if (_is_rate_limited(e) and attempt < retries - 1
                    and not _past(deadline)):
                delay = min(0.75 * (2 ** attempt), 4.0)
                log.info("fork rate-limited by RPC; retry %d/%d in %.1fs",
                         attempt + 1, retries - 1, delay)
                sleep(delay)
                continue
            log.warning("fork simulation failed (block %s): %s",
                        fork_no, _decode_revert(str(e)))
            return None


# --- orchestrator ------------------------------------------------------------

# Wall-clock budget for the retry loops (one slow fork attempt can still
# overrun it — see _simulate_via_fork — but the loop won't start another).
_TIME_BUDGET_S = 40.0


def simulate_logs(chain, from_addr: str, to_addr, data, value,
                  *, evm_cls=None, retries=4, sleep=None, budget_s=_TIME_BUDGET_S):
    """Simulate the tx and return its event logs as ``decode_event``-ready
    dicts ``[{"address", "topics", "data"}, …]``, or ``None`` (logged) when
    it's a contract creation, the tx reverts, or no route can run it.

    Prefers ``eth_simulateV1`` (one request) and remembers per RPC URL
    whether the endpoint supports it; falls back to a local pyrevm fork
    otherwise. ``budget_s`` bounds the retry loops so a thrashing,
    rate-limited fork can't run for minutes (``None`` = unbounded).
    ``evm_cls`` (a test seam) forces the fork path. ``sleep`` is injectable
    so retry tests don't actually wait."""
    if not to_addr:
        return None   # contract creation — not previewed
    import time as _time
    deadline = _time.monotonic() + budget_s if budget_s else None
    if evm_cls is None and _SIMV1_SUPPORT.get(chain.rpc_url) is not False:
        try:
            logs = _simulate_via_rpc(chain, from_addr, to_addr, data, value,
                                     retries=retries, sleep=sleep,
                                     deadline=deadline)
            _SIMV1_SUPPORT[chain.rpc_url] = True
            return logs   # list (success) or None (definitive revert)
        except _SimV1Unsupported:
            _SIMV1_SUPPORT[chain.rpc_url] = False
            log.info("eth_simulateV1 not on %s; using local fork",
                     chain.rpc_url)
        except Exception as e:
            log.warning("eth_simulateV1 failed on %s (%s); trying local fork",
                        chain.rpc_url, e)
    return _simulate_via_fork(chain, from_addr, to_addr, data, value,
                              evm_cls=evm_cls, retries=retries, sleep=sleep,
                              deadline=deadline)
