"""Ledger's signed token metadata, mirrored locally so signing never asks a
server what is being signed (docs/ledger-clear-signing.md, milestone M4).

A Ledger shows an ERC-20 transfer or approval with its ticker and decimals
only when it is handed a descriptor signed by Ledger. Instead of looking one up
when signing — which would tell Ledger which token the user is about to move —
qeth downloads Ledger's WHOLE token list for each EVM network it knows, on a
schedule, and looks descriptors up locally at signing time. A token missing
from the mirror is signed exactly as before (the device asks to blind sign).

* The list comes from Ledger's keyless CAL endpoint, paged, revalidated by its
  ETag (the dataset commit), so an unchanged dataset costs one request.
* Every descriptor is checked on the host before it is sent: the bytes must be
  the token's (address, chain) and Ledger's signature must verify against the
  key Ledger publishes in app-ethereum ``doc/apdu.md`` — a corrupt or tampered
  mirror is dropped, not sent. (The device verifies it again.)

Qt-free; the refresh runs on a worker thread, lookups on any thread.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fsatomic import atomic_write_text

log = logging.getLogger(__name__)

CAL_URL = "https://global.api.prd.ledger.com/cal/v1"
CACHE_DIR = Path.home() / ".qeth" / "ledger"
# How often the mirror is revalidated. An unchanged dataset costs one request.
REFRESH_INTERVAL_S = 24 * 3600
_HTTP_TIMEOUT_S = 60.0
# A safety stop for the cursor walk (mainnet is ~15 pages of 1000).
_MAX_PAGES = 200

# The secp256k1 key Ledger signs legacy ERC-20 descriptors with (app-ethereum
# doc/apdu.md, PROVIDE ERC 20 TOKEN INFORMATION, P1 = 00): ECDSA over
# sha256(ticker ‖ address ‖ decimals u32 ‖ chain id u32).
LEGACY_TOKEN_KEY = bytes.fromhex(
    "045e6c1020c14dc46442fe89f97c0b68cdb15976dc24f24c316e7b30fe4e8cc76b"
    "1489150c21514ebf440ff5dea5393d83de5358cd098fce8fd0f81daa94979183")

# The ERC-20 calls the Ethereum app clear-signs once it has the token's info.
SELECTOR_TRANSFER = "a9059cbb"
SELECTOR_APPROVE = "095ea7b3"


@dataclass(frozen=True)
class TokenDescriptor:
    """One token's signed descriptor: ``data`` = ticker ‖ address(20) ‖
    decimals(u32) ‖ chain id(u32), ``signature`` = Ledger's DER ECDSA."""

    chain_id: int
    address: str          # lowercase 0x-hex
    ticker: str
    decimals: int
    data: bytes
    signature: bytes

    def apdu_payload(self) -> bytes:
        """PROVIDE ERC 20 TOKEN INFORMATION (legacy) data: the ticker length
        prefixed to the signed bytes, then the signature."""
        return bytes([len(self.ticker)]) + self.data + self.signature

    def verify(self) -> bool:
        """Ledger's signature over ``data`` checks out."""
        return verify_legacy_signature(self.data, self.signature)


def _der_rs(signature: bytes) -> tuple[int, int] | None:
    """``(r, s)`` from a DER ``SEQUENCE { INTEGER r, INTEGER s }``."""
    try:
        if signature[0] != 0x30 or signature[1] != len(signature) - 2:
            return None
        i = 2
        values = []
        for _ in range(2):
            if signature[i] != 0x02:
                return None
            n = signature[i + 1]
            values.append(int.from_bytes(signature[i + 2:i + 2 + n], "big"))
            i += 2 + n
        if i != len(signature):
            return None
        return values[0], values[1]
    except IndexError:
        return None


def verify_legacy_signature(data: bytes, signature: bytes) -> bool:
    from eth_keys.datatypes import NonRecoverableSignature, PublicKey
    from eth_keys.exceptions import BadSignature, ValidationError
    rs = _der_rs(signature)
    if rs is None:
        return False
    try:
        return NonRecoverableSignature(rs=rs).verify_msg_hash(
            hashlib.sha256(data).digest(), PublicKey(LEGACY_TOKEN_KEY[1:]))
    except (BadSignature, ValidationError, ValueError):
        return False


