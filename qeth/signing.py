"""Signing seam — ``Signer`` ABC + cross-thread ``SignerBridge``.

The ABC is the plug-point for future signing backends. Today the
only concrete signer is the Ledger one (Phase 2); a hot-wallet
signer can land later by implementing the same surface.

The bridge is the conduit between the aiohttp RPC handler (running
on a background asyncio loop) and the Qt UI thread:

    aiohttp coroutine
        ↓ submit_async(req)            (thread: qeth-rpc)
    bridge.request_received.emit       (Qt::AutoConnection → queued)
        ↓                              (thread: Qt main)
    MainWindow opens SignTransactionDialog
        ↓ user confirms / cancels / signs
    bridge.resolve(fut, hash)
        ↓                              (resumes aiohttp coroutine)
    aiohttp returns {"result": "0x…"}  (thread: qeth-rpc)

``concurrent.futures.Future`` is safe to set from any thread — it's
the natural type for this style of cross-thread waiting and
``asyncio.wrap_future`` adapts it to the awaiting side."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Optional

from eth_utils import to_checksum_address

from PySide6.QtCore import QObject, QThread, Signal


log = logging.getLogger("qeth.signing")


class SignerError(Exception):
    """Signer-side failure (no matching key, user cancelled, dongle
    locked, etc.). The RPC handler surfaces this as a JSON-RPC error
    so the dapp gets a structured reject."""


@dataclass
class SigningRequest:
    """Normalised ``eth_sendTransaction`` params.

    All numeric fields are ``int`` (wei or gas units) or ``None``;
    the JSON-RPC layer converted from hex on the way in. The dialog
    fills in any ``None`` from the chain via ``GasSuggestionWorker``.
    """
    chain_id: int
    from_addr: str
    to_addr: Optional[str]
    value_wei: int = 0
    data: str = "0x"
    gas: Optional[int] = None
    max_fee_per_gas: Optional[int] = None
    max_priority_fee_per_gas: Optional[int] = None
    gas_price: Optional[int] = None
    nonce: Optional[int] = None
    # HTTP Origin / WS Origin of the caller — typically the dapp's
    # URL (https://app.uniswap.org), populated by the RPC handler
    # from the incoming request headers. ``None`` for locally
    # initiated requests (e.g. the user clicked Send in the UI).
    origin: Optional[str] = None


def _hex_to_int(v) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.startswith("0x") else int(s)


def parse_send_transaction_params(
    params: list, chain_id: int, *, origin: Optional[str] = None,
) -> SigningRequest:
    """Parse the dapp's ``eth_sendTransaction`` params list into a
    typed ``SigningRequest``. ``origin`` is the caller's HTTP /WS
    Origin header — set by the RPC handler, surfaced to the signing
    dialog so the user sees which site is requesting the tx."""
    if not params or not isinstance(params[0], dict):
        raise SignerError("eth_sendTransaction expects a single object parameter")
    p = params[0]
    if "from" not in p:
        raise SignerError("missing `from` in tx params")
    # Normalise to EIP-55 mixed case here so downstream code (web3.py
    # call sites, dialog display) never has to handle the lower-cased
    # form dapps tend to send. web3.py outright refuses non-checksum
    # addresses with a long error message.
    from_addr = to_checksum_address(p["from"])
    to_raw = p.get("to")
    to_addr = to_checksum_address(to_raw) if to_raw else None
    return SigningRequest(
        chain_id=chain_id,
        from_addr=from_addr,
        to_addr=to_addr,
        value_wei=_hex_to_int(p.get("value")) or 0,
        data=p.get("data") or p.get("input") or "0x",
        gas=_hex_to_int(p.get("gas")),
        max_fee_per_gas=_hex_to_int(p.get("maxFeePerGas")),
        max_priority_fee_per_gas=_hex_to_int(p.get("maxPriorityFeePerGas")),
        gas_price=_hex_to_int(p.get("gasPrice")),
        nonce=_hex_to_int(p.get("nonce")),
        origin=origin,
    )


@dataclass
class MessageSigningRequest:
    """``personal_sign`` request — sign a human-readable text or
    raw bytes prefixed with the EIP-191 personal-message tag
    (``\\x19Ethereum Signed Message:\\n<len>``).

    ``raw`` is the message bytes (NOT 0x-prefixed). The dialog
    can render it as UTF-8 when it decodes cleanly, else hex.
    """
    from_addr: str
    raw: bytes
    origin: Optional[str] = None


@dataclass
class TypedDataSigningRequest:
    """``eth_signTypedData_v4`` request — EIP-712 structured data.

    ``typed_data`` is the parsed JSON object with ``domain``,
    ``types``, ``primaryType``, and ``message``. Dialog renders
    a tree of the message; signer uses ``encode_typed_data``
    + ``Account.sign_hash`` (hot wallet) or Ledger's typed-data
    flow (Ledger).
    """
    from_addr: str
    typed_data: dict
    origin: Optional[str] = None


def parse_personal_sign_params(
    params: list, *, origin: Optional[str] = None,
) -> MessageSigningRequest:
    """Parse the dapp's ``personal_sign`` params into a typed
    request.

    EIP-191 wire shape: ``personal_sign(message, address)`` —
    yes, message FIRST in personal_sign, the opposite of
    ``eth_sign(address, message)``. Both message and address
    are hex-encoded. We accept either order by sniffing which
    arg looks like an address; lots of dapps in the wild get
    the order wrong.

    ``message`` may also arrive as a plain UTF-8 string with no
    ``0x`` prefix (some legacy paths); treat anything that isn't
    valid hex as the literal string."""
    if len(params) < 2:
        raise SignerError("personal_sign expects [message, address]")
    a, b = params[0], params[1]
    # Sniff which arg is the address.
    def _looks_like_addr(x) -> bool:
        return (isinstance(x, str) and x.startswith("0x")
                and len(x) == 42)
    if _looks_like_addr(a) and not _looks_like_addr(b):
        message_raw, addr = b, a
    else:
        message_raw, addr = a, b
    try:
        from_addr = to_checksum_address(addr)
    except Exception as e:
        raise SignerError(f"invalid signer address: {e}") from e
    raw = _decode_message_bytes(message_raw)
    return MessageSigningRequest(from_addr=from_addr, raw=raw, origin=origin)


def _decode_message_bytes(value) -> bytes:
    """``personal_sign`` messages arrive as hex (``0x...``) or as
    plain UTF-8 (legacy). Try hex first; fall back to UTF-8.
    Returns the raw bytes BEFORE the EIP-191 prefix is added."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    s = str(value)
    if s.startswith("0x") or s.startswith("0X"):
        try:
            return bytes.fromhex(s[2:])
        except ValueError:
            pass
    return s.encode("utf-8")


