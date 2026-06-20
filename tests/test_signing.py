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

    def test_origin_is_propagated(self):
        """Origin (HTTP/WS header) flows through the parser onto
        the typed request so the signing dialog can show
        "Requested by: <site>". RPC handler is the only caller that
        passes a non-None value; local-send flows leave it None."""
        req = parse_send_transaction_params(
            [{"from": ADDR_CHECKSUM, "to": TOKEN_CHECKSUM}],
            CHAIN_ID,
            origin="https://app.uniswap.org",
        )
        assert req.origin == "https://app.uniswap.org"

    def test_origin_defaults_to_none(self):
        req = parse_send_transaction_params(
            [{"from": ADDR_CHECKSUM, "to": TOKEN_CHECKSUM}],
            CHAIN_ID,
        )
        assert req.origin is None


class TestRpcOriginExtraction:
    """The RPC handler picks the real dapp URL out of Frame's
    custom ``__frameOrigin`` body field; the HTTP/WS Origin
    header (which on extension-mediated calls is the extension's
    own) is only the fallback."""

    def _server_with_bridge(self):
        from unittest.mock import MagicMock
        from qeth.rpc import RpcServer
        from qeth.chains import DEFAULT_CHAINS
        bridge = SignerBridge()
        captured: list = []

        def on_request(req, fut):
            captured.append(req)
            bridge.resolve(fut, "0xfeed")
        bridge.request_received.connect(on_request)

        store = MagicMock()
        store.current_chain.return_value = DEFAULT_CHAINS[0]
        return RpcServer(store, signer_bridge=bridge), captured

    def test_frame_origin_overrides_http_origin(self, qtbot):
        server, captured = self._server_with_bridge()

        async def go():
            return await server._handle_one(
                {
                    "method": "eth_sendTransaction",
                    "params": [{"from": ADDR_LOWER, "to": TOKEN_LOWER}],
                    "__frameOrigin": "https://www.curve.finance",
                },
                origin="chrome-extension://abc",
            )

        result = asyncio.run(go())
        assert result["result"] == "0xfeed"
        assert captured[0].origin == "https://www.curve.finance"

    def test_falls_back_to_http_origin_when_no_frame_field(self, qtbot):
        server, captured = self._server_with_bridge()

        async def go():
            return await server._handle_one(
                {
                    "method": "eth_sendTransaction",
                    "params": [{"from": ADDR_LOWER, "to": TOKEN_LOWER}],
                },
                origin="https://app.uniswap.org",
            )

        asyncio.run(go())
        assert captured[0].origin == "https://app.uniswap.org"

    def test_web_origin_cannot_be_overridden_by_frame_spoof(self, qtbot):
        """A malicious page POSTs __frameOrigin to impersonate a trusted dapp
        while its true (browser-set, unforgeable) Origin is evil.example. The
        transport origin must win, so the dialog shows the real site (#2)."""
        server, captured = self._server_with_bridge()

        async def go():
            return await server._handle_one(
                {
                    "method": "eth_sendTransaction",
                    "params": [{"from": ADDR_LOWER, "to": TOKEN_LOWER}],
                    "__frameOrigin": "https://app.uniswap.org",   # the spoof
                },
                origin="https://evil.example",                   # the truth
            )

        asyncio.run(go())
        assert captured[0].origin == "https://evil.example"


class TestEffectiveOrigin:
    """_effective_origin: trust the unforgeable transport Origin over a body
    __frameOrigin claim, except where the transport isn't a web page (the
    Frame-extension case it exists for)."""

    def _eff(self, http, frame):
        from qeth.rpc import _effective_origin
        return _effective_origin(http, frame)

    def test_extension_transport_honours_frame_origin(self):
        # Frame: transport is the extension → trust the dapp it reports.
        assert self._eff("chrome-extension://abc",
                         "https://curve.finance") == "https://curve.finance"

    def test_absent_transport_honours_frame_origin(self):
        assert self._eff(None, "https://curve.finance") == "https://curve.finance"

    def test_conflicting_web_transport_wins(self):
        assert self._eff("https://evil.example",
                         "https://uniswap.org") == "https://evil.example"

    def test_no_frame_origin_uses_transport(self):
        assert self._eff("https://app.uniswap.org", None) == "https://app.uniswap.org"

    def test_matching_origins_are_stable(self):
        assert self._eff("https://x.org", "https://x.org") == "https://x.org"


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

    def test_gnosis_tiny_base_fee_floors_tip_at_one_wei(self):
        """Gnosis: a few-wei base fee makes 5% underflow to 0, and the node
        tip is 0 when the chain is idle — without a floor the tip is 0 and
        Nethermind rejects it ("FeeTooLow … 0 < 1"). Floor at 1 wei, and the
        effective tip min(priority, maxFee - baseFee) must clear 1."""
        out = apply_gas_policy(
            estimated_gas=21_000, eip1559=True,
            base_fee_wei=18, gas_price_wei=0, req=_req(),
            max_priority_fee_wei=0)
        prio = out["max_priority_fee_per_gas"]
        assert prio >= 1
        assert min(prio, out["max_fee_per_gas"] - 18) >= 1   # effective tip

    def test_gwei_decimals_widen_for_tiny_fees(self):
        """QDoubleSpinBox rounds its STORED value to `decimals`, so the
        gwei spinboxes must widen past the default 4 for sub-10⁵-wei
        values — otherwise the Gnosis tip from the test above quantizes
        back to a 0 the chain rejects, undoing the policy floor."""
        from qeth.plugins.transactions import _gwei_decimals
        assert _gwei_decimals(2 * 10**9) == 4        # 2 gwei
        assert _gwei_decimals(5 * 10**5) == 4        # 0.0005 gwei: exact at 4
        assert _gwei_decimals(30_528) == 9           # 5% of Gnosis base fee
        assert _gwei_decimals(1) == 9                # the policy floor itself
        assert _gwei_decimals(0) == 4

    def test_priority_floor_does_not_clobber_a_real_node_tip(self):
        """The 1-wei floor binds only when every other signal is 0; a real
        node tip (active chain) still wins."""
        out = apply_gas_policy(
            estimated_gas=21_000, eip1559=True,
            base_fee_wei=12, gas_price_wei=0, req=_req(),
            max_priority_fee_wei=60_059)
        assert out["max_priority_fee_per_gas"] == 60_059

    def test_dapp_max_fee_raises_the_floor(self):
        # Dapp's maxFeePerGas raises our suggestion (it's an upper-
        # bound safety buffer — keeping the ceiling low risks a
        # stuck tx if baseFee spikes).
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
        assert out["max_fee_per_gas"] == 100 * 10**9

    def test_dapp_priority_fee_is_ignored(self):
        # Priority is what the user actually pays — dapps in the
        # wild set it conservatively-high "just in case" (a tx
        # with dapp-priority=2 gwei on Ethereum when baseFee is
        # 0.1 gwei costs ~20× the chain cost). Always use 5 % of
        # baseFee regardless of dapp's request.
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
        # 5 % of 10 gwei, ignoring dapp's 5 gwei request.
        assert out["max_priority_fee_per_gas"] == (10 * 10**9 * 5) // 100

    def test_falls_back_to_gas_price_when_base_fee_missing(self):
        """EIP-1559 chain with baseFee=0 (BNB Smart Chain and
        friends): gas_price IS the network's required priority
        floor, not a reference value for a 5 % tip. Taking 5 %
        would land below BSC's accept threshold ("gas tip cap
        below minimum needed")."""
        out = apply_gas_policy(
            estimated_gas=21_000,
            eip1559=True,
            base_fee_wei=0,
            gas_price_wei=50_000_000,   # 0.05 gwei, BSC's current min
            req=_req(),
        )
        assert out["base_fee"] == 50_000_000
        # Priority equals gas_price, not 5 % of it.
        assert out["max_priority_fee_per_gas"] == 50_000_000
        assert out["max_fee_per_gas"] == 100_000_000

    def test_l2_tiny_base_fee_floors_tip_at_node_suggestion(self):
        """OP-stack L2 (Optimism/Base): baseFee is a few hundred wei, so
        baseFee × 0.05 underflows to a ~zero tip and the tx is
        underpriced. The node's eth_maxPriorityFeePerGas is the real
        floor; maxFee must cover base+tip (baseFee × 2 is a useless 578
        wei here)."""
        out = apply_gas_policy(
            estimated_gas=21_000,
            eip1559=True,
            base_fee_wei=289,           # Optimism, live
            gas_price_wei=0,
            req=_req(),
            max_priority_fee_wei=1_000_000,   # node tip ≈ 0.001 gwei
        )
        assert out["max_priority_fee_per_gas"] == 1_000_000  # not (289*5)//100 == 14
        assert out["max_fee_per_gas"] == 289 + 1_000_000
        assert out["max_fee_per_gas"] >= out["max_priority_fee_per_gas"]

    def test_node_tip_ignored_when_below_five_percent_of_base(self):
        """Ethereum: the node sometimes returns a nonsense tiny tip
        (single-digit wei); baseFee × 0.05 dominates and is used, so the
        floor doesn't disturb mainnet behaviour."""
        out = apply_gas_policy(
            estimated_gas=21_000,
            eip1559=True,
            base_fee_wei=20 * 10**9,
            gas_price_wei=0,
            req=_req(),
            max_priority_fee_wei=11,    # DRPC's bogus mainnet value
        )
        assert out["max_priority_fee_per_gas"] == 10**9   # 5 % of 20 gwei
        assert out["max_fee_per_gas"] == 40 * 10**9        # base × 2, unchanged

    def test_base_zero_priority_floored_at_node_tip(self):
        """baseFee == 0 and the node suggests a higher tip than gas_price
        → use the node tip (doubled for the ceiling)."""
        out = apply_gas_policy(
            estimated_gas=21_000,
            eip1559=True,
            base_fee_wei=0,
            gas_price_wei=50_000_000,
            req=_req(),
            max_priority_fee_wei=80_000_000,
        )
        assert out["max_priority_fee_per_gas"] == 80_000_000
        assert out["max_fee_per_gas"] == 160_000_000


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

