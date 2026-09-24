"""``TronClient`` — Tron's full-node HTTP API (``/wallet/*``), plus TronGrid's
indexed ``/v1`` API where only an indexer can answer (account history).

Synchronous (urllib), for QThread workers — the same shape as ``EthClient``.
Requests send addresses in the ``41…`` hex form without ``visible``, so
responses come back in hex too and nothing base58 crosses into qeth's core.

Endpoints and limits (probed 2026-09-24, see docs/tron.md): keyless TronGrid
allows ~3 req/s and suspends a burst for 4-5 s with an HTTP 429, so the
chain's ``api_url`` is PublicNode (same full-node API, no such limit) with
TronGrid as the fallback. A 429 or a transport failure moves on to the next
base; an answer from a node — even an error — is final.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

from eth_abi import decode as abi_decode
from dataclasses import dataclass
from typing import Any

from .. import USER_AGENT
from ..address import tron_hex41
from ..chains import Chain

log = logging.getLogger("qeth.tron.client")

DEFAULT_TIMEOUT = 15.0

# chain id → TronGrid base, for the indexed /v1 API (history, TRC-20 lists).
# No other keyless provider serves it; full nodes don't index by account.
TRONGRID_INSTANCES: dict[int, str] = {
    728126428: "https://api.trongrid.io",
    3448148188: "https://nile.trongrid.io",
    2494104990: "https://api.shasta.trongrid.io",
}


# Keyless TronGrid allows ~3 requests/s per IP and, past that, SUSPENDS the
# caller for 4-5 s ("request rate exceeded the allowed_rps(3), and the query
# server is suspended for 4 s"). The tokens tab, history and activities hit
# /v1 concurrently, so pace every /v1 request process-wide below the limit,
# and on a 429 wait out the suspension it names before retrying.
_INDEX_INTERVAL_S = 0.4
_INDEX_RETRIES = 2
_INDEX_SUSPENSION_S = 5.0       # when the 429 body doesn't say
_SUSPENDED = re.compile(r"suspended for (\d+) ?s")


class _Pacer:
    """Spaces calls at least ``interval`` apart across threads."""

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self._interval
        if slot > now:
            time.sleep(slot - now)

    def hold(self, seconds: float) -> None:
        """Push every caller's next slot past a server-imposed suspension."""
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


_INDEX_PACER = _Pacer(_INDEX_INTERVAL_S)


def _suspension(e: urllib.error.HTTPError) -> float:
    """The suspension a TronGrid 429 names, in seconds (a default if not)."""
    try:
        body = e.read().decode(errors="replace") if e.fp is not None else ""
    except Exception:
        body = ""
    m = _SUSPENDED.search(body)
    return float(m.group(1)) + 0.5 if m else _INDEX_SUSPENSION_S


class TronError(Exception):
    """A Tron node answered, and refused (bad request, rejected broadcast)."""


class TronTransportError(TronError):
    """No Tron endpoint could be reached (all down, or all rate-limiting)."""


@dataclass(frozen=True)
class BlockRef:
    number: int
    block_id: str     # 64 hex, no 0x
    timestamp: int    # unix ms


def _block_ref(resp: dict) -> BlockRef:
    raw = (resp.get("block_header") or {}).get("raw_data") or {}
    bid = resp.get("blockID")
    if not isinstance(bid, str) or "number" not in raw:
        raise TronError(f"malformed block: {str(resp)[:200]}")
    return BlockRef(int(raw["number"]), bid, int(raw.get("timestamp", 0)))


def _message(resp: dict) -> str:
    """A node error's human text. ``broadcasttransaction`` hex-encodes it;
    ``broadcasthex`` and most others send plain text."""
    msg = str(resp.get("message") or resp.get("Error") or "")
    try:
        decoded = bytes.fromhex(msg).decode()
        if decoded.isprintable():
            return decoded
    except ValueError:
        pass
    return msg


def call_reverted(resp: dict) -> bool:
    """Whether a ``triggerconstantcontract`` answer is a revert."""
    ret = ((resp.get("transaction") or {}).get("ret") or [{}])[0]
    return ret.get("ret") == "FAILED"


def revert_reason(resp: dict) -> str:
    """The human reason for a reverted constant call: the ``Error(string)``
    message when the contract gave one, else the node's text ("REVERT
    opcode executed")."""
    out = (resp.get("constant_result") or [""])[0] or ""
    if out.startswith("08c379a0") and len(out) >= 8 + 128:
        try:
            return str(abi_decode(["string"], bytes.fromhex(out[8:]))[0])
        except Exception:      # malformed revert data — fall back to the text
            pass
    return _message(resp.get("result") or {}) or "reverted"