def parse_typed_data_params(
    params: list, *, origin: Optional[str] = None,
) -> TypedDataSigningRequest:
    """Parse the dapp's ``eth_signTypedData_v4`` params:
    ``[address, typedData]``. ``typedData`` may be a JSON string
    or an already-parsed object (some clients serialise; some
    don't — accept both)."""
    if len(params) < 2:
        raise SignerError(
            "eth_signTypedData_v4 expects [address, typedData]",
        )
    addr_raw, data_raw = params[0], params[1]
    try:
        from_addr = to_checksum_address(addr_raw)
    except Exception as e:
        raise SignerError(f"invalid signer address: {e}") from e
    if isinstance(data_raw, str):
        import json as _json
        try:
            typed = _json.loads(data_raw)
        except Exception as e:
            raise SignerError(f"typed data not valid JSON: {e}") from e
    else:
        typed = data_raw
    if not isinstance(typed, dict):
        raise SignerError("typed data must be an object")
    return TypedDataSigningRequest(
        from_addr=from_addr, typed_data=typed, origin=origin,
    )


class Signer(ABC):
    """Backend that can produce a signed payload for a given
    request. Three flavours of request:

    - ``SigningRequest`` → signed-and-RLP-encoded transaction
      bytes, ready for ``eth_sendRawTransaction``.
    - ``MessageSigningRequest`` → ``personal_sign`` 65-byte
      ECDSA signature.
    - ``TypedDataSigningRequest`` → EIP-712 typed-data signature.

    ``can_sign`` lets the caller refuse up front when the
    ``from`` address has no known signer."""

    @abstractmethod
    def can_sign(self, address: str) -> bool:
        ...

    @abstractmethod
    def sign(self, req: SigningRequest, chain) -> bytes:
        """Return the raw bytes to hand to ``eth_sendRawTransaction``."""

    @abstractmethod
    def sign_message(self, req: MessageSigningRequest) -> bytes:
        """Return the 65-byte ``personal_sign`` signature for the
        EIP-191 prefixed message. No chain context — message
        signatures are chain-agnostic."""

    @abstractmethod
    def sign_typed_data(self, req: TypedDataSigningRequest) -> bytes:
        """Return the 65-byte EIP-712 signature."""