class TestExplainRpcError:
    """The upstream JSON-RPC error message should be surfaced as a
    plain sentence rather than the dict repr the user originally
    saw (``{'message': 'Insufficient funds…', 'code': -32000}``)."""

    def test_extracts_message_from_dict_repr_string(self):
        from qeth.signing import explain_rpc_error
        # Mimic the exception text the user reported.
        e = RuntimeError(
            "{'message': 'Insufficient funds for gas * price + value: "
            "have 1000000000000000 want 1003083850000000', "
            "'code': -32000}"
        )
        msg = explain_rpc_error(e)
        assert msg.startswith("Insufficient funds")
        assert "{" not in msg  # the dict repr is gone

    def test_extracts_message_from_web3_rpc_error(self):
        from qeth.signing import explain_rpc_error
        from web3.exceptions import Web3RPCError
        e = Web3RPCError("rpc error")
        e.rpc_response = {
            "error": {"message": "nonce too low", "code": -32000},
        }
        assert explain_rpc_error(e) == "nonce too low"

    def test_falls_back_to_str_for_non_rpc_exception(self):
        from qeth.signing import explain_rpc_error
        assert explain_rpc_error(RuntimeError("plain error")) == "plain error"


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

class TestLedgerSignerLookup:
    """The path-lookup half of LedgerSigner is testable hermetically —
    the actual ``sign`` call hits a device and isn't covered here."""

    def _store(self, accounts):
        class _S:
            pass
        s = _S()
        s.accounts = accounts
        return s

    def test_can_sign_for_known_ledger_account(self):
        from qeth.ledger import LedgerSigner
        signer = LedgerSigner(self._store([
            {"address": ADDR_CHECKSUM, "source": "ledger",
             "path": "44'/60'/0'/0/0"},
        ]))
        assert signer.can_sign(ADDR_CHECKSUM)
        # case insensitive
        assert signer.can_sign(ADDR_LOWER)

    def test_can_sign_false_for_unknown_address(self):
        from qeth.ledger import LedgerSigner
        signer = LedgerSigner(self._store([
            {"address": ADDR_CHECKSUM, "source": "ledger",
             "path": "44'/60'/0'/0/0"},
        ]))
        assert not signer.can_sign("0x" + "ff" * 20)

    def test_can_sign_false_for_non_ledger_source(self):
        """Same address on a different source (hot wallet, watch-only)
        doesn't satisfy LedgerSigner."""
        from qeth.ledger import LedgerSigner
        signer = LedgerSigner(self._store([
            {"address": ADDR_CHECKSUM, "source": "hot",
             "path": "44'/60'/0'/0/0"},
        ]))
        assert not signer.can_sign(ADDR_CHECKSUM)

    def test_sign_without_path_raises(self):
        from qeth.ledger import LedgerSigner
        signer = LedgerSigner(self._store([
            {"address": ADDR_CHECKSUM, "source": "ledger"},  # no path
        ]))
        from qeth.chains import DEFAULT_CHAINS
        req = SigningRequest(
            chain_id=1, from_addr=ADDR_CHECKSUM, to_addr=TOKEN_CHECKSUM,
            gas=21000, nonce=0,
            max_fee_per_gas=10**9, max_priority_fee_per_gas=10**8,
        )
        with pytest.raises(SignerError, match="derivation path"):
            signer.sign(req, DEFAULT_CHAINS[0])

    def test_sign_refuses_when_connected_device_derives_different_address(
            self, monkeypatch):
        """Wrong Ledger plugged in: the path derives a different address,
        so signing must bail out instead of signing for the wrong account."""
        import ledgereth.accounts as _acc
        import ledgereth.comms as _comms
        from qeth.ledger import LedgerSigner
        from qeth.chains import DEFAULT_CHAINS
        monkeypatch.setattr(_comms, "init_dongle", lambda *a, **k: object())

        class _Acct:
            address = "0x" + "ab" * 20      # NOT ADDR_CHECKSUM

        monkeypatch.setattr(_acc, "get_account_by_path", lambda *a, **k: _Acct())
        signer = LedgerSigner(self._store([
            {"address": ADDR_CHECKSUM, "source": "ledger",
             "path": "44'/60'/0'/0/0"},
        ]))
        req = SigningRequest(
            chain_id=1, from_addr=ADDR_CHECKSUM, to_addr=TOKEN_CHECKSUM,
            gas=21000, nonce=0,
            max_fee_per_gas=10**9, max_priority_fee_per_gas=10**8,
        )
        with pytest.raises(SignerError, match="doesn't hold"):
            signer.sign(req, DEFAULT_CHAINS[0])

    def test_sign_proceeds_when_connected_device_matches(self, monkeypatch):
        """Correct device: the path derives the stored address, so the
        guard passes and we reach the real device sign."""
        import ledgereth.accounts as _acc
        import ledgereth.comms as _comms
        import ledgereth.transactions as _txs
        from qeth.ledger import LedgerSigner
        from qeth.chains import DEFAULT_CHAINS
        monkeypatch.setattr(_comms, "init_dongle", lambda *a, **k: object())

        class _Acct:
            address = ADDR_CHECKSUM

        class _Signed:
            rawTransaction = "0x" + "cd" * 4

        monkeypatch.setattr(_acc, "get_account_by_path", lambda *a, **k: _Acct())
        monkeypatch.setattr(_txs, "create_transaction", lambda **k: _Signed())
        signer = LedgerSigner(self._store([
            {"address": ADDR_CHECKSUM, "source": "ledger",
             "path": "44'/60'/0'/0/0"},
        ]))
        req = SigningRequest(
            chain_id=1, from_addr=ADDR_CHECKSUM, to_addr=TOKEN_CHECKSUM,
            gas=21000, nonce=0,
            max_fee_per_gas=10**9, max_priority_fee_per_gas=10**8,
        )
        assert signer.sign(req, DEFAULT_CHAINS[0]) == bytes.fromhex("cd" * 4)

    def test_sign_for_unknown_address_raises(self):
        from qeth.ledger import LedgerSigner
        from qeth.chains import DEFAULT_CHAINS
        signer = LedgerSigner(self._store([]))
        req = SigningRequest(
            chain_id=1, from_addr=ADDR_CHECKSUM, to_addr=TOKEN_CHECKSUM,
            gas=21000, nonce=0,
            max_fee_per_gas=10**9, max_priority_fee_per_gas=10**8,
        )
        with pytest.raises(SignerError, match="No Ledger account"):
            signer.sign(req, DEFAULT_CHAINS[0])

    def test_sign_without_gas_or_nonce_raises(self):
        from qeth.ledger import LedgerSigner
        from qeth.chains import DEFAULT_CHAINS
        signer = LedgerSigner(self._store([
            {"address": ADDR_CHECKSUM, "source": "ledger",
             "path": "44'/60'/0'/0/0"},
        ]))
        # Missing gas
        req = SigningRequest(
            chain_id=1, from_addr=ADDR_CHECKSUM, to_addr=TOKEN_CHECKSUM,
            nonce=0,
            max_fee_per_gas=10**9, max_priority_fee_per_gas=10**8,
        )
        with pytest.raises(SignerError, match="gas and nonce"):
            signer.sign(req, DEFAULT_CHAINS[0])

    def test_sign_eip1559_without_fees_raises(self):
        from qeth.ledger import LedgerSigner
        from qeth.chains import DEFAULT_CHAINS
        signer = LedgerSigner(self._store([
            {"address": ADDR_CHECKSUM, "source": "ledger",
             "path": "44'/60'/0'/0/0"},
        ]))
        req = SigningRequest(
            chain_id=1, from_addr=ADDR_CHECKSUM, to_addr=TOKEN_CHECKSUM,
            gas=21000, nonce=0,
            # no EIP-1559 fees
        )
        with pytest.raises(SignerError, match="EIP-1559 fees"):
            signer.sign(req, DEFAULT_CHAINS[0])


