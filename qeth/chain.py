"""Chain access via web3.py + a batching Multicall3 context manager.

The high-level eth_* helpers (``get_balance``, ``chain_id``, …) delegate
straight to ``Web3.eth.*``. The bespoke ``Multicall`` context manager
queues up calls and flushes them as ``Multicall3.aggregate3`` batches
on context exit, returning per-call ``_Pending`` slots whose ``.value``
attribute is populated when the context closes:

    with client.multicall() as mc:
        usdc_bal = mc.balance_of(USDC, holder)
        usdt_sym = mc.symbol(USDT)
    print(usdc_bal.value, usdt_sym.value)

Chunking, ``allowFailure``-per-inner-call, decoding, and the bytes32 vs
string fallback for legacy MKR-style tokens are all handled by the
context manager so callers never see the raw aggregate3 wire format.
"""

import logging
from decimal import Decimal
from typing import Callable, Optional

from .chains import Chain

log = logging.getLogger("qeth.chain")


# The web3 / eth_abi / requests stack costs ~700 ms to import. The
# UI doesn't need any of it until an RPC call actually happens (i.e.
# in a worker thread, post-startup), so the imports live inside
# ``_ensure_heavy_imports`` and are pulled in lazily on first
# EthClient instantiation. After that, the symbols sit in this
# module's globals and the rest of the file references them
# unmodified — keeps the deferred path invisible to call sites.
def _ensure_heavy_imports() -> None:
    g = globals()
    if "Web3" in g:
        return
    import requests
    from eth_abi import decode as abi_decode
    from eth_abi import encode as abi_encode
    from web3 import Web3
    from web3.exceptions import Web3RPCError
    from web3.middleware import ExtraDataToPOAMiddleware
    from web3.providers.rpc import HTTPProvider
    g["requests"] = requests
    g["abi_decode"] = abi_decode
    g["abi_encode"] = abi_encode
    g["Web3"] = Web3
    g["Web3RPCError"] = Web3RPCError
    g["ExtraDataToPOAMiddleware"] = ExtraDataToPOAMiddleware
    g["HTTPProvider"] = HTTPProvider

from . import USER_AGENT

# Native asset has 18 decimals on every EVM chain we currently support.
_WEI_PER_ETHER = Decimal(10) ** 18


def wei_to_ether(wei: int) -> Decimal:
    """Convert a wei int to a Decimal ether amount.

    Always prefer this over ``wei / 1e18`` — float arithmetic on on-chain
    amounts silently loses precision (double has ~15-17 sig digits; wei
    has 18 decimal places) and round-trips badly through display formats.
    """
    return Decimal(int(wei)) / _WEI_PER_ETHER