class SignerBridge(QObject):
    """Cross-thread coordinator. Construct on the Qt main thread
    (parent it to MainWindow); pass to ``RpcServer`` so the aiohttp
    handler can hand requests off. The receiving slot in MainWindow
    opens the dialog and resolves the future when done."""

    # (request, future) — the slot connected to this signal must
    # eventually call bridge.resolve(future, hash) on success or
    # bridge.reject(future, exc) on cancel/error, otherwise the
    # awaiting RPC coroutine never resumes.
    request_received = Signal(object, object)
    # Fired from the RPC thread when a dapp adds a chain via
    # ``wallet_addEthereumChain``. The UI uses this to append the
    # new chain to the toolbar combo (and kick icon discovery)
    # without waiting for the user to restart.
    chain_added = Signal(int)

    async def submit_async(self, req) -> str:
        """Called from the aiohttp event loop. Emits the signal
        (cross-thread, queued onto the Qt main loop) and awaits the
        future. ``req`` is one of ``SigningRequest``,
        ``MessageSigningRequest``, or ``TypedDataSigningRequest`` —
        the slot dispatches by type. The future's resolved value
        is a 0x-prefixed hex string: a tx hash for ``SigningRequest``,
        a 65-byte signature for the message types. Raises
        ``SignerError`` on cancel / signing failure."""
        fut: Future = Future()
        self.request_received.emit(req, fut)
        try:
            return await asyncio.wrap_future(fut)
        except Exception as e:
            # asyncio.wrap_future will surface set_exception causes
            # directly; re-wrap unknowns as SignerError so callers
            # always see the same shape.
            if isinstance(e, SignerError):
                raise
            raise SignerError(str(e)) from e

    def resolve(self, fut: Future, tx_hash: str) -> None:
        if not fut.done():
            fut.set_result(tx_hash)

    def reject(self, fut: Future, error: Exception) -> None:
        if not fut.done():
            fut.set_exception(error)


def explain_rpc_error(e: Exception) -> str:
    """Best-effort extract a human-readable message from an
    upstream JSON-RPC error. web3.py 7's ``Web3RPCError`` carries
    the parsed dict on ``.rpc_response``; some other paths bubble
    the dict's ``repr()`` as the exception text. Either way the
    ``{message}`` field is what we want to show — the raw repr
    (``"{'message': '…', 'code': -32000}"``) is what the user
    complained about."""
    try:
        from web3.exceptions import Web3RPCError
        if isinstance(e, Web3RPCError):
            resp = getattr(e, "rpc_response", None)
            if isinstance(resp, dict):
                err = resp.get("error")
                if isinstance(err, dict) and err.get("message"):
                    return str(err["message"])
    except ImportError:
        pass
    text = str(e)
    # Some web3 versions stringify the response as a Python dict
    # literal rather than wrapping in Web3RPCError — recover the
    # message via ast.literal_eval rather than regex.
    if text.startswith("{") and "'message'" in text:
        import ast
        try:
            data = ast.literal_eval(text)
            if isinstance(data, dict) and data.get("message"):
                return str(data["message"])
        except (ValueError, SyntaxError):
            pass
    return text