class TestExplainLedgerError:
    """The typed ledgereth exceptions get friendly action-oriented
    messages; anything that falls through carries the SW code so
    UNKNOWNs stay diagnosable."""

    def test_cancel(self):
        from qeth.ledger import _explain_ledger_error
        from ledgereth.exceptions import LedgerCancel
        msg = _explain_ledger_error(LedgerCancel("rejected"))
        assert "rejected on the ledger device" in msg.lower()

    def test_locked(self):
        from qeth.ledger import _explain_ledger_error
        from ledgereth.exceptions import LedgerLocked
        msg = _explain_ledger_error(LedgerLocked("locked"))
        assert "unlock" in msg.lower()

    def test_app_not_opened_covers_sleep(self):
        """LedgerAppNotOpened is the umbrella for APP_SLEEP /
        APP_NOT_STARTED / APP_NOT_FOUND — i.e. the screensaver
        dimmed, the user is on the dashboard, or Ethereum isn't
        installed."""
        from qeth.ledger import _explain_ledger_error
        from ledgereth.exceptions import LedgerAppNotOpened
        msg = _explain_ledger_error(LedgerAppNotOpened("sleep"))
        assert "ethereum app" in msg.lower()

    def test_not_found(self):
        from qeth.ledger import _explain_ledger_error
        from ledgereth.exceptions import LedgerNotFound
        msg = _explain_ledger_error(LedgerNotFound("nope"))
        assert "not detected" in msg.lower()

    def test_unknown_sw_includes_hex_code(self):
        """A CommException with a status word ledgereth doesn't have
        a typed exception for must surface the SW (so we can grow
        the mapping next time) rather than hiding behind 'UNKNOWN'."""
        from qeth.ledger import _explain_ledger_error
        from ledgereth.exceptions import CommException
        e = CommException("weird")
        e.sw = 0x6f42   # not in LedgerErrorCodes
        msg = _explain_ledger_error(e)
        assert "0x6f42" in msg
        assert "unknown" in msg.lower()

    def test_55xx_sw_means_device_asleep(self):
        """The 0x55xx range isn't in any public Ledger SW list, but
        the device emits it reproducibly when the screensaver turns
        off (USB still attached). User-confirmed mapping — show the
        sleep cause, not "UNKNOWN" or a generic hex code."""
        from qeth.ledger import _explain_ledger_error
        from ledgereth.exceptions import CommException
        e = CommException("sleep")
        e.sw = 0x5515
        msg = _explain_ledger_error(e)
        assert "asleep" in msg.lower()
        assert "wake" in msg.lower()

    def test_unknown_sw_extracted_from_cause_chain(self):
        """Regression: ledgereth's comms layer catches CommException
        and re-raises ``LedgerError("Unexpected error: 0x???? UNKNOWN")
        from err``, so by the time we see the exception it's a
        LedgerError and the CommException is on ``__cause__``. The
        decoder must walk the cause chain to recover the SW —
        otherwise the raw "Unexpected error: 0x5515 UNKNOWN" text
        leaks straight through."""
        from qeth.ledger import _explain_ledger_error
        from ledgereth.exceptions import CommException, LedgerError
        cause = CommException("sleep")
        cause.sw = 0x5515
        try:
            try:
                raise cause
            except CommException as err:
                raise LedgerError(
                    "Unexpected error: 0x5515 UNKNOWN"
                ) from err
        except LedgerError as e:
            msg = _explain_ledger_error(e)
        assert "asleep" in msg.lower()
        assert "UNKNOWN" not in msg, (
            "raw ledgereth text leaked through — __cause__ wasn't walked"
        )

    def test_known_sw_without_typed_exception_names_the_code(self):
        """Codes that ARE in LedgerErrorCodes but not in
        ERROR_CODE_EXCEPTIONS (e.g. INCORRECT_LENGTH) should at
        least name the code — much better than 'UNKNOWN'."""
        from qeth.ledger import _explain_ledger_error
        from ledgereth.exceptions import CommException
        e = CommException("size")
        e.sw = 0x6700   # INCORRECT_LENGTH
        msg = _explain_ledger_error(e)
        assert "INCORRECT_LENGTH" in msg or "0x6700" in msg


class TestIsLedgerAvailable:
    """The pre-flight probe used by signing + discovery to decide
    whether to open the "Connect your Ledger" prompt or proceed."""

    def test_returns_true_when_init_dongle_succeeds(self, monkeypatch):
        from qeth.ledger import is_ledger_available
        from ledgereth import comms as _comms

        class _FakeDongle:
            def close(self): pass

        fake = _FakeDongle()
        monkeypatch.setattr(_comms, "init_dongle", lambda: fake)
        # Cache should be cleared by the probe regardless of prior
        # state — load it to confirm.
        monkeypatch.setattr(_comms, "DONGLE_CACHE", fake)
        monkeypatch.setattr(_comms, "DONGLE_CONFIG_CACHE", object())

        ok, reason = is_ledger_available()
        assert ok is True
        assert reason is None
        # Probe must leave the cache empty so the next real call
        # opens a fresh handle.
        assert _comms.DONGLE_CACHE is None
        assert _comms.DONGLE_CONFIG_CACHE is None

    def test_returns_false_with_reason_when_init_dongle_raises(self, monkeypatch):
        from qeth.ledger import is_ledger_available
        from ledgereth import comms as _comms

        def boom():
            raise RuntimeError("device not connected")
        monkeypatch.setattr(_comms, "init_dongle", boom)

        ok, reason = is_ledger_available()
        assert ok is False
        assert reason is not None
        assert "device not connected" in reason