class TronClient:
    def __init__(self, chain: Chain, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.chain = chain
        self.timeout = timeout
        self._bases = [u.rstrip("/") for u in (chain.api_url, *chain.api_fallbacks) if u]
        if not self._bases:
            raise TronError(f"{chain.name} has no Tron HTTP API configured")

    # --- transport ----------------------------------------------------------

    def _request(self, url: str, body: dict | None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method="POST" if body is not None else "GET",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read() or b"{}")

    def _post(self, path: str, body: dict) -> dict:
        """POST to each base in turn until one answers. Rate limiting (429)
        and transport failures fall through to the next base."""
        last: Exception | None = None
        for base in self._bases:
            try:
                resp = self._request(base + path, body)
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    log.info("tron %s %s: HTTP %s, trying next", base, path, e.code)
                    last = e
                    continue
                raise TronError(f"{path}: HTTP {e.code}") from e
            except (urllib.error.URLError, OSError, ValueError) as e:
                log.info("tron %s %s: %s, trying next", base, path, e)
                last = e
                continue
            if not isinstance(resp, dict):
                raise TronError(f"{path}: unexpected response {str(resp)[:200]}")
            return resp
        raise TronTransportError(f"{path}: no Tron endpoint reachable ({last})")

    # --- reads --------------------------------------------------------------

    def get_account(self, address: str) -> dict:
        """The account record, or ``{}`` for an address that was never
        activated (never received TRX) — such an account can't send."""
        return self._post("/wallet/getaccount", {"address": tron_hex41(address)})

    def get_balance(self, address: str) -> int:
        """TRX balance in sun (0 for an unactivated address)."""
        return int(self.get_account(address).get("balance", 0))

    def account_resources(self, address: str) -> dict:
        """Bandwidth / energy limits and usage (``freeNetLimit``,
        ``freeNetUsed``, ``NetLimit``, ``NetUsed``, ``EnergyLimit``,
        ``EnergyUsed``; absent keys are 0). ``{}`` when unactivated."""
        return self._post("/wallet/getaccountresource",
                          {"address": tron_hex41(address)})

    def chain_parameters(self) -> dict[str, int]:
        """``getchainparameters`` as ``{key: value}`` — the fee schedule
        (``getTransactionFee``, ``getEnergyFee``, …)."""
        resp = self._post("/wallet/getchainparameters", {})
        return {p["key"]: int(p.get("value", 0))
                for p in resp.get("chainParameter", []) if "key" in p}

    def head_block(self) -> BlockRef:
        return _block_ref(self._post("/wallet/getblock", {"detail": False}))

    def solid_block(self) -> BlockRef:
        """The latest solidified block — the TaPoS reference java-tron itself
        defaults to (a head reference can hit a fork and fail TaPoS)."""
        return _block_ref(self._post("/walletsolidity/getblock", {"detail": False}))

    def constant_call(self, owner: str, contract: str, data: bytes,
                      call_value: int = 0) -> dict:
        """Simulate a contract call (read-only) and return the node's whole
        answer: ``energy_used`` (dynamic penalty included — the energy
        estimate, since ``/wallet/estimateenergy`` is off on public nodes),
        ``constant_result`` (return or revert data), ``logs`` (the events it
        emits) and ``transaction.ret`` — ``FAILED`` when the call reverts
        (``result.result`` only says the node ran it)."""
        body: dict[str, Any] = {
            "owner_address": tron_hex41(owner),
            "contract_address": tron_hex41(contract),
            "data": data.hex(),
        }
        if call_value:
            body["call_value"] = call_value
        return self._post("/wallet/triggerconstantcontract", body)

    def trigger_constant(self, owner: str, contract: str, data: bytes,
                         call_value: int = 0) -> dict:
        """``constant_call`` for callers that need a SUCCESSFUL run (the
        energy estimate, reads): raises ``TronError`` when the node refuses
        the call or the call reverts — a reverting call's energy prices
        nothing real."""
        resp = self.constant_call(owner, contract, data, call_value)
        result = resp.get("result") or {}
        if not result.get("result"):
            raise TronError(_message(result) or "constant call failed")
        if call_reverted(resp):
            raise TronError(revert_reason(resp))
        return resp

    def call(self, contract: str, data: bytes) -> bytes:
        """``eth_call``-style read: the call's return bytes."""
        resp = self.trigger_constant(contract, contract, data)
        out = resp.get("constant_result") or [""]
        return bytes.fromhex(out[0])

    def transaction_info(self, tx_id: str) -> dict:
        """The receipt of an included transaction, ``{}`` until then."""
        return self._post("/wallet/gettransactioninfobyid", {"value": tx_id})

    # --- writes -------------------------------------------------------------

    def broadcast(self, signed: bytes) -> str:
        """Broadcast a signed ``Transaction`` and return its txid (hex, no
        0x). The exact bytes go to ``broadcasthex`` — no JSON round-trip
        that could re-serialize them. Raises ``TronError`` with the node's
        reason when it refuses the transaction."""
        resp = self._post("/wallet/broadcasthex", {"transaction": signed.hex()})
        if not resp.get("result"):
            raise TronError(_message(resp) or str(resp.get("code") or "rejected"))
        return str(resp.get("txid") or "")

    # --- indexed (TronGrid /v1) ----------------------------------------------

    def index_get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """GET from TronGrid's indexed ``/v1`` API (account history and
        token lists — nothing a full node answers)."""
        base = TRONGRID_INSTANCES.get(self.chain.chain_id)
        if base is None:
            raise TronError(f"no Tron indexer for {self.chain.name}")
        url = base + path
        if params:
            url += "?" + urlencode(params)
        for attempt in range(_INDEX_RETRIES + 1):
            _INDEX_PACER.wait()
            try:
                resp = self._request(url, None)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < _INDEX_RETRIES:
                    wait = _suspension(e)
                    log.info("TronGrid rate limit on %s — waiting %.1f s", path, wait)
                    _INDEX_PACER.hold(wait)
                    continue
                raise TronTransportError(f"{path}: HTTP {e.code}") from e
            except (urllib.error.URLError, OSError, ValueError) as e:
                raise TronTransportError(f"{path}: {e}") from e
        if not isinstance(resp, dict) or resp.get("success") is False:
            raise TronError(f"{path}: {str(resp)[:200]}")
        return resp
