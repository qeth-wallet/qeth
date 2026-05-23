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
_SEL_AGGREGATE3 = bytes.fromhex("82ad56cb")
_SEL_BALANCE_OF = bytes.fromhex("70a08231")


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
        self, tokens: list[str], holder: str,
    ) -> dict[str, int]:
        """Fetch ERC-20 ``balanceOf(holder)`` for every address in ``tokens``
        in a single Multicall3.aggregate3 call.

        Returns ``{token_lower: balance_raw}``. Tokens whose inner call
        reverted or returned malformed data are silently omitted, so the
        caller should treat absence as "unknown" rather than zero.
        """
        if not tokens:
            return {}
        from eth_abi import decode, encode

        addr_hex = holder[2:].lower() if holder.startswith("0x") else holder.lower()
        balof_calldata = _SEL_BALANCE_OF + b"\x00" * 12 + bytes.fromhex(addr_hex)
        calls = [(t, False, balof_calldata) for t in tokens]
        calldata = _SEL_AGGREGATE3 + encode(
            ["(address,bool,bytes)[]"], [calls]
        )
        result_hex = self.call({"to": MULTICALL3, "data": "0x" + calldata.hex()})
        if not result_hex.startswith("0x"):
            return {}
        decoded = decode(
            ["(bool,bytes)[]"], bytes.fromhex(result_hex[2:])
        )[0]
        out: dict[str, int] = {}
        for (token, _, _), (success, retdata) in zip(calls, decoded):
            if success and len(retdata) >= 32:
                out[token.lower()] = int.from_bytes(retdata[:32], "big")
        return out