class TestLedgerDongleCache:
    """ledgereth caches its Dongle handle in a module-level slot.
    Reusing the cached handle across signs is unreliable — the USB
    session goes stale and the second sign silently fails before
    the device prompt. LedgerSigner.sign must close + clear the
    cache after every call, success or failure."""

    def _store(self):
        class _S:
            pass
        s = _S()
        s.accounts = [{
            "address": ADDR_CHECKSUM, "source": "ledger",
            "path": "44'/60'/0'/0/0",
        }]
        return s

    def _req(self):
        return SigningRequest(
            chain_id=1, from_addr=ADDR_CHECKSUM, to_addr=TOKEN_CHECKSUM,
            gas=21000, nonce=0,
            max_fee_per_gas=10**9, max_priority_fee_per_gas=10**8,
        )

    def _install_fake_dongle(self, monkeypatch):
        """Plant a fake dongle in ledgereth's cache and a stub
        create_transaction so the test never touches USB. Returns
        the fake dongle + a record dict the test can inspect."""
        from ledgereth import accounts as _accts
        from ledgereth import comms as _comms
        from ledgereth import transactions as _txns

        record = {"closed": False, "create_called": False}

        class _FakeDongle:
            def close(self):
                record["closed"] = True

        class _FakeSigned:
            rawTransaction = "0x02f8"

        class _FakeAcct:
            address = ADDR_CHECKSUM      # the device "holds" the stored addr

        def fake_create_transaction(**kwargs):
            record["create_called"] = True
            return _FakeSigned()

        fake = _FakeDongle()
        monkeypatch.setattr(_comms, "DONGLE_CACHE", fake)
        monkeypatch.setattr(_comms, "DONGLE_CONFIG_CACHE", object())
        # The verify-before-sign guard re-derives the path on the
        # connected device; make that return the stored address so the
        # guard passes and we exercise the real sign + cache cleanup.
        monkeypatch.setattr(_comms, "init_dongle", lambda *a, **k: fake)
        monkeypatch.setattr(_accts, "get_account_by_path", lambda *a, **k: _FakeAcct())
        monkeypatch.setattr(_txns, "create_transaction", fake_create_transaction)
        return fake, record

    def test_sign_closes_and_clears_cache_on_success(self, monkeypatch):
        from qeth.ledger import LedgerSigner
        from qeth.chains import DEFAULT_CHAINS
        from ledgereth import comms as _comms

        fake, record = self._install_fake_dongle(monkeypatch)
        signer = LedgerSigner(self._store())
        signer.sign(self._req(), DEFAULT_CHAINS[0])

        assert record["create_called"]
        assert record["closed"], (
            "previous dongle handle wasn't closed; next sign will "
            "fail on stale USB session"
        )
        assert _comms.DONGLE_CACHE is None
        assert _comms.DONGLE_CONFIG_CACHE is None

    def test_sign_closes_and_clears_cache_on_failure(self, monkeypatch):
        """Same invariant must hold when create_transaction raises
        (user rejects on device, USB comm error, etc.). Otherwise
        the next sign attempt inherits a dead handle."""
        from qeth.ledger import LedgerSigner
        from qeth.chains import DEFAULT_CHAINS
        from ledgereth import comms as _comms, transactions as _txns

        fake, record = self._install_fake_dongle(monkeypatch)

        def boom(**kwargs):
            raise RuntimeError("user rejected on device")
        monkeypatch.setattr(_txns, "create_transaction", boom)

        signer = LedgerSigner(self._store())
        with pytest.raises(SignerError):
            signer.sign(self._req(), DEFAULT_CHAINS[0])

        assert record["closed"]
        assert _comms.DONGLE_CACHE is None
        assert _comms.DONGLE_CONFIG_CACHE is None


class TestSignAndBroadcastWorker:
    """The worker's failure-path branches are testable by passing a
    mock signer / monkeypatching EthClient. Successful run hits a
    real chain and isn't covered here."""

    def _req(self):
        return SigningRequest(
            chain_id=1, from_addr=ADDR_CHECKSUM, to_addr=TOKEN_CHECKSUM,
            gas=21000, nonce=0,
            max_fee_per_gas=10**9, max_priority_fee_per_gas=10**8,
        )

    def test_signer_failure_emits_failed_not_broadcast(self, qtbot, monkeypatch):
        """Worker.run runs synchronously when invoked directly, so we
        can call it without spinning a thread up — keeps the test
        deterministic."""
        from qeth.signing import SignAndBroadcastWorker
        from qeth.chains import DEFAULT_CHAINS

        class _BadSigner:
            def can_sign(self, addr): return True
            def sign(self, req, chain):
                raise SignerError("user rejected on device")

        worker = SignAndBroadcastWorker(_BadSigner(), self._req(), DEFAULT_CHAINS[0])
        captured: dict = {}
        worker.broadcast.connect(lambda h: captured.setdefault("hash", h))
        worker.failed.connect(lambda msg: captured.setdefault("msg", msg))
        worker.run()    # synchronous; no thread / event loop needed
        assert "hash" not in captured
        assert "user rejected" in captured["msg"]

    def test_node_rejection_surfaces_as_failure(self, qtbot, monkeypatch):
        """Signer succeeds but the node REJECTS the tx (it answered with
        an RPC error — those bytes can never land). The worker reports
        broadcast failure so the dialog stays open for a re-price."""
        from web3.exceptions import Web3RPCError
        from qeth.signing import SignAndBroadcastWorker
        from qeth.chains import DEFAULT_CHAINS

        class _OkSigner:
            def can_sign(self, addr): return True
            def sign(self, req, chain): return b"\x01" * 64

        class _RejectingClient:
            def __init__(self, chain): pass
            def send_raw_transaction(self, raw):
                raise Web3RPCError("{'code': -32000, 'message': 'nonce too low'}")

        # The worker imports EthClient lazily from qeth.chain inside
        # run(); patch the source-of-truth.
        import qeth.chain
        monkeypatch.setattr(qeth.chain, "EthClient", _RejectingClient)

        worker = SignAndBroadcastWorker(_OkSigner(), self._req(), DEFAULT_CHAINS[0])
        captured: dict = {}
        worker.broadcast.connect(lambda h: captured.setdefault("hash", h))
        worker.failed.connect(lambda msg: captured.setdefault("msg", msg))
        worker.run()
        assert "hash" not in captured
        assert "Broadcast failed" in captured["msg"]
        assert "nonce too low" in captured["msg"]

    def test_transport_failure_still_emits_broadcast(self, qtbot, monkeypatch):
        """The push died at the transport level (timeout / connection drop):
        the node may or may not hold the tx, so the signed bytes must NOT be
        dropped on the floor. The worker computes the hash locally (keccak of
        the signed bytes) and emits broadcast with first_push_ok=False — the
        pending row gets recorded and PendingTxWatcher re-broadcasts it,
        regardless of which account is selected afterwards."""
        from eth_utils import keccak
        from qeth.signing import SignAndBroadcastWorker
        from qeth.chains import DEFAULT_CHAINS

        signed = b"\x02\xf8\x6f\x01\x02\x03"

        class _OkSigner:
            def can_sign(self, addr): return True
            def sign(self, req, chain): return signed

        class _DeadClient:
            def __init__(self, chain): pass
            def send_raw_transaction(self, raw):
                raise ConnectionError("connection reset by peer")

        import qeth.chain
        monkeypatch.setattr(qeth.chain, "EthClient", _DeadClient)

        worker = SignAndBroadcastWorker(_OkSigner(), self._req(), DEFAULT_CHAINS[0])
        captured: dict = {}
        worker.broadcast.connect(
            lambda h, raw, ok: captured.update(hash=h, raw=raw, ok=ok))
        worker.failed.connect(lambda msg: captured.setdefault("msg", msg))
        worker.run()
        assert "msg" not in captured
        assert captured["hash"] == "0x" + keccak(signed).hex()
        assert captured["raw"] == "0x" + signed.hex()
        assert captured["ok"] is False

    def test_happy_path_emits_broadcast_with_hash(self, qtbot, monkeypatch):
        from qeth.signing import SignAndBroadcastWorker
        from qeth.chains import DEFAULT_CHAINS

        class _OkSigner:
            def can_sign(self, addr): return True
            def sign(self, req, chain): return b"\xab\xcd"

        class _OkClient:
            def __init__(self, chain): pass
            def send_raw_transaction(self, raw):
                return "0xfeed1234"

        import qeth.chain
        monkeypatch.setattr(qeth.chain, "EthClient", _OkClient)

        worker = SignAndBroadcastWorker(_OkSigner(), self._req(), DEFAULT_CHAINS[0])
        captured: dict = {}
        worker.broadcast.connect(
            lambda h, raw, ok: captured.update(hash=h, ok=ok))
        worker.failed.connect(lambda msg: captured.setdefault("msg", msg))
        worker.run()
        assert captured.get("hash") == "0xfeed1234"
        assert captured.get("ok") is True
        assert "msg" not in captured


