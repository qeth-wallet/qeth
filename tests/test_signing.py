"""Hermetic tests for the signing seam — parse, gas policy, bridge.

Network-touching code (GasSuggestionWorker.run, EthClient) isn't
exercised here — the policy logic was deliberately extracted into a
pure ``apply_gas_policy`` so we can lock it in without spinning a
chain or QThread up.
"""

import asyncio
from concurrent.futures import Future

import pytest

from qeth.signing import (
    SignerBridge, SignerError, SigningRequest,
    parse_send_transaction_params,
)


CHAIN_ID = 1
ADDR_LOWER = "0x7a16ff8270133f063aab6c9977183d9e72835428"
ADDR_CHECKSUM = "0x7a16fF8270133F063aAb6C9977183D9e72835428"
TOKEN_LOWER = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
TOKEN_CHECKSUM = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


# --- parse_send_transaction_params ---------------------------------------

class TestParseSendTransactionParams:
    def test_happy_path_all_fields(self):
        req = parse_send_transaction_params([{
            "from": ADDR_CHECKSUM,
            "to": TOKEN_CHECKSUM,
            "value": "0x10",                  # 16 wei
            "data": "0xabcd",
            "gas": "0x5208",                  # 21000
            "maxFeePerGas": "0x77359400",     # 2 gwei
            "maxPriorityFeePerGas": "0x3b9aca00",  # 1 gwei
            "nonce": "0x7",
        }], CHAIN_ID)
        assert req.chain_id == CHAIN_ID
        assert req.from_addr == ADDR_CHECKSUM
        assert req.to_addr == TOKEN_CHECKSUM
        assert req.value_wei == 16
        assert req.data == "0xabcd"
        assert req.gas == 21000
        assert req.max_fee_per_gas == 2 * 10**9
        assert req.max_priority_fee_per_gas == 10**9
        assert req.nonce == 7

    def test_lowercase_addresses_normalised_to_checksum(self):
        """dapps frequently send lower-cased addresses; web3.py
        refuses them downstream so the parse seam has to fix it."""
        req = parse_send_transaction_params([{
            "from": ADDR_LOWER,
            "to": TOKEN_LOWER,
        }], CHAIN_ID)
        assert req.from_addr == ADDR_CHECKSUM
        assert req.to_addr == TOKEN_CHECKSUM

    def test_missing_to_means_contract_creation(self):
        req = parse_send_transaction_params([{
            "from": ADDR_CHECKSUM,
            "data": "0x6080604052",
        }], CHAIN_ID)
        assert req.to_addr is None
        assert req.data == "0x6080604052"

    def test_input_field_aliases_data(self):
        """Some dapps send ``input`` instead of ``data`` for the
        calldata; both are accepted (Geth historically accepted
        either)."""
        req = parse_send_transaction_params([{
            "from": ADDR_CHECKSUM,
            "to": TOKEN_CHECKSUM,
            "input": "0xdeadbeef",
        }], CHAIN_ID)
        assert req.data == "0xdeadbeef"

    def test_missing_value_defaults_to_zero(self):
        req = parse_send_transaction_params([{
            "from": ADDR_CHECKSUM,
            "to": TOKEN_CHECKSUM,
        }], CHAIN_ID)
        assert req.value_wei == 0

    def test_legacy_gas_price_passed_through(self):
        req = parse_send_transaction_params([{
            "from": ADDR_CHECKSUM,
            "to": TOKEN_CHECKSUM,
            "gasPrice": "0x12a05f200",   # 5 gwei
        }], CHAIN_ID)
        assert req.gas_price == 5 * 10**9

    def test_missing_from_raises(self):
        with pytest.raises(SignerError):
            parse_send_transaction_params([{"to": TOKEN_CHECKSUM}], CHAIN_ID)

    def test_empty_params_raises(self):
        with pytest.raises(SignerError):
            parse_send_transaction_params([], CHAIN_ID)

    def test_non_object_param_raises(self):
        with pytest.raises(SignerError):
            parse_send_transaction_params(["0xdeadbeef"], CHAIN_ID)


# --- apply_gas_policy (pure) --------------------------------------------

# Import the policy from the plugins module; this isn't a UI test —
# the function is pure and can run without Qt.
from qeth.plugins.transactions import apply_gas_policy


def _req(**overrides) -> SigningRequest:
    base = dict(
        chain_id=CHAIN_ID,
        from_addr=ADDR_CHECKSUM,
        to_addr=TOKEN_CHECKSUM,
        value_wei=0,
        data="0x",
    )
    base.update(overrides)
    return SigningRequest(**base)