def parse_descriptor(chain_id: int, address: str, data: bytes,
                     signature: bytes) -> TokenDescriptor | None:
    """A ``TokenDescriptor`` if ``data`` is a well-formed descriptor for
    exactly this token on exactly this chain (signature not checked here)."""
    if len(data) < 29 or not signature:
        return None
    ticker_bytes = data[:-28]
    body_address = "0x" + data[-28:-8].hex()
    decimals = int.from_bytes(data[-8:-4], "big")
    body_chain = int.from_bytes(data[-4:], "big")
    if body_address != address.lower() or body_chain != chain_id:
        return None
    if len(ticker_bytes) > 0xFF or decimals > 0xFF:
        return None
    if not all(0x20 <= b < 0x7F for b in ticker_bytes):
        return None
    return TokenDescriptor(chain_id, address.lower(), ticker_bytes.decode("ascii"),
                           decimals, data, signature)


def erc20_call_kind(data: str | None) -> str | None:
    """``"transfer"`` / ``"approve"`` for the calls the app clear-signs with
    token info, else ``None``."""
    body = (data or "").lower().removeprefix("0x")
    if body[:8] == SELECTOR_TRANSFER:
        return "transfer"
    if body[:8] == SELECTOR_APPROVE:
        return "approve"
    return None


class TokenMirror:
    """The on-disk mirror: ``<root>/tokens/state.json`` (dataset ETag, last
    check, chain → network) and ``<root>/tokens/<chain_id>.json`` (address →
    ``data:signature`` hex)."""

    def __init__(self, root: Path) -> None:
        self._dir = root / "tokens"
        self._lock = threading.Lock()
        self._loaded: dict[int, tuple[float, dict[str, str]]] = {}

    # --- lookup ------------------------------------------------------------

    def _chain_path(self, chain_id: int) -> Path:
        return self._dir / f"{chain_id}.json"

    def _tokens(self, chain_id: int) -> dict[str, str]:
        path = self._chain_path(chain_id)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        with self._lock:
            cached = self._loaded.get(chain_id)
            if cached is not None and cached[0] == mtime:
                return cached[1]
        try:
            tokens = json.loads(path.read_text()).get("tokens", {})
        except (OSError, ValueError, AttributeError):
            tokens = {}
        if not isinstance(tokens, dict):
            tokens = {}
        with self._lock:
            self._loaded[chain_id] = (mtime, tokens)
        return tokens

    def lookup(self, chain_id: int, address: str | None) -> TokenDescriptor | None:
        """The verified descriptor for ``address`` on ``chain_id``, or None.
        Local only — never touches the network."""
        if not address:
            return None
        entry = self._tokens(chain_id).get(address.lower())
        if not isinstance(entry, str) or ":" not in entry:
            return None
        data_hex, _, sig_hex = entry.partition(":")
        try:
            descriptor = parse_descriptor(chain_id, address, bytes.fromhex(data_hex),
                                          bytes.fromhex(sig_hex))
        except ValueError:
            return None
        if descriptor is None or not descriptor.verify():
            log.warning("ledger token mirror: bad descriptor for %s on %s",
                        address, chain_id)
            return None
        return descriptor

    # --- refresh -----------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        try:
            state = json.loads((self._dir / "state.json").read_text())
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def last_checked(self) -> float | None:
        checked = self._state().get("checked_at")
        return float(checked) if isinstance(checked, (int, float)) else None

    def needs_refresh(self, chain_ids: Iterable[int], *,
                      now: float | None = None) -> bool:
        """Stale (older than ``REFRESH_INTERVAL_S``), or a chain qeth knows is
        missing from the mirror."""
        state = self._state()
        checked = state.get("checked_at")
        now = time.time() if now is None else now
        if not isinstance(checked, (int, float)) or now - checked >= REFRESH_INTERVAL_S:
            return True
        known = state.get("chains", [])
        return any(c not in known for c in chain_ids)

    def refresh(self, chain_ids: Iterable[int], *, session: Any = None,
                now: float | None = None) -> int:
        """Bring the mirror up to date for ``chain_ids``; return how many
        networks were (re)downloaded. One conditional request when nothing
        changed. Raises on a network failure (the caller retries later)."""
        import requests
        from . import USER_AGENT
        http = session or requests.Session()
        headers = {"User-Agent": USER_AGENT}
        wanted = sorted(set(chain_ids))
        state = self._state()
        commit = state.get("commit")
        now = time.time() if now is None else now

        # Every wanted chain was processed at ``commit`` → ask whether the
        # dataset moved on; a 304 means there's nothing to do.
        net_headers = dict(headers)
        if commit and set(wanted) <= set(state.get("chains", [])):
            net_headers["If-None-Match"] = commit
        r = http.get(f"{CAL_URL}/networks", params={"output": "id,chain_id,family"},
                     headers=net_headers, timeout=_HTTP_TIMEOUT_S)
        if r.status_code == 304:
            state["checked_at"] = now
            self._write_state(state)
            return 0
        r.raise_for_status()
        # The ETag names the dataset commit; kept verbatim (it's a weak
        # ``W/"…"`` one) to send back as If-None-Match.
        commit = r.headers.get("ETag") or None
        networks = {
            int(n["chain_id"]): str(n["id"]) for n in r.json()
            if n.get("family") == "ethereum" and isinstance(n.get("chain_id"), int)
        }

        fetched = 0
        for chain_id in wanted:
            network = networks.get(chain_id)
            if network is None:
                continue          # Ledger has no list for this chain
            if commit and self._file_commit(chain_id) == commit:
                continue
            tokens = self._download(http, headers, chain_id, network)
            atomic_write_text(self._chain_path(chain_id), json.dumps({
                "network": network, "commit": commit, "fetched_at": now,
                "tokens": tokens,
            }, separators=(",", ":")))
            fetched += 1
        self._write_state({
            "commit": commit, "checked_at": now, "chains": wanted,
            "networks": {str(c): networks[c] for c in wanted if c in networks},
        })
        return fetched

    def _file_commit(self, chain_id: int) -> str | None:
        try:
            meta = json.loads(self._chain_path(chain_id).read_text())
        except (OSError, ValueError):
            return None
        commit = meta.get("commit") if isinstance(meta, dict) else None
        return commit if isinstance(commit, str) else None

    def _download(self, http: Any, headers: dict[str, str], chain_id: int,
                  network: str) -> dict[str, str]:
        """Every token of ``network``, as address → ``data:signature`` hex,
        keeping only well-formed descriptors for that chain."""
        tokens: dict[str, str] = {}
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            params = {"network": network, "output": "contract_address,descriptor"}
            if cursor:
                params["cursor"] = cursor
            r = http.get(f"{CAL_URL}/tokens", params=params, headers=headers,
                         timeout=_HTTP_TIMEOUT_S)
            r.raise_for_status()
            for row in r.json():
                descriptor = _row_descriptor(chain_id, row)
                if descriptor is not None:
                    tokens[descriptor.address] = (
                        f"{descriptor.data.hex()}:{descriptor.signature.hex()}")
            cursor = r.headers.get("x-ledger-next") or None
            if not cursor:
                break
        else:
            raise RuntimeError(f"Ledger token list for {network} didn't end")
        log.info("ledger token mirror: %s (%d) — %d tokens",
                 network, chain_id, len(tokens))
        return tokens

    def _write_state(self, state: dict[str, Any]) -> None:
        atomic_write_text(self._dir / "state.json", json.dumps(state))


