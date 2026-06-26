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

2. **Fallback — local py-evm fork (``qeth.pyevm_fork``).** Universal:
   forking only uses standard state reads (``getStorageAt`` / ``getCode``
   / …) that every endpoint exposes. The cost is one request per cold
   storage slot, fetched serially on demand, so it's slow and can trip a
   throttled endpoint's rate limit — hence it's the fallback, not the
   default. The block env is built from the real latest block (an
   env-less fork falsely reverts time-math contracts — oracle staleness,
   deadlines, TWAP — on a zeroed timestamp), and the engine is pure
   Python, so this route ships on every platform/Python (unlike the
   pyrevm it replaced, whose wheels stopped at Linux cp312).

``py-evm`` is an **optional** dependency (``qeth[simulate]``) and
``eth_simulateV1`` isn't everywhere, so simulation may be unavailable;
``simulation_available`` reports whether *either* route can work, and the
helpers return ``None`` when neither can (or the tx reverts) — callers
then skip the preview. Simulation is slow-ish, so callers run it off the
main thread.
"""

import logging
import re
from typing import Any

from eth_utils import to_checksum_address

log = logging.getLogger("qeth.simulate")

# Standard Solidity revert envelopes: Error(string) and Panic(uint256).
_ERROR_SELECTOR = "08c379a0"
_PANIC_SELECTOR = "4e487b71"
# Some RPC error strings embed the revert payload as "output: 0x.."; pull
# it back out to decode the reason.
_REVERT_OUTPUT_RE = re.compile(r"output:\s*(0x[0-9a-fA-F]*)")

# Per-RPC-URL eth_simulateV1 capability, learned by using it: True =
# supported, False = the endpoint returned method-not-found (skip it
# next time), absent = not yet probed (try it). In-memory for the
# session — re-probing costs one request per endpoint per run.
_SIMV1_SUPPORT: dict = {}


class _SimV1Unsupported(Exception):
    """The endpoint doesn't implement ``eth_simulateV1`` — fall back to
    the local fork."""


class SimulationNote:
    """A 'ran fine, but an events list would mislead' outcome. The UI
    shows ``text`` as the placeholder instead of an (empty) events list."""

    def __init__(self, text: str) -> None:
        self.text = text


class RevertNote(SimulationNote):
    """A *definitive* 'this transaction reverts' outcome, carrying the decoded
    reason (the ``Error(string)`` message, a panic code, or the bare selector).

    Distinct from ``SimulationNote`` (ran fine, empty preview) and from ``None``
    (no route / transient failure) so the UI can warn in red *before* broadcast
    instead of hedging. ``verified`` marks a revert computed over Helios-proven
    state — the RPC cannot have faked it, so the warning is trustworthy."""

    def __init__(self, reason: str, *, verified: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.verified = verified


class VerifiedLogs(list):
    """Simulation logs produced over proof-verified state (a Helios
    sidecar): every account/slot the execution touched was checked
    against sync-committee-verified roots, so the RPC endpoint cannot
    have faked this preview. A plain ``list`` everywhere it matters —
    the type is the marker the UI uses to show the verified badge."""


# Calldata sent to an address with no contract code: the EVM ignores it
# (simulates as a clean no-op), but on chains with NATIVE system
# contracts (e.g. TAC's 0x08xx precompile range) the node acts on it
# outside the EVM — so an empty preview would falsely read as "this tx
# does nothing".
_NO_CODE_NOTE = (
    "(the target address has no contract code — the EVM ignores the "
    "calldata, so there is nothing to preview; on chains with native "
    "system contracts the node may act on it outside the EVM)")


def fork_available() -> bool:
    """True if the optional local-fork engine (py-evm) can be imported."""
    from .pyevm_fork import fork_available as _avail
    return _avail()


def simulation_available(chain) -> bool:
    """True if *some* route can simulate on ``chain``'s current RPC: the
    local fork (py-evm installed), or ``eth_simulateV1`` unless we've
    already learned this endpoint lacks it. Lets the UI show an accurate
    'no preview available' note only when nothing can work."""
    if fork_available():
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
    """Reason from an error string that embeds ``output: 0x..``."""
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
                reason = _decode_simv1_revert(c)
                log.warning("eth_simulateV1 call reverted (status %s): %s",
                            status, reason)
                return RevertNote(reason)
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
        logs = _logs_from_simv1(res)
        if logs == [] and "data" in call:
            # Empty preview for a calldata-carrying tx: if the target has
            # no code, an events list would mislead (see _NO_CODE_NOTE).
            # One extra RPC, only on this rare shape; best-effort.
            try:
                code = EthClient(chain).rpc(
                    "eth_getCode", [call["to"], "latest"])
                if not code or code in ("0x", "0x0"):
                    return SimulationNote(_NO_CODE_NOTE)
            except Exception:
                pass
        return logs
    return None


# --- fallback: local py-evm fork ---------------------------------------------

# A neutral block env for injected-reader (hermetic test) runs.
_TEST_BLOCK: dict = {
    "number": 1, "timestamp": 1_700_000_000, "basefee": 0,
    "gas_limit": 30_000_000, "coinbase": None,
    "mix_hash": None, "excess_blob_gas": 0,
}


def _hexint(v):
    if v is None:
        return None
    if isinstance(v, int):
        return v
    return int(v, 16)


# Blocks to fork BEHIND the head for a verified (Helios) simulation. The
# proof-verification failures we see ("root hash mismatch in account proof
# trie") are a bleeding-edge artifact: behind a load balancer the backend
# that answers eth_getProof can be a block or two off from the head Helios
# verified, so its proof hashes to a different state root. A handful of
# blocks back, the backends have converged and the proof matches. Sized in
# WALL-CLOCK (~15-25s) per chain, not a fixed block count — small enough to
# not fork before a just-landed tx (an approve-then-swap in the same
# session), large enough to clear the disagreement window. Latest, not
# finalized: finalized (~13 min on mainnet) is far too stale for a preview.
_VERIFIED_FORK_LAG: dict[int, int] = {
    1:     2,     # mainnet, ~12s blocks  → ~24s
    10:    10,    # OP,      ~2s blocks   → ~20s
    8453:  10,    # Base,    ~2s blocks   → ~20s
    59144: 8,     # Linea,   ~2-3s blocks → ~16-24s
}
_DEFAULT_FORK_LAG = 3

# A real block height is astronomically smaller than the "fork at head"
# sentinel ``fork_floor_block`` returns while a tx is pending, so anything below
# this is a genuine block number (no chain reaches 2**40 blocks for millennia).
_REAL_BLOCK_CEILING = 1 << 40


def _floor_ahead_of_head(chain, floor_block) -> bool:
    """True when ``floor_block`` — this wallet's latest *confirmed* tx — is a
    real block height that ``chain``'s head hasn't reached yet.

    In verified mode the head is Helios's, which trails the execution head by a
    few slots. A verified fork is capped at that head (it can't prove a block
    Helios hasn't synced), so right after a tx confirms, the fork can land
    BEFORE it and miss its state — e.g. a just-confirmed approval, making the
    follow-up swap falsely look like it reverts until Helios catches up (~30s).
    When that's the case the caller skips verified and uses the unverified sim,
    which forks at the real head and sees the tx. Excludes the pending-tx
    sentinel (a huge number that legitimately wants the head)."""
    if floor_block is None or floor_block >= _REAL_BLOCK_CEILING:
        return False
    from .chain import EthClient
    try:
        head = _hexint(EthClient(chain).rpc("eth_blockNumber", [])) or 0
    except Exception:
        return False
    return floor_block > head


def _latest_block(chain, lag: int = 0, floor_block=None) -> "dict | None":
    """Env-relevant fields (ints / address / bytes) of the head block — or,
    when ``lag`` > 0, of the block ``lag`` behind it — so the fork runs in a
    realistic block context. The lag is how a verified simulation dodges the
    head-boundary proof mismatch (see ``_VERIFIED_FORK_LAG``); ``floor_block``
    (this wallet's latest confirmed tx) clamps the backoff forward so the
    preview never forks before the user's own recent state. Raises on RPC
    failure — the retry loop handles transient rate-limits; other errors
    abort the preview (an env-less fork falsely reverts time-math contracts)."""
    from .chain import EthClient
    client = EthClient(chain)
    tag = "latest"
    if lag > 0:
        head = _hexint(client.rpc("eth_blockNumber", [])) or 0
        target = head - lag
        if floor_block is not None:
            # Never fork BEFORE this wallet's own latest confirmed tx, or the
            # preview misses it — an approval done moments ago would make the
            # follow-up swap falsely revert. Clamp toward the head (and never
            # past it, in case the floor is somehow stale-ahead).
            target = max(target, min(floor_block, head))
        if target > 0:                       # guard a very young chain / test net
            tag = hex(target)
    blk = client.rpc("eth_getBlockByNumber", [tag, False])
    if not blk:
        return None
    mix = blk.get("mixHash")
    return {
        "number": _hexint(blk["number"]),
        "timestamp": _hexint(blk["timestamp"]),
        "basefee": _hexint(blk.get("baseFeePerGas")) or 0,
        "gas_limit": _hexint(blk.get("gasLimit")) or 0,
        "coinbase": blk.get("miner"),
        # PREVRANDAO / blob-fee inputs for the execution context.
        "mix_hash": bytes.fromhex(mix[2:]) if isinstance(mix, str) else None,
        "excess_blob_gas": _hexint(blk.get("excessBlobGas")) or 0,
    }


def _simulate_via_fork(chain, from_addr, to_addr, data, value,
                       *, fork_reader=None, fork_block=None, hint_url=None,
                       fork_lag=0, floor_block=None,
                       retries=4, sleep=None, deadline=None):
    """Local py-evm fork (``qeth.pyevm_fork``). ``fork_reader`` is the
    test seam: a fake ``StateReader`` makes the run hermetic (the real
    pure-Python engine executes against injected state, no network) —
    ``fork_block`` then defaults to a neutral env. ``fork_lag`` forks that
    many blocks behind the head (used in verified mode to dodge the
    head-boundary proof mismatch — see ``_VERIFIED_FORK_LAG``). Retries
    transient rate-limits with backoff up to ``deadline`` then gives up; a
    genuine revert fails fast (logged with the decoded reason).

    State is fetched slot-by-slot through the reader, so on a throttled
    endpoint *one* attempt can still take tens of seconds — the deadline
    only stops the retry loop from compounding that. The UI caps how long
    it *waits* separately (see ``_EventPreviewMixin``)."""
    import time as _time
    sleep = sleep or _time.sleep
    from .pyevm_fork import (
        RpcStateReader, SimulationRevert, fork_available, run_fork_call,
    )
    if not fork_available():
        return None
    injected = fork_reader is not None
    fork_no = None
    for attempt in range(retries):
        try:
            block = (fork_block or _TEST_BLOCK) if injected \
                else _latest_block(chain, lag=fork_lag, floor_block=floor_block)
            if block is None:
                log.warning("no block env available; skipping the preview")
                return None
            fork_no = int(block["number"])
            reader = fork_reader if injected \
                else RpcStateReader(chain, hex(fork_no), hint_url=hint_url)
            log.debug("forking at block %s (ts=%s)",
                      block["number"], block["timestamp"])
            out = run_fork_call(
                reader, block, chain_id=chain.chain_id,
                from_addr=from_addr, to_addr=to_addr,
                data=data, value=value,
            )
            if out == [] and data and data not in ("0x", "0X"):
                # Empty preview for a calldata-carrying tx: a code-less
                # target makes the events list a lie (TAC-style native
                # precompiles act outside the EVM). The reader memoizes,
                # so this re-ask is free on the RPC path.
                _, _, code = reader.get_account(to_checksum_address(to_addr))
                if not code:
                    return SimulationNote(_NO_CODE_NOTE)
            return out
        except SimulationRevert as e:
            reason = _decode_revert_output("0x" + e.output.hex())
            log.warning("fork simulation reverted (block %s): %s",
                        fork_no, reason)
            return RevertNote(reason)
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
                  *, fork_reader=None, fork_block=None, floor_block=None,
                  retries=4, sleep=None, budget_s=_TIME_BUDGET_S):
    """Simulate the tx and return its event logs as ``decode_event``-ready
    dicts ``[{"address", "topics", "data"}, …]``, or ``None`` (logged) when
    it's a contract creation, the tx reverts, or no route can run it.

    Prefers ``eth_simulateV1`` (one request) and remembers per RPC URL
    whether the endpoint supports it; falls back to a local py-evm fork
    otherwise. ``budget_s`` bounds the retry loops so a thrashing,
    rate-limited fork can't run for minutes (``None`` = unbounded).
    ``fork_reader`` (a test seam — a fake ``StateReader``) forces the fork
    path and keeps it hermetic. ``sleep`` is injectable so retry tests
    don't actually wait."""
    if not to_addr:
        return None   # contract creation — not previewed
    import time as _time
    deadline = _time.monotonic() + budget_s if budget_s else None
    if fork_reader is None:
        # Verified mode: when a Helios sidecar can serve this chain,
        # prefer the local fork over PROOF-VERIFIED state to the
        # (unverified) eth_simulateV1 fast path — a compromised RPC
        # could otherwise serve a benign-looking preview for a
        # malicious tx, and the preview is exactly the signing-time
        # defense. Slower (per-slot proof fetches), but the remote
        # node can no longer lie about what this tx does.
        from .helios import verified_chain
        # Verified mode needs the LOCAL engine: without py-evm, entering
        # this branch would return None instead of falling through to
        # eth_simulateV1 — killing previews for anyone with a helios
        # binary but no simulate extra. Don't even probe (or spawn) the
        # sidecar in that case.
        helios_chain = verified_chain(chain) if fork_available() else None
        if helios_chain is not None and _floor_ahead_of_head(
                helios_chain, floor_block):
            # The Helios head is still behind this wallet's own latest confirmed
            # tx (it catches up within ~30s). A verified fork would run before
            # that tx and miss its state, falsely reverting the follow-up — so
            # fall through to the unverified sim, which forks at the real head
            # and sees it. A correct (unverified) preview beats a wrong verified
            # one; verified mode resumes automatically once Helios catches up.
            log.info("helios head behind wallet's last tx (block %s); using "
                     "unverified sim this round", floor_block)
            helios_chain = None
        if helios_chain is not None:
            log.info("simulating on helios-verified state (%s)",
                     helios_chain.rpc_url)
            out = _simulate_via_fork(
                helios_chain, from_addr, to_addr, data, value,
                # Hint from the untrusted upstream (fast prestateTracer),
                # not the Helios shadow (slow iterative proving). Keys
                # only — values are re-proven through Helios.
                hint_url=chain.rpc_url,
                # Fork a few blocks behind the head so the RPC backends have
                # converged on the state Helios verified — otherwise the
                # bleeding-edge eth_getProof fails root verification (-32000).
                # floor_block clamps that backoff so we never fork before the
                # wallet's own latest tx (don't hide our just-sent approval).
                fork_lag=_VERIFIED_FORK_LAG.get(chain.chain_id, _DEFAULT_FORK_LAG),
                floor_block=floor_block,
                retries=retries, sleep=sleep, deadline=deadline)
            # Mark success (incl. an empty log list) as verified; None /
            # SimulationNote pass through unchanged. A revert proven over
            # Helios state is trustworthy too — flag it so the warning can say
            # the RPC couldn't have faked it.
            if isinstance(out, RevertNote):
                out.verified = True
            return VerifiedLogs(out) if isinstance(out, list) else out
    if fork_reader is None and _SIMV1_SUPPORT.get(chain.rpc_url) is not False:
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
                              fork_reader=fork_reader, fork_block=fork_block,
                              retries=retries, sleep=sleep, deadline=deadline)
