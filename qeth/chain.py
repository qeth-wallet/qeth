"""Chain access — uniform interface over JSON-RPC.

Currently implemented with stdlib urllib and manual hex encoding.
Designed so callers can be migrated to web3.py later without changes:
method names, argument types, and return types mirror web3.py's
``w3.eth.*`` (sync flavor)."""

import json
import urllib.request
from decimal import Decimal

from .chains import Chain

USER_AGENT = "qeth/0.1"

# Native asset has 18 decimals on every EVM chain we currently support.
_WEI_PER_ETHER = Decimal(10) ** 18

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


def wei_to_ether(wei: int) -> Decimal:
    """Convert a wei int to a Decimal ether amount.

    Always prefer this over ``wei / 1e18`` — float arithmetic on on-chain
    amounts silently loses precision (double has ~15-17 sig digits; wei
    has 18 decimal places) and round-trips badly through display formats.
    """
    return Decimal(int(wei)) / _WEI_PER_ETHER


class ChainError(Exception):
    """JSON-RPC error from the upstream node."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class EthClient:
    """Synchronous chain client backed by JSON-RPC over HTTP.

    Method shape mirrors web3.py's ``w3.eth`` so swapping the
    implementation later is a drop-in at this module boundary.
    """

    def __init__(self, chain: Chain, *, timeout: float = 15.0):
        self.chain = chain
        self.timeout = timeout

    # --- low-level ---------------------------------------------------------

    def rpc(self, method: str, params: list | None = None):
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": method, "params": params or [],
        }
        req = urllib.request.Request(
            self.chain.rpc_url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                # Some RPC providers (notably DRPC behind Cloudflare) reject
                # the default Python-urllib/x.y User-Agent.
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            data = json.loads(r.read())
        if data.get("error"):
            err = data["error"]
            raise ChainError(err.get("code", -1), err.get("message", "rpc error"))
        return data.get("result")

    # --- reads (mirroring web3.eth) ----------------------------------------

    def get_balance(self, address: str, block: str = "latest") -> int:
        """Native balance in wei."""
        return int(self.rpc("eth_getBalance", [address, block]), 16)

    def get_block_number(self) -> int:
        return int(self.rpc("eth_blockNumber"), 16)

    def chain_id(self) -> int:
        return int(self.rpc("eth_chainId"), 16)

    def get_transaction_count(self, address: str, block: str = "pending") -> int:
        return int(self.rpc("eth_getTransactionCount", [address, block]), 16)

    def gas_price(self) -> int:
        return int(self.rpc("eth_gasPrice"), 16)

    def max_priority_fee(self) -> int:
        return int(self.rpc("eth_maxPriorityFeePerGas"), 16)

    def estimate_gas(self, tx: dict) -> int:
        return int(self.rpc("eth_estimateGas", [tx]), 16)

    def call(self, tx: dict, block: str = "latest") -> str:
        """Returns hex-encoded return data (with 0x prefix)."""
        return self.rpc("eth_call", [tx, block])

    # --- writes ------------------------------------------------------------

    def send_raw_transaction(self, raw_tx: bytes | str) -> str:
        """Returns the transaction hash."""
        if isinstance(raw_tx, (bytes, bytearray)):
            raw_tx = "0x" + raw_tx.hex()
        return self.rpc("eth_sendRawTransaction", [raw_tx])

    # --- batch helpers -----------------------------------------------------

    def multicall_erc20_balances(
        self, tokens: list[str], holder: str, batch_size: int = 100,
    ) -> dict[str, int]:
        """Fetch ERC-20 ``balanceOf(holder)`` for every address in ``tokens``
        via Multicall3.aggregate3.

        Returns ``{token_lower: balance_raw}``. Tokens whose inner call
        reverted or returned malformed data are silently omitted, so the
        caller should treat absence as "unknown" rather than zero.

        Two robustness measures matter for "show all" mode where the
        token set can grow into the hundreds:
        - ``allowFailure=True`` per inner call so a single malicious
          contract whose balanceOf reverts can't sink the whole batch.
        - Batched in chunks (default 100 inner calls per round-trip) so
          we stay under the eth_call gas/size limit.
        """
        if not tokens:
            return {}
        from eth_abi import decode, encode

        addr_hex = holder[2:].lower() if holder.startswith("0x") else holder.lower()
        balof_calldata = _SEL_BALANCE_OF + b"\x00" * 12 + bytes.fromhex(addr_hex)

        out: dict[str, int] = {}
        for start in range(0, len(tokens), batch_size):
            batch = tokens[start:start + batch_size]
            calls = [(t, True, balof_calldata) for t in batch]
            calldata = _SEL_AGGREGATE3 + encode(
                ["(address,bool,bytes)[]"], [calls]
            )
            try:
                result_hex = self.call(
                    {"to": MULTICALL3, "data": "0x" + calldata.hex()}
                )
                decoded = decode(
                    ["(bool,bytes)[]"], bytes.fromhex(result_hex[2:])
                )[0]
            except Exception:
                continue
            for (token, _, _), (success, retdata) in zip(calls, decoded):
                if success and len(retdata) >= 32:
                    out[token.lower()] = int.from_bytes(retdata[:32], "big")
        return out

    def multicall_erc20_metadata(
        self, tokens: list[str], batch_size: int = 30,
    ) -> dict[str, dict]:
        """Fetch (name, symbol, decimals) for every contract in ``tokens``
        via Multicall3.aggregate3 (3 inner calls per token).

        Returns ``{token_lower: {"symbol": str, "name": str, "decimals": int}}``.
        Tokens for which any inner call reverted are omitted. Legacy
        tokens that return bytes32 instead of string for name/symbol
        (MKR-style) are decoded by stripping NUL padding.

        Batched in chunks (default 30 tokens = 90 inner calls per
        round-trip) so the eth_call response stays manageable for large
        discovery sets.
        """
        if not tokens:
            return {}
        from eth_abi import decode, encode

        out: dict[str, dict] = {}
        for start in range(0, len(tokens), batch_size):
            batch = tokens[start:start + batch_size]
            calls = []
            for t in batch:
                # allowFailure=True so one weird contract doesn't abort
                # the whole aggregate3 (returns success=False, empty
                # retdata for the offending call instead).
                calls.append((t, True, _SEL_NAME))
                calls.append((t, True, _SEL_SYMBOL))
                calls.append((t, True, _SEL_DECIMALS))
            calldata = _SEL_AGGREGATE3 + encode(
                ["(address,bool,bytes)[]"], [calls]
            )
            try:
                result_hex = self.call(
                    {"to": MULTICALL3, "data": "0x" + calldata.hex()}
                )
                decoded = decode(
                    ["(bool,bytes)[]"], bytes.fromhex(result_hex[2:])
                )[0]
            except Exception:
                continue

            for i, token in enumerate(batch):
                name_ok, name_data = decoded[i * 3]
                sym_ok, sym_data = decoded[i * 3 + 1]
                dec_ok, dec_data = decoded[i * 3 + 2]
                name = _decode_string_or_bytes32(name_data) if name_ok else ""
                symbol = _decode_string_or_bytes32(sym_data) if sym_ok else ""
                if dec_ok and len(dec_data) >= 32:
                    decimals = int.from_bytes(dec_data[:32], "big")
                else:
                    decimals = 18
                # Require at least a symbol — entries with empty symbol
                # are unusable for display.
                if symbol:
                    out[token.lower()] = {
                        "symbol": symbol, "name": name,
                        "decimals": int(decimals),
                    }
        return out


def _decode_string_or_bytes32(data: bytes) -> str:
    """Some legacy ERC-20s (MKR, …) return bytes32 instead of string for
    name/symbol. Try string first, fall back to bytes32-stripped."""
    from eth_abi import decode
    try:
        return decode(["string"], data)[0]
    except Exception:
        try:
            b = decode(["bytes32"], data)[0]
            return b.rstrip(b"\x00").decode("utf-8", errors="replace")
        except Exception:
            return ""