class TestApplyGasPolicy1559:
    def test_gas_is_estimate_times_one_point_five(self):
        out = apply_gas_policy(
            estimated_gas=100_000,
            eip1559=True,
            base_fee_wei=10 * 10**9,
            gas_price_wei=0,
            req=_req(),
        )
        assert out["gas"] == 150_000
        assert out["estimated_gas"] == 100_000

    def test_dapp_gas_raises_the_floor(self):
        """If the dapp requests more than estimate × 1.5, use the
        dapp value — they may know about an edge case we don't."""
        out = apply_gas_policy(
            estimated_gas=100_000,
            eip1559=True,
            base_fee_wei=10 * 10**9,
            gas_price_wei=0,
            req=_req(gas=500_000),
        )
        assert out["gas"] == 500_000

    def test_dapp_gas_below_floor_is_ignored(self):
        out = apply_gas_policy(
            estimated_gas=100_000,
            eip1559=True,
            base_fee_wei=10 * 10**9,
            gas_price_wei=0,
            req=_req(gas=50_000),
        )
        assert out["gas"] == 150_000   # our × 1.5 stays

    def test_max_fee_per_gas_is_base_times_two(self):
        out = apply_gas_policy(
            estimated_gas=21_000,
            eip1559=True,
            base_fee_wei=20 * 10**9,
            gas_price_wei=0,
            req=_req(),
        )
        assert out["max_fee_per_gas"] == 40 * 10**9
        assert out["base_fee"] == 20 * 10**9

    def test_priority_is_five_percent_of_base(self):
        out = apply_gas_policy(
            estimated_gas=21_000,
            eip1559=True,
            base_fee_wei=20 * 10**9,
            gas_price_wei=0,
            req=_req(),
        )
        assert out["max_priority_fee_per_gas"] == 10**9  # 20 gwei × 0.05

    def test_dapp_fee_values_raise_the_floor(self):
        out = apply_gas_policy(
            estimated_gas=21_000,
            eip1559=True,
            base_fee_wei=10 * 10**9,
            gas_price_wei=0,
            req=_req(
                max_fee_per_gas=100 * 10**9,
                max_priority_fee_per_gas=5 * 10**9,
            ),
        )
        # Dapp's 100 gwei > our 20; dapp's 5 gwei priority > our 0.5
        assert out["max_fee_per_gas"] == 100 * 10**9
        assert out["max_priority_fee_per_gas"] == 5 * 10**9

    def test_falls_back_to_gas_price_when_base_fee_missing(self):
        """eip1559 chain whose latest block has no baseFeePerGas
        (theoretical, but tolerated). gas_price reading takes over."""
        out = apply_gas_policy(
            estimated_gas=21_000,
            eip1559=True,
            base_fee_wei=0,
            gas_price_wei=8 * 10**9,
            req=_req(),
        )
        assert out["base_fee"] == 8 * 10**9
        assert out["max_fee_per_gas"] == 16 * 10**9


class TestFormatUsd:
    """Adaptive precision so layer-2 fees in the sub-cent range
    don't all read as 0.00."""

    def test_dollars_show_two_decimals(self):
        from decimal import Decimal
        from qeth.plugins.transactions import _format_usd
        assert _format_usd(Decimal("12.34")) == "12.34 USD"

    def test_cents_show_four_decimals(self):
        from decimal import Decimal
        from qeth.plugins.transactions import _format_usd
        assert _format_usd(Decimal("0.0153")) == "0.0153 USD"

    def test_sub_cent_shows_six_decimals(self):
        from decimal import Decimal
        from qeth.plugins.transactions import _format_usd
        assert _format_usd(Decimal("0.000123")) == "0.000123 USD"


class TestApplyGasPolicyLegacy:
    def test_gas_price_is_current_times_one_point_three_five(self):
        out = apply_gas_policy(
            estimated_gas=21_000,
            eip1559=False,
            base_fee_wei=0,
            gas_price_wei=10 * 10**9,
            req=_req(),
        )
        # 10 × 1.35 = 13.5 gwei
        assert out["gas_price"] == 135 * 10**8

    def test_dapp_gas_price_raises_the_floor(self):
        out = apply_gas_policy(
            estimated_gas=21_000,
            eip1559=False,
            base_fee_wei=0,
            gas_price_wei=10 * 10**9,
            req=_req(gas_price=100 * 10**9),
        )
        assert out["gas_price"] == 100 * 10**9

    def test_no_eip1559_keys_emitted(self):
        out = apply_gas_policy(
            estimated_gas=21_000,
            eip1559=False,
            base_fee_wei=0,
            gas_price_wei=10 * 10**9,
            req=_req(),
        )
        assert "max_fee_per_gas" not in out
        assert "max_priority_fee_per_gas" not in out