class TestErc20TransferCalldata:
    """Pure ABI encoding for the Send-token flow."""

    def test_selector_and_args(self):
        from qeth.plugins.transactions import _erc20_transfer_calldata
        recipient = "0xB325c1AC788f02fF7997cF53C6FF40Dd762897B3"
        amount = 5000 * 10**18
        calldata = _erc20_transfer_calldata(recipient, amount)
        # Selector for transfer(address,uint256)
        assert calldata.startswith("0xa9059cbb")
        # Address right-padded into 32 bytes, then amount as 32-byte
        # big-endian.
        assert calldata[10:74].lower() == "0" * 24 + recipient[2:].lower()
        assert int(calldata[74:138], 16) == amount

    def test_zero_amount(self):
        from qeth.plugins.transactions import _erc20_transfer_calldata
        calldata = _erc20_transfer_calldata(
            "0x" + "ab" * 20, 0,
        )
        assert calldata.startswith("0xa9059cbb")
        # Amount last 32 bytes = all zeros.
        assert int(calldata[74:138], 16) == 0


class TestConfirmedFromReceipt:
    """The pure function that merges an eth_getTransactionReceipt
    payload into a pending Transaction → confirmed Transaction."""

    def _pending(self):
        from qeth.transactions import Transaction
        return Transaction(
            chain_id=1, hash="0x" + "ab" * 32, block_number=0, timestamp=1700,
            nonce=5, from_addr=ADDR_LOWER, to_addr=TOKEN_LOWER,
            value_wei=0, gas_used=0, gas_price_wei=2 * 10**9,
            method_id="0xa9059cbb", input_data="0xa9059cbb",
            success=True, pending=True,
        )

    def test_success_receipt(self):
        from qeth.plugins.transactions import _confirmed_from_receipt
        out = _confirmed_from_receipt(self._pending(), {
            "blockNumber": "0x1234",
            "gasUsed": "0xc350",
            "status": "0x1",
            "effectiveGasPrice": "0x77359400",   # 2 gwei
        })
        assert out.pending is False
        assert out.success is True
        assert out.block_number == 0x1234
        assert out.gas_used == 0xc350
        assert out.gas_price_wei == 2 * 10**9
        # Other fields preserved
        assert out.nonce == 5
        assert out.hash == self._pending().hash

    def test_revert_receipt(self):
        from qeth.plugins.transactions import _confirmed_from_receipt
        out = _confirmed_from_receipt(self._pending(), {
            "blockNumber": "0x1234",
            "gasUsed": "0xc350",
            "status": "0x0",
            "effectiveGasPrice": "0x77359400",
        })
        assert out.pending is False
        assert out.success is False

    def test_missing_status_defaults_to_success(self):
        """Pre-Byzantium receipts (pre-block 4_370_000 on Ethereum)
        had no ``status`` field. Treat absence as success since the
        tx was mined — Blockscout has the same convention."""
        from qeth.plugins.transactions import _confirmed_from_receipt
        out = _confirmed_from_receipt(self._pending(), {
            "blockNumber": "0x1234",
            "gasUsed": "0xc350",
            "effectiveGasPrice": "0x77359400",
        })
        assert out.success is True
        assert out.pending is False

    def test_missing_effective_gas_price_keeps_old_value(self):
        """Some upstream providers omit effectiveGasPrice on legacy
        txs. Fall back to the cached value the user signed with so
        the row isn't suddenly showing 0 wei."""
        from qeth.plugins.transactions import _confirmed_from_receipt
        out = _confirmed_from_receipt(self._pending(), {
            "blockNumber": "0x1234",
            "gasUsed": "0xc350",
            "status": "0x1",
        })
        assert out.gas_price_wei == 2 * 10**9


class TestAddPending:
    """The plugin's add_pending injects a Transaction(pending=True)
    into cache + disk, prepended to the (chain, from_addr) bucket."""

    def _plugin(self, tmp_qeth):
        from qeth.plugins.transactions import TransactionsPlugin
        return TransactionsPlugin()

    def _req(self):
        return SigningRequest(
            chain_id=1, from_addr=ADDR_CHECKSUM, to_addr=TOKEN_CHECKSUM,
            value_wei=0, data="0xa9059cbb" + "00" * 64,
            gas=100_000, nonce=7,
            max_fee_per_gas=2 * 10**9, max_priority_fee_per_gas=10**8,
        )

    def test_pending_entry_appears_in_cache(self, tmp_qeth):
        from qeth.chains import DEFAULT_CHAINS
        plugin = self._plugin(tmp_qeth)
        plugin.add_pending("0xfeed", self._req(), DEFAULT_CHAINS[0])
        key = (1, ADDR_LOWER)
        assert key in plugin._cache
        assert len(plugin._cache[key]) == 1
        tx = plugin._cache[key][0]
        assert tx.pending is True
        assert tx.hash == "0xfeed"
        assert tx.nonce == 7
        assert tx.from_addr == ADDR_LOWER
        assert tx.to_addr == TOKEN_LOWER
        assert tx.block_number == 0
        # method_id is the first 10 chars of data (selector)
        assert tx.method_id == "0xa9059cbb"

    def test_pending_stores_raw_signed_for_rebroadcast(self, tmp_qeth):
        from qeth.chains import DEFAULT_CHAINS
        plugin = self._plugin(tmp_qeth)
        plugin.add_pending(
            "0xfeed", self._req(), DEFAULT_CHAINS[0], raw_signed="0xRAW",
        )
        tx = plugin._cache[(1, ADDR_LOWER)][0]
        assert tx.raw_signed == "0xRAW"   # kept so the watcher can re-push

    def test_pending_eip1559_gas_price_uses_max_fee(self, tmp_qeth):
        """Until the receipt lands we don't know the effective rate
        yet, so the displayed gas_price_wei is the ceiling the user
        signed with (max fee). Receipt arrival overwrites it with
        effectiveGasPrice."""
        from qeth.chains import DEFAULT_CHAINS
        plugin = self._plugin(tmp_qeth)
        plugin.add_pending("0xfeed", self._req(), DEFAULT_CHAINS[0])
        tx = plugin._cache[(1, ADDR_LOWER)][0]
        assert tx.gas_price_wei == 2 * 10**9

    def test_pending_persists_to_disk(self, tmp_qeth):
        """Disk-cache round-trip — pending entries must survive
        restart so the watcher picks them up on next launch."""
        from qeth.chains import DEFAULT_CHAINS
        from qeth.transactions_cache import TransactionCache
        plugin = self._plugin(tmp_qeth)
        plugin.add_pending("0xfeed", self._req(), DEFAULT_CHAINS[0])
        disk = TransactionCache().load(1, ADDR_LOWER)
        assert disk is not None
        assert len(disk) == 1
        assert disk[0].pending is True
        assert disk[0].hash == "0xfeed"

    def test_pending_prepended_to_existing_cache(self, tmp_qeth):
        """When the user already has historical txs cached, the
        pending row should land on top (it has the highest nonce
        per the user's just-signed broadcast)."""
        from qeth.chains import DEFAULT_CHAINS
        from qeth.transactions import Transaction
        plugin = self._plugin(tmp_qeth)
        key = (1, ADDR_LOWER)
        plugin._cache[key] = [
            Transaction(
                chain_id=1, hash="0x" + "11" * 32, block_number=100,
                timestamp=1000, nonce=6, from_addr=ADDR_LOWER,
                to_addr=TOKEN_LOWER, value_wei=0, gas_used=21000,
                gas_price_wei=10**9, method_id="", input_data="0x",
                success=True,
            ),
        ]
        plugin.add_pending("0xfeed", self._req(), DEFAULT_CHAINS[0])
        # merge_txs sorts by nonce desc; pending has nonce 7 > 6
        # so it ends up first.
        assert plugin._cache[key][0].hash == "0xfeed"
        assert plugin._cache[key][0].pending is True


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