def _row_descriptor(chain_id: int, row: Any) -> TokenDescriptor | None:
    if not isinstance(row, dict):
        return None
    address = row.get("contract_address")
    descriptor = row.get("descriptor")
    if not isinstance(address, str) or not isinstance(descriptor, dict):
        return None
    signatures = descriptor.get("signatures")
    sig_hex = signatures.get("prod") if isinstance(signatures, dict) else None
    data_hex = descriptor.get("data")
    if not isinstance(sig_hex, str) or not isinstance(data_hex, str):
        return None
    try:
        return parse_descriptor(chain_id, address, bytes.fromhex(data_hex),
                                bytes.fromhex(sig_hex))
    except ValueError:
        return None


_MIRROR: TokenMirror | None = None
_MIRROR_ROOT: Path | None = None
_MIRROR_LOCK = threading.Lock()


def default_mirror() -> TokenMirror:
    """The process-wide mirror under ``CACHE_DIR`` (re-read on each call, so a
    test redirecting it gets a fresh instance)."""
    global _MIRROR, _MIRROR_ROOT
    with _MIRROR_LOCK:
        if _MIRROR is None or _MIRROR_ROOT != CACHE_DIR:
            _MIRROR, _MIRROR_ROOT = TokenMirror(CACHE_DIR), CACHE_DIR
        return _MIRROR