# --- SignerBridge round-trip --------------------------------------------

class TestSignerBridge:
    """The bridge's contract: caller awaits a future, slot resolves
    or rejects. Tested without aiohttp by driving the future directly
    inside the same event loop. The cross-thread queued-connection
    behaviour is Qt's job, not ours — what we cover here is "calling
    resolve wakes the awaiter with the value; reject wakes with the
    exception".
    """

    def test_resolve_returns_value(self, qtbot):
        bridge = SignerBridge()
        captured: list = []

        def on_request(req, fut):
            captured.append(req)
            bridge.resolve(fut, "0xdeadbeef")
        bridge.request_received.connect(on_request)

        async def go():
            return await bridge.submit_async(_req())

        result = asyncio.run(go())
        assert result == "0xdeadbeef"
        assert len(captured) == 1
        assert captured[0].from_addr == ADDR_CHECKSUM

    def test_reject_with_signer_error_raises_signer_error(self, qtbot):
        bridge = SignerBridge()

        def on_request(req, fut):
            bridge.reject(fut, SignerError("user cancelled"))
        bridge.request_received.connect(on_request)

        async def go():
            await bridge.submit_async(_req())

        with pytest.raises(SignerError, match="user cancelled"):
            asyncio.run(go())

    def test_reject_with_generic_error_is_wrapped_as_signer_error(self, qtbot):
        """Future-side rejections that aren't already SignerError
        get wrapped so RPC handlers see a single error type."""
        bridge = SignerBridge()

        def on_request(req, fut):
            bridge.reject(fut, RuntimeError("dongle locked"))
        bridge.request_received.connect(on_request)

        async def go():
            await bridge.submit_async(_req())

        with pytest.raises(SignerError, match="dongle locked"):
            asyncio.run(go())

    def test_resolve_after_done_is_noop(self, qtbot):
        """Calling resolve / reject on an already-resolved future
        should silently do nothing — defensive against double-emit
        from the UI side."""
        bridge = SignerBridge()
        fut: Future = Future()
        fut.set_result("0xaaa")
        bridge.resolve(fut, "0xbbb")
        bridge.reject(fut, SignerError("late"))
        assert fut.result() == "0xaaa"


# --- RpcServer dispatch surface -----------------------------------------

class TestRpcDispatchSendTransaction:
    """The eth_sendTransaction dispatch should route through the
    bridge when present, error cleanly when absent."""

    def _make_server(self, bridge=None):
        from unittest.mock import MagicMock
        from qeth.rpc import RpcServer
        from qeth.chains import DEFAULT_CHAINS
        store = MagicMock()
        store.current_chain.return_value = DEFAULT_CHAINS[0]
        return RpcServer(store, signer_bridge=bridge)

    def test_dispatch_without_bridge_returns_method_not_found(self):
        server = self._make_server(bridge=None)
        async def go():
            await server._dispatch("eth_sendTransaction", [{
                "from": ADDR_CHECKSUM, "to": TOKEN_CHECKSUM,
            }])
        from qeth.rpc import RpcError
        with pytest.raises(RpcError) as ei:
            asyncio.run(go())
        assert ei.value.code == -32601

    def test_dispatch_with_bridge_returns_hash(self, qtbot):
        bridge = SignerBridge()

        def on_request(req, fut):
            bridge.resolve(fut, "0xfeed")
        bridge.request_received.connect(on_request)

        server = self._make_server(bridge=bridge)

        async def go():
            return await server._dispatch("eth_sendTransaction", [{
                "from": ADDR_LOWER, "to": TOKEN_LOWER,
            }])

        assert asyncio.run(go()) == "0xfeed"

    def test_dispatch_user_cancel_surfaces_as_rpc_error(self, qtbot):
        bridge = SignerBridge()

        def on_request(req, fut):
            bridge.reject(fut, SignerError("User cancelled"))
        bridge.request_received.connect(on_request)

        server = self._make_server(bridge=bridge)

        async def go():
            await server._dispatch("eth_sendTransaction", [{
                "from": ADDR_LOWER, "to": TOKEN_LOWER,
            }])

        from qeth.rpc import RpcError
        with pytest.raises(RpcError) as ei:
            asyncio.run(go())
        assert ei.value.code == -32000
        assert "cancelled" in ei.value.message.lower()