class TestRpcDispatchAddChain:
    """wallet_addEthereumChain for an UNKNOWN chain must ask the user
    (via the bridge) before persisting the site-supplied RPC. A chain
    we already know is a silent no-op (we keep our own RPC); with no
    bridge wired (headless / tests) the old silent-add is preserved."""

    def _make_server(self, bridge=None, known=None):
        from unittest.mock import MagicMock
        from qeth.rpc import RpcServer
        from qeth.chains import DEFAULT_CHAINS
        store = MagicMock()
        chains = list(known if known is not None else [])
        store.chains = chains
        store.current_chain.return_value = DEFAULT_CHAINS[0]

        def _add(chain):   # mirror store.add_chain's id-dedupe
            if not any(c.chain_id == chain.chain_id for c in chains):
                chains.append(chain)
        store.add_chain.side_effect = _add
        return RpcServer(store, signer_bridge=bridge), chains

    def _add_params(self):
        return [{
            "chainId": "0x5ca1ab1e",
            "chainName": "Sketchy L2",
            "rpcUrls": ["https://rpc.sketchy.example"],
            "nativeCurrency": {"symbol": "SKT"},
            "blockExplorerUrls": ["https://scan.sketchy.example"],
        }]

    def test_unknown_chain_added_when_user_approves(self, qtbot):
        bridge = SignerBridge()
        seen: list = []

        def on_add(info, fut):
            seen.append(info)
            bridge.resolve_chain(fut, True)
        bridge.chain_add_requested.connect(on_add)
        server, chains = self._make_server(bridge=bridge)

        async def go():
            return await server._dispatch(
                "wallet_addEthereumChain", self._add_params(),
                origin="https://dapp.example")

        assert asyncio.run(go()) is None
        assert len(chains) == 1
        assert chains[0].chain_id == 0x5ca1ab1e
        assert chains[0].rpc_url == "https://rpc.sketchy.example"
        assert chains[0].symbol == "SKT"
        # The user was actually asked, with the site origin + the URL
        # it's being asked to trust.
        assert seen[0]["origin"] == "https://dapp.example"
        assert seen[0]["rpc_url"] == "https://rpc.sketchy.example"

    def test_unknown_chain_rejected_returns_4001_and_not_added(self, qtbot):
        from qeth.rpc import RpcError
        bridge = SignerBridge()
        bridge.chain_add_requested.connect(
            lambda info, fut: bridge.resolve_chain(fut, False))
        server, chains = self._make_server(bridge=bridge)

        async def go():
            await server._dispatch(
                "wallet_addEthereumChain", self._add_params())

        with pytest.raises(RpcError) as ei:
            asyncio.run(go())
        assert ei.value.code == 4001
        assert chains == []

    def test_non_network_rpc_url_is_rejected_before_prompt(self, qtbot):
        """A file:// (or other non-network) RPC URL is structurally rejected
        with -32602 and the user is never even prompted (issue #1)."""
        from qeth.rpc import RpcError
        bridge = SignerBridge()
        asked: list = []
        bridge.chain_add_requested.connect(lambda i, f: asked.append(i))
        server, chains = self._make_server(bridge=bridge)
        params = self._add_params()
        params[0]["rpcUrls"] = ["file:///etc/passwd"]

        async def go():
            await server._dispatch("wallet_addEthereumChain", params)

        with pytest.raises(RpcError) as ei:
            asyncio.run(go())
        assert ei.value.code == -32602
        assert chains == [] and asked == []   # not persisted, not prompted

    def test_bad_explorer_scheme_is_rejected(self, qtbot):
        from qeth.rpc import RpcError
        bridge = SignerBridge()
        bridge.chain_add_requested.connect(
            lambda i, f: bridge.resolve_chain(f, True))
        server, chains = self._make_server(bridge=bridge)
        params = self._add_params()
        params[0]["blockExplorerUrls"] = ["javascript:alert(1)"]

        async def go():
            await server._dispatch("wallet_addEthereumChain", params)

        with pytest.raises(RpcError) as ei:
            asyncio.run(go())
        assert ei.value.code == -32602
        assert chains == []

    def test_known_chain_is_silent_noop_without_prompt(self, qtbot):
        from qeth.chains import DEFAULT_CHAINS
        bridge = SignerBridge()
        asked: list = []
        bridge.chain_add_requested.connect(lambda i, f: asked.append(i))
        server, chains = self._make_server(
            bridge=bridge, known=[DEFAULT_CHAINS[0]])

        async def go():
            # Re-add Ethereum (chain id 1) with a hostile RPC.
            return await server._dispatch(
                "wallet_addEthereumChain",
                [{"chainId": "0x1", "rpcUrls": ["https://evil.example"]}])

        assert asyncio.run(go()) is None
        assert asked == []                       # never prompted
        assert len(chains) == 1                  # no second entry
        assert chains[0].rpc_url == DEFAULT_CHAINS[0].rpc_url  # our RPC kept

    def test_concurrent_adds_for_same_chain_share_one_prompt(self, qtbot):
        """A dapp re-firing wallet_addEthereumChain while the first
        prompt is still open must NOT stack a second modal — both
        requests share one prompt and one decision."""
        bridge = SignerBridge()
        prompts: list = []
        futures: list = []

        def on_add(info, fut):     # record, defer the decision
            prompts.append(info)
            futures.append(fut)
        bridge.chain_add_requested.connect(on_add)
        server, chains = self._make_server(bridge=bridge)

        async def go():
            async def one():
                return await server._dispatch(
                    "wallet_addEthereumChain", self._add_params())
            t1 = asyncio.create_task(one())
            t2 = asyncio.create_task(one())
            for _ in range(4):     # let both reach their await
                await asyncio.sleep(0)
            assert len(prompts) == 1   # one prompt despite two requests
            bridge.resolve_chain(futures[0], True)
            return await asyncio.gather(t1, t2)

        r1, r2 = asyncio.run(go())
        assert r1 is None and r2 is None
        assert len(prompts) == 1
        assert len(chains) == 1     # added exactly once

    def test_no_bridge_silently_adds(self):
        # Headless / tests: no user to ask, so the old behaviour stands.
        server, chains = self._make_server(bridge=None)

        async def go():
            return await server._dispatch(
                "wallet_addEthereumChain", self._add_params())

        assert asyncio.run(go()) is None
        assert len(chains) == 1
        assert chains[0].chain_id == 0x5ca1ab1e


class TestRpcProxyFailFast:
    """_proxy's per-host fail-fast cooldown. When an upstream RPC times
    out or drops the connection, the host is marked and subsequent
    requests short-circuit for _FAIL_FAST_S instead of each grinding
    through another ~15 s timeout — the behaviour that kept the app
    responsive when DRPC's DNS went out. A successful response clears
    the mark so the next request flows normally."""

    def _make_server(self):
        from unittest.mock import MagicMock
        from qeth.rpc import RpcServer
        from qeth.chains import DEFAULT_CHAINS
        store = MagicMock()
        store.chains = list(DEFAULT_CHAINS)
        # origin=None routes to current_chain (no per-origin override).
        store.current_chain.return_value = DEFAULT_CHAINS[0]
        return RpcServer(store), DEFAULT_CHAINS[0].rpc_url

    def _client(self, mode: str, result=None):
        """Fake aiohttp session. mode='fail' drops the connection on
        entry; mode='ok' returns ``result``. Counts post() calls so a
        test can prove the wire was never touched."""
        import json
        from aiohttp import ServerDisconnectedError
        payload = result or {"jsonrpc": "2.0", "id": 1, "result": "0x10"}

        class _Resp:
            status = 200

            async def text(self_inner):
                return json.dumps(payload)

        class _CM:
            async def __aenter__(self_inner):
                if mode == "fail":
                    raise ServerDisconnectedError()
                return _Resp()

            async def __aexit__(self_inner, *_a):
                return False

        class _Client:
            def __init__(self_inner):
                self_inner.calls = 0

            def post(self_inner, *_a, **_k):
                self_inner.calls += 1
                return _CM()

        return _Client()

    def test_transient_failure_marks_the_host(self):
        from aiohttp import ServerDisconnectedError
        server, host = self._make_server()
        server._client = self._client("fail")

        async def go():
            with pytest.raises(ServerDisconnectedError):
                await server._proxy("eth_blockNumber", [])
            return host in server._host_last_fail

        assert asyncio.run(go()) is True

    def test_within_cooldown_short_circuits_without_calling_upstream(self):
        # Short-circuit only when EVERY provider (primary + fallbacks) is on
        # cooldown — otherwise the proxy fails over to a fresh one.
        from qeth.rpc import RpcError
        from qeth.chains import DEFAULT_CHAINS
        server, host = self._make_server()
        client = self._client("ok")  # would succeed if any host were tried
        server._client = client

        async def go():
            loop = asyncio.get_event_loop()
            for url in [DEFAULT_CHAINS[0].rpc_url, *DEFAULT_CHAINS[0].fallback_rpcs]:
                server._host_last_fail[url] = loop.time()  # all failed just now
            with pytest.raises(RpcError) as ei:
                await server._proxy("eth_blockNumber", [])
            return ei.value.code, client.calls

        code, calls = asyncio.run(go())
        assert code == -32603
        assert calls == 0  # never hit the wire — every provider on cooldown

    def test_success_after_cooldown_clears_the_mark(self):
        server, host = self._make_server()
        client = self._client("ok")
        server._client = client

        async def go():
            loop = asyncio.get_event_loop()
            # A failure older than the cooldown window: the next request
            # should re-probe rather than short-circuit.
            server._host_last_fail[host] = loop.time() - server._FAIL_FAST_S - 1
            result = await server._proxy("eth_blockNumber", [])
            return result, host in server._host_last_fail, client.calls

        result, still_marked, calls = asyncio.run(go())
        assert result == "0x10"
        assert still_marked is False  # cleared on success
        assert calls == 1


