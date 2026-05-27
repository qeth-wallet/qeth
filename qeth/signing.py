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


class Signer(ABC):
    """Backend that can produce a signed-and-RLP-encoded transaction
    for a given finalised request. ``can_sign`` lets the dialog
    refuse the request up front when the ``from`` address has no
    known signer."""

    @abstractmethod
    def can_sign(self, address: str) -> bool:
        ...

    @abstractmethod
    def sign(self, req: SigningRequest, chain) -> bytes:
        """Return the raw bytes to hand to ``eth_sendRawTransaction``."""


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

    async def submit_async(self, req: SigningRequest) -> str:
        """Called from the aiohttp event loop. Emits the signal
        (cross-thread, queued onto the Qt main loop) and awaits the
        future. Returns the broadcast tx hash, or raises
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

    broadcast = Signal(str)   # tx hash, 0x-prefixed
    failed = Signal(str)      # human-readable reason

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
        self.broadcast.emit(tx_hash)