class SignAndBroadcastWorker(QThread):
    """Off-main-thread orchestrator: ``signer.sign(req, chain)`` ->
    ``EthClient(chain).send_raw_transaction(...)`` -> emit the tx
    hash. Signing on a Ledger blocks for several seconds while the
    user confirms on the device; the worker keeps the UI responsive
    in the meantime."""

    broadcast = Signal(str, object)   # (tx hash 0x-prefixed, raw signed hex)
    failed = Signal(str)              # human-readable reason

    def __init__(self, signer: Signer, req: SigningRequest, chain,
                 parent=None):
        super().__init__(parent)
        self._signer = signer
        self._req = req
        self._chain = chain

    def run(self) -> None:
        try:
            raw = self._signer.sign(self._req, self._chain)
        except SignerError as e:
            self.failed.emit(str(e))
            return
        except Exception as e:
            log.exception("signer raised unexpectedly")
            self.failed.emit(f"Signing failed: {e}")
            return
        try:
            # Import lazily so the test suite can patch EthClient at
            # module level without the cross-import dance.
            from .chain import EthClient
            client = EthClient(self._chain)
            tx_hash = client.send_raw_transaction(raw)
        except Exception as e:
            log.exception("broadcast failed")
            self.failed.emit(f"Broadcast failed: {explain_rpc_error(e)}")
            return
        # Keep the raw signed bytes so the pending-tx watcher can
        # re-broadcast if the RPC silently drops the tx. Normalise to a
        # 0x-hex string for storage.
        if isinstance(raw, str):
            raw_hex = raw if raw.startswith("0x") else "0x" + raw
        else:
            raw_hex = "0x" + bytes(raw).hex()
        self.broadcast.emit(tx_hash, raw_hex)


class SignMessageWorker(QThread):
    """Off-main-thread orchestrator for personal_sign and
    eth_signTypedData_v4. Hot wallets call scrypt + Account.sign_*
    here (slow), Ledger calls block waiting for the user to confirm
    on the device — either way the work must not block the UI.

    Accepts either a ``MessageSigningRequest`` (personal_sign) or
    a ``TypedDataSigningRequest`` (EIP-712); dispatches to the
    matching signer method. The emitted signature is a 0x-prefixed
    65-byte hex string ready to hand back to the dapp."""

    signed = Signal(str)      # 0x-prefixed signature hex
    failed = Signal(str)      # human-readable reason

    def __init__(self, signer: Signer, req, parent=None):
        super().__init__(parent)
        self._signer = signer
        self._req = req

    def run(self) -> None:
        try:
            if isinstance(self._req, MessageSigningRequest):
                raw = self._signer.sign_message(self._req)
            elif isinstance(self._req, TypedDataSigningRequest):
                raw = self._signer.sign_typed_data(self._req)
            else:
                raise SignerError(
                    f"unsupported request type {type(self._req).__name__}"
                )
        except SignerError as e:
            self.failed.emit(str(e))
            return
        except Exception as e:
            log.exception("message signer raised unexpectedly")
            self.failed.emit(f"Signing failed: {e}")
            return
        if not isinstance(raw, (bytes, bytearray)) or len(raw) != 65:
            self.failed.emit(
                f"unexpected signature shape: {type(raw).__name__} "
                f"len={len(raw) if isinstance(raw, (bytes, bytearray)) else '?'}",
            )
            return
        self.signed.emit("0x" + bytes(raw).hex())