class TestParsePersonalSignParams:
    """``personal_sign`` is the message-first cousin of ``eth_sign``
    (which is address-first). Dapps confuse the two; the parser
    sniffs which arg is the address rather than trusting order."""

    def test_message_first_address_second(self):
        from qeth.signing import parse_personal_sign_params
        req = parse_personal_sign_params([
            "0x" + b"hello".hex(),
            ADDR_CHECKSUM,
        ])
        assert req.from_addr == ADDR_CHECKSUM
        assert req.raw == b"hello"

    def test_address_first_message_second(self):
        from qeth.signing import parse_personal_sign_params
        req = parse_personal_sign_params([
            ADDR_CHECKSUM,
            "0x" + b"hello".hex(),
        ])
        assert req.from_addr == ADDR_CHECKSUM
        assert req.raw == b"hello"

    def test_plain_utf8_message_no_hex_prefix(self):
        from qeth.signing import parse_personal_sign_params
        req = parse_personal_sign_params([
            "Welcome to qeth!", ADDR_CHECKSUM,
        ])
        assert req.raw == b"Welcome to qeth!"

    def test_missing_args_raises(self):
        from qeth.signing import parse_personal_sign_params, SignerError
        with pytest.raises(SignerError):
            parse_personal_sign_params([ADDR_CHECKSUM])