class ChainError(Exception):
    """JSON-RPC error response from the upstream node."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# Multicall3 is deployed at the same address on every EVM chain we support.
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

# 4-byte function selectors. Pre-computed (Keccak isn't in stdlib hashlib).
# Verified against the published ABIs:
#   aggregate3((address,bool,bytes)[]) -> 0x82ad56cb
#   balanceOf(address)                 -> 0x70a08231
#   name()                             -> 0x06fdde03
#   symbol()                           -> 0x95d89b41
#   decimals()                         -> 0x313ce567
_SEL_AGGREGATE3 = bytes.fromhex("82ad56cb")
_SEL_BALANCE_OF = bytes.fromhex("70a08231")
_SEL_NAME = bytes.fromhex("06fdde03")
_SEL_SYMBOL = bytes.fromhex("95d89b41")
_SEL_DECIMALS = bytes.fromhex("313ce567")


def _build_session():
    """A requests.Session with our User-Agent. DRPC's Cloudflare front
    rejects the default ``python-requests/x.y`` UA (HTTP 403, "error
    code: 1010"), so every HTTP call out of EthClient needs this set."""
    _ensure_heavy_imports()
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


class EthClient:
    """Thin wrapper around ``web3.Web3`` that provides:

    - a ``ChainError``-raising ``rpc(method, params)`` escape hatch
    - the simple eth_* helpers we use throughout the wallet
    - a ``Multicall3.aggregate3`` context manager via ``client.multicall()``
    - high-level ``multicall_erc20_balances`` / ``multicall_erc20_metadata``
      built on top of that context manager

    Internally everything goes through a single requests Session so the
    UA header that DRPC's CDN requires is always applied.
    """

    def __init__(self, chain: Chain, *, timeout: float = 15.0):
        _ensure_heavy_imports()
        self.chain = chain
        self.timeout = timeout
        self._session = _build_session()
        self._w3 = Web3(HTTPProvider(
            chain.rpc_url,
            request_kwargs={"timeout": timeout},
            session=self._session,
        ))
        # PoA chains (BSC, Polygon-PoS, Avalanche C-Chain, …) put
        # validator signatures in the block header's extraData
        # field, which is 65–280 bytes instead of the 32 web3.py
        # validates by default — and the validation runs on every
        # ``eth_getBlockBy*`` response, including the raw
        # ``rpc()`` path (web3.py 7 routes raw requests through
        # the middleware onion too). The middleware truncates
        # extraData to 32 bytes on the way back; harmless on
        # non-PoA chains because qeth never reads extraData
        # anyway, so we inject unconditionally instead of gating
        # on a per-chain flag we'd have to maintain.
        self._w3.middleware_onion.inject(
            ExtraDataToPOAMiddleware, layer=0,
        )

    @property
    def w3(self):
        """Underlying ``Web3`` instance — for callers that need the full
        web3.py surface (contracts, filters, account abstractions, etc.).
        Type hint omitted so this module can be imported without web3."""
        return self._w3

    # --- low-level ---------------------------------------------------------

    def rpc(self, method: str, params: Optional[list] = None):
        """Direct JSON-RPC call. Raises ``ChainError`` on RPC-level errors."""
        try:
            return self._w3.manager.request_blocking(method, params or [])
        except Web3RPCError as e:
            rpc_resp = getattr(e, "rpc_response", None) or {}
            err = rpc_resp.get("error") or {}
            raise ChainError(err.get("code", -1), err.get("message") or str(e))

    # --- reads (mirroring web3.eth) ---------------------------------------

    def get_balance(self, address: str, block: str = "latest") -> int:
        """Native balance in wei."""
        return int(self._w3.eth.get_balance(address, block))

    def get_block_number(self) -> int:
        return int(self._w3.eth.block_number)

    def chain_id(self) -> int:
        return int(self._w3.eth.chain_id)

    def get_transaction_count(self, address: str, block: str = "pending") -> int:
        return int(self._w3.eth.get_transaction_count(address, block))

    def gas_price(self) -> int:
        return int(self._w3.eth.gas_price)

    def max_priority_fee(self) -> int:
        return int(self._w3.eth.max_priority_fee)

    def estimate_gas(self, tx: dict) -> int:
        return int(self._w3.eth.estimate_gas(tx))

    def call(self, tx: dict, block: str = "latest") -> str:
        """Returns hex-encoded return data (with 0x prefix)."""
        result = self._w3.eth.call(tx, block)
        if hasattr(result, "to_0x_hex"):
            return result.to_0x_hex()
        b = bytes(result)
        return "0x" + b.hex()

    # --- writes ------------------------------------------------------------

    def send_raw_transaction(self, raw_tx) -> str:
        """Returns the transaction hash as a 0x-prefixed hex string."""
        if isinstance(raw_tx, str):
            raw_tx = bytes.fromhex(
                raw_tx[2:] if raw_tx.startswith("0x") else raw_tx
            )
        tx_hash = self._w3.eth.send_raw_transaction(raw_tx)
        if hasattr(tx_hash, "to_0x_hex"):
            return tx_hash.to_0x_hex()
        return "0x" + bytes(tx_hash).hex()

    # --- batch helpers (built on Multicall context manager) ---------------

    def multicall(self, *, batch_size: int = 100) -> "Multicall":
        """Open a batching context. Calls queued via ``mc.add(...)`` or the
        ERC-20 helpers (``balance_of``, ``name``, ``symbol``, ``decimals``)
        are flushed as ``aggregate3`` batches when the context exits."""
        return Multicall(self, batch_size=batch_size)

    def multicall_erc20_balances(
        self, tokens: list[str], holder: str, batch_size: int = 100,
    ) -> dict[str, int]:
        """Fetch ERC-20 ``balanceOf(holder)`` for every address in
        ``tokens``. Tokens whose inner call reverted or returned
        malformed data are silently omitted, so the caller should treat
        absence as "unknown" rather than zero."""
        if not tokens:
            return {}
        with self.multicall(batch_size=batch_size) as mc:
            queued = [(t, mc.balance_of(t, holder)) for t in tokens]
        return {t.lower(): f.value for t, f in queued
                if f.success and f.value is not None}

    def multicall_erc20_metadata(
        self, tokens: list[str], batch_size: int = 30,
    ) -> dict[str, dict]:
        """Fetch (name, symbol, decimals) for every contract.

        Returns ``{token_lower: {"symbol", "name", "decimals"}}``. Tokens
        whose ``symbol`` call returned empty or reverted are dropped (an
        entry with no symbol is unusable for display). Legacy MKR-style
        tokens returning bytes32 instead of string are decoded by the
        context manager's bytes32 fallback.
        """
        if not tokens:
            return {}
        # 3 inner calls per token; flush more often to stay under
        # eth_call gas/size limits.
        with self.multicall(batch_size=batch_size * 3) as mc:
            queued = [
                (t, mc.name(t), mc.symbol(t), mc.decimals(t))
                for t in tokens
            ]
        out: dict[str, dict] = {}
        for token, name_f, sym_f, dec_f in queued:
            symbol = sym_f.value if sym_f.success else ""
            if not symbol:
                continue
            name = (name_f.value or "") if name_f.success else ""
            decimals = (
                dec_f.value if (dec_f.success and dec_f.value is not None)
                else 18
            )
            out[token.lower()] = {
                "symbol": symbol, "name": name, "decimals": int(decimals),
            }
        return out


# --- Multicall context manager --------------------------------------------

class _Pending:
    """A slot for a multicall result; filled in when the enclosing
    ``Multicall`` context manager exits.

    Inspect ``.success`` (True/False) to tell whether the inner call
    succeeded, and read ``.value`` for the decoded result. ``.value`` is
    ``None`` if the call reverted; you'll almost always want to check
    ``.success`` first.
    """

    __slots__ = ("success", "raw", "value", "_decoder")

    def __init__(self, decoder: Optional[Callable] = None):
        self.success: Optional[bool] = None  # None until flushed
        self.raw: Optional[bytes] = None
        self.value = None
        self._decoder = decoder


class Multicall:
    """Context manager that queues calls and flushes them through
    ``Multicall3.aggregate3`` when the context exits.

    Calls are batched in chunks of ``batch_size`` (default 100) and use
    ``allowFailure=True`` per inner call, so one reverting contract can
    never sink the batch.

    Convenience methods are provided for the common ERC-20 reads
    (``balance_of``, ``name``, ``symbol``, ``decimals``); use ``add()``
    directly for arbitrary call data + an optional decoder.
    """

    def __init__(self, client: EthClient, *, batch_size: int = 100):
        self.client = client
        self.batch_size = batch_size
        self._queued: list[tuple[str, bytes, _Pending]] = []

    def __enter__(self) -> "Multicall":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Don't flush if the with-block raised — caller is bailing.
        if exc_type is None:
            self._flush()
        return False

    # ---- queuing API ----------------------------------------------------

    def add(self, target: str, calldata: bytes,
            *, decoder: Optional[Callable] = None) -> _Pending:
        """Queue an arbitrary call to ``target`` with ``calldata``.

        Returns a ``_Pending`` whose ``.value`` will be populated when
        the context exits. ``decoder`` is invoked on the raw return
        bytes; if omitted, ``.value`` is the raw bytes.
        """
        p = _Pending(decoder)
        self._queued.append((target, calldata, p))
        return p

    def balance_of(self, token: str, holder: str) -> _Pending:
        """Queue ``ERC20.balanceOf(holder)`` on ``token``; ``.value`` is
        the raw uint256 balance."""
        addr_hex = (
            holder[2:].lower() if holder.startswith("0x") else holder.lower()
        )
        calldata = _SEL_BALANCE_OF + b"\x00" * 12 + bytes.fromhex(addr_hex)
        return self.add(token, calldata, decoder=_decode_uint256)

    def name(self, token: str) -> _Pending:
        return self.add(token, _SEL_NAME, decoder=_decode_string_or_bytes32)

    def symbol(self, token: str) -> _Pending:
        return self.add(token, _SEL_SYMBOL, decoder=_decode_string_or_bytes32)

    def decimals(self, token: str) -> _Pending:
        return self.add(token, _SEL_DECIMALS, decoder=_decode_uint256)

    # ---- internal -------------------------------------------------------

    def _flush(self) -> None:
        for start in range(0, len(self._queued), self.batch_size):
            batch = self._queued[start:start + self.batch_size]
            calls = [(target, True, data) for target, data, _ in batch]
            calldata = _SEL_AGGREGATE3 + abi_encode(
                ["(address,bool,bytes)[]"], [calls]
            )
            try:
                result_hex = self.client.call(
                    {"to": MULTICALL3, "data": "0x" + calldata.hex()}
                )
                decoded = abi_decode(
                    ["(bool,bytes)[]"], bytes.fromhex(result_hex[2:])
                )[0]
            except Exception as e:
                log.debug("multicall batch failed: %s", e)
                for _, _, pending in batch:
                    pending.success = False
                continue
            for (_, _, pending), (success, retdata) in zip(batch, decoded):
                pending.success = bool(success)
                if not success:
                    continue
                pending.raw = retdata
                if pending._decoder is None:
                    pending.value = retdata
                    continue
                try:
                    pending.value = pending._decoder(retdata)
                except Exception:
                    pending.success = False
                    pending.value = None


def _decode_uint256(data: bytes):
    return int.from_bytes(data[:32], "big") if len(data) >= 32 else None


def _decode_string_or_bytes32(data: bytes) -> str:
    """Some legacy ERC-20s (MKR, …) return bytes32 instead of string for
    name/symbol. Try string first, fall back to bytes32-stripped."""
    try:
        return abi_decode(["string"], data)[0]
    except Exception:
        try:
            b = abi_decode(["bytes32"], data)[0]
            return b.rstrip(b"\x00").decode("utf-8", errors="replace")
        except Exception:
            return ""