class TestParseTypedDataParams:
    """``eth_signTypedData_v4`` accepts either a JSON-string
    second arg or an already-parsed object. Both must work."""

    def _typed(self) -> dict:
        return {
            "domain": {"name": "x", "version": "1", "chainId": 1,
                       "verifyingContract": "0x" + "00" * 20},
            "types": {"EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ]},
            "primaryType": "EIP712Domain",
            "message": {},
        }

    def test_accepts_dict(self):
        from qeth.signing import parse_typed_data_params
        req = parse_typed_data_params([ADDR_CHECKSUM, self._typed()])
        assert req.from_addr == ADDR_CHECKSUM
        assert req.typed_data["primaryType"] == "EIP712Domain"

    def test_accepts_json_string(self):
        import json as _json
        from qeth.signing import parse_typed_data_params
        req = parse_typed_data_params(
            [ADDR_CHECKSUM, _json.dumps(self._typed())],
        )
        assert req.typed_data["domain"]["name"] == "x"

    def test_bad_json_raises(self):
        from qeth.signing import parse_typed_data_params, SignerError
        with pytest.raises(SignerError):
            parse_typed_data_params([ADDR_CHECKSUM, "not-json"])


class TestRpcDispatchMessageSigning:
    """eth_sign is refused; personal_sign and eth_signTypedData_v*
    flow through the bridge."""

    def _make_server(self, bridge=None):
        from unittest.mock import MagicMock
        from qeth.rpc import RpcServer
        from qeth.chains import DEFAULT_CHAINS
        store = MagicMock()
        store.current_chain.return_value = DEFAULT_CHAINS[0]
        store.chains = DEFAULT_CHAINS
        return RpcServer(store, signer_bridge=bridge)

    def test_eth_sign_refused(self):
        from qeth.rpc import RpcError
        server = self._make_server()

        async def go():
            await server._dispatch("eth_sign", [ADDR_CHECKSUM, "0x00"])

        with pytest.raises(RpcError) as ei:
            asyncio.run(go())
        assert ei.value.code == -32601
        assert "unsafe" in ei.value.message.lower()

    def test_personal_sign_routes_through_bridge(self, qtbot):
        from qeth.signing import (
            MessageSigningRequest, SignerBridge,
        )
        bridge = SignerBridge()
        captured: list = []

        def on_request(req, fut):
            captured.append(req)
            bridge.resolve(fut, "0x" + "ab" * 65)
        bridge.request_received.connect(on_request)

        server = self._make_server(bridge=bridge)

        async def go():
            return await server._dispatch(
                "personal_sign",
                ["0x" + b"hi".hex(), ADDR_CHECKSUM],
            )

        result = asyncio.run(go())
        assert result == "0x" + "ab" * 65
        assert len(captured) == 1
        assert isinstance(captured[0], MessageSigningRequest)
        assert captured[0].raw == b"hi"

    def test_signTypedData_v4_routes_through_bridge(self, qtbot):
        from qeth.signing import (
            SignerBridge, TypedDataSigningRequest,
        )
        bridge = SignerBridge()
        captured: list = []

        def on_request(req, fut):
            captured.append(req)
            bridge.resolve(fut, "0x" + "cd" * 65)
        bridge.request_received.connect(on_request)

        server = self._make_server(bridge=bridge)
        typed = {
            "domain": {"name": "x", "version": "1", "chainId": 1,
                       "verifyingContract": "0x" + "00" * 20},
            "types": {"EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ]},
            "primaryType": "EIP712Domain",
            "message": {},
        }

        async def go():
            return await server._dispatch(
                "eth_signTypedData_v4", [ADDR_CHECKSUM, typed],
            )

        result = asyncio.run(go())
        assert result == "0x" + "cd" * 65
        assert isinstance(captured[0], TypedDataSigningRequest)


class TestRpcEventBroadcast:
    """EIP-1193 push events to connected WS clients —
    ``accountsChanged`` when the user picks a new default account,
    ``chainChanged`` when the chain switches (UI- OR dapp-driven).

    Frame's wire format: dapps first ``eth_subscribe`` for the
    event type and get back a subscription id; the wallet then
    sends ``eth_subscription`` notifications with that id when the
    event fires. The browser extension translates these into the
    standard EIP-1193 events on window.ethereum."""

    def _make_server(self):
        from unittest.mock import MagicMock
        from qeth.rpc import RpcServer
        from qeth.chains import DEFAULT_CHAINS
        store = MagicMock()
        store.current_chain.return_value = DEFAULT_CHAINS[0]
        store.chains = DEFAULT_CHAINS
        return RpcServer(store)

    def test_eth_subscribe_for_wallet_event_returns_sub_id(self):
        from unittest.mock import MagicMock
        server = self._make_server()
        ws = MagicMock(closed=False)
        async def go():
            return await server._dispatch(
                "eth_subscribe", ["accountsChanged"], ws=ws,
            )
        sub_id = asyncio.run(go())
        assert isinstance(sub_id, str)
        assert sub_id.startswith("0x")
        # Mapped under (ws, sub_type).
        assert server._ws_subscriptions[ws]["accountsChanged"] == sub_id

    def test_eth_subscribe_without_ws_context_raises(self):
        # HTTP-only callers can't be pushed to; refuse the subscribe.
        from qeth.rpc import RpcError
        server = self._make_server()
        async def go():
            await server._dispatch(
                "eth_subscribe", ["accountsChanged"], ws=None,
            )
        with pytest.raises(RpcError):
            asyncio.run(go())

    def test_broadcast_event_targets_subscribers_only(self):
        # ws_a subscribed; ws_b didn't. Only ws_a gets a push.
        from unittest.mock import AsyncMock, MagicMock
        import json as _json
        server = self._make_server()
        ws_a = MagicMock(closed=False, send_str=AsyncMock())
        ws_b = MagicMock(closed=False, send_str=AsyncMock())
        server._ws_clients = {ws_a, ws_b}
        sub_id = server._register_subscription(ws_a, "accountsChanged")

        asyncio.run(server._broadcast_event(
            "accountsChanged", ["0x7a16ff"],
        ))
        ws_a.send_str.assert_awaited_once()
        ws_b.send_str.assert_not_awaited()
        sent = _json.loads(ws_a.send_str.call_args.args[0])
        assert sent == {
            "jsonrpc": "2.0",
            "method": "eth_subscription",
            "params": {
                "subscription": sub_id,
                "result": ["0x7a16ff"],
            },
        }

    def test_broadcast_event_prunes_closed_and_flaky_clients(self):
        from unittest.mock import AsyncMock, MagicMock
        server = self._make_server()
        flaky = MagicMock(closed=False, send_str=AsyncMock(
            side_effect=ConnectionResetError("peer gone"),
        ))
        gone = MagicMock(closed=True, send_str=AsyncMock())
        server._ws_clients = {flaky, gone}
        server._register_subscription(flaky, "chainChanged")
        server._register_subscription(gone, "chainChanged")

        asyncio.run(server._broadcast_event("chainChanged", "0x1"))
        # Both removed from clients + subscriptions.
        assert flaky not in server._ws_clients
        assert gone not in server._ws_clients
        assert flaky not in server._ws_subscriptions
        assert gone not in server._ws_subscriptions

    def test_event_is_noop_when_loop_not_running(self):
        # Before .start() or after .stop() — must not raise.
        server = self._make_server()
        assert server._loop is None
        server.broadcast_accounts_changed(["0xabc"])
        server.broadcast_chain_changed(137)

    def test_eth_unsubscribe_removes_subscription(self):
        from unittest.mock import MagicMock
        server = self._make_server()
        ws = MagicMock(closed=False)
        sub_id = server._register_subscription(ws, "chainChanged")

        async def go():
            return await server._dispatch(
                "eth_unsubscribe", [sub_id], ws=ws,
            )
        result = asyncio.run(go())
        assert result is True
        assert "chainChanged" not in server._ws_subscriptions.get(ws, {})

    def test_dispatch_switch_chain_emits_chainChanged(self):
        from unittest.mock import AsyncMock, MagicMock
        import json as _json
        from qeth.rpc import RpcServer
        from qeth.chains import DEFAULT_CHAINS
        store = MagicMock()
        store.current_chain.return_value = DEFAULT_CHAINS[0]
        store.chains = DEFAULT_CHAINS
        server = RpcServer(store)
        ws = MagicMock(closed=False, send_str=AsyncMock())
        server._ws_clients = {ws}
        # Treat this ws as the calling dapp's socket so the
        # scoped chainChanged broadcast actually reaches it.
        server._ws_origin[ws] = "https://app.example"
        chain_sub = server._register_subscription(ws, "chainChanged")

        async def go():
            return await server._dispatch(
                "wallet_switchEthereumChain", [{"chainId": "0xa"}],
                origin="https://app.example",
            )
        asyncio.run(go())
        # Dapp-driven switch updates only that origin's chain.
        # The store and other origins remain untouched.
        store.set_current_chain.assert_not_called()
        assert server._rpc_chain_id_by_origin["https://app.example"] == 10
        # chainChanged push happened with the right subscription id.
        sent_payloads = [
            _json.loads(c.args[0]) for c in ws.send_str.call_args_list
        ]
        assert any(
            p["method"] == "eth_subscription"
            and p["params"]["subscription"] == chain_sub
            and p["params"]["result"] == "0xa"
            for p in sent_payloads
        )

    def test_set_rpc_chain_emits_chainChanged_to_unscoped(self):
        # UI ⇒ RPC asymmetric link: when the user picks a chain in
        # the wallet combo, dapps that haven't pinned themselves
        # via wallet_switchEthereumChain see the new chainId
        # immediately. The store is now the source of truth for
        # the default chain, so set_rpc_chain itself stores
        # nothing — it just pushes scoped notifications.
        from unittest.mock import AsyncMock, MagicMock
        from qeth.rpc import RpcServer
        from qeth.chains import DEFAULT_CHAINS
        store = MagicMock()
        store.current_chain.return_value = DEFAULT_CHAINS[0]
        store.chains = DEFAULT_CHAINS
        server = RpcServer(store)
        ws = MagicMock(closed=False, send_str=AsyncMock())
        server._ws_clients = {ws}
        server._register_subscription(ws, "chainChanged")
        server._register_subscription(ws, "networkChanged")

        # _schedule_event needs a running loop to actually push;
        # capture the calls (with kwargs) instead.
        seen: list = []
        server._schedule_event = (
            lambda t, r, **kw: seen.append((t, r, kw))
        )

        server.set_rpc_chain(100)
        # Both notifications fire, both filtered to unscoped
        # subscribers (dapps that haven't picked their own chain).
        assert (
            "chainChanged", "0x64", {"only_unscoped": True}
        ) in seen
        assert (
            "networkChanged", "100", {"only_unscoped": True}
        ) in seen

    def test_rpc_chain_is_decoupled_from_store_chain(self):
        # The dapp's RPC chain is tracked separately from the
        # wallet UI's chain. ``wallet_switchEthereumChain`` from a
        # dapp must NOT pull the user's UI selection along — they
        # should be able to look at Ethereum in the wallet while
        # the dapp transacts on Polygon. Tracking is per-origin so
        # other dapps stay on the UI's chain.
        from unittest.mock import MagicMock
        from qeth.rpc import RpcServer
        from qeth.chains import DEFAULT_CHAINS
        eth = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
        store = MagicMock()
        store.current_chain.return_value = eth
        store.chains = DEFAULT_CHAINS
        server = RpcServer(store)
        # Initial: any origin sees the store's current chain.
        assert server._chain_for_origin("https://a.example") == 1

        # Dapp at a.example switches to Polygon.
        async def switch():
            await server._dispatch(
                "wallet_switchEthereumChain", [{"chainId": "0x89"}],
                origin="https://a.example",
            )
        asyncio.run(switch())
        assert server._chain_for_origin("https://a.example") == 137
        # Other origins still see Ethereum.
        assert server._chain_for_origin("https://b.example") == 1
        # Store left alone.
        store.set_current_chain.assert_not_called()
        # eth_chainId, asked by a.example, reports the dapp chain.
        async def cid_a():
            return await server._dispatch(
                "eth_chainId", [], origin="https://a.example",
            )
        assert asyncio.run(cid_a()) == "0x89"
        # eth_chainId, asked by an unrelated origin, still reports
        # Ethereum — this is the leak the per-origin tracking fixes.
        async def cid_b():
            return await server._dispatch(
                "eth_chainId", [], origin="https://b.example",
            )
        assert asyncio.run(cid_b()) == "0x1"

    def test_dispatch_switch_unrecognized_chain_does_not_emit(self):
        from unittest.mock import AsyncMock, MagicMock
        from qeth.rpc import RpcError, RpcServer
        from qeth.chains import DEFAULT_CHAINS
        store = MagicMock()
        store.current_chain.return_value = DEFAULT_CHAINS[0]
        store.chains = DEFAULT_CHAINS
        server = RpcServer(store)
        ws = MagicMock(closed=False, send_str=AsyncMock())
        server._ws_clients = {ws}
        server._register_subscription(ws, "chainChanged")

        async def go():
            await server._dispatch(
                "wallet_switchEthereumChain", [{"chainId": "0xffffff"}],
            )
        with pytest.raises(RpcError):
            asyncio.run(go())
        ws.send_str.assert_not_awaited()


def test_chain_added_signal_carries_ids_above_qint32(qtbot):
    """Dapp-supplied chain ids (wallet_addEthereumChain) can exceed qint32 —
    Palm mainnet is 11297108109. The bridge's chain_added is declared
    ``Signal("qulonglong")`` so the id arrives exactly; ``Signal(int)``
    (qint32) would overflow at emit."""
    bridge = SignerBridge()
    got: list = []
    bridge.chain_added.connect(got.append)
    bridge.chain_added.emit(11297108109)
    assert got == [11297108109]
