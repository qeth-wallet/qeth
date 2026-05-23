"""Tests for qeth.chain — pure (no network)."""

import json
from decimal import Decimal

import pytest
from eth_abi import encode

from qeth.chain import (
    ChainError,
    EthClient,
    _SEL_AGGREGATE3,
    _SEL_BALANCE_OF,
    _decode_string_or_bytes32,
    wei_to_ether,
)
from qeth.chains import DEFAULT_CHAINS


# --- wei_to_ether ---------------------------------------------------------

class TestWeiToEther:
    def test_round_eth(self):
        assert wei_to_ether(10**18) == Decimal(1)

    def test_zero(self):
        assert wei_to_ether(0) == Decimal(0)

    def test_full_18_decimals_no_precision_loss(self):
        # The whole reason we use Decimal: this exact value round-trips,
        # whereas wei / 1e18 corrupts the last few digits.
        wei = 123_456_789_012_345_678
        got = wei_to_ether(wei)
        assert got == Decimal("0.123456789012345678")

    def test_huge_balance(self):
        wei = 10**27
        assert wei_to_ether(wei) == Decimal(10**9)

    def test_returns_decimal_not_float(self):
        assert isinstance(wei_to_ether(1), Decimal)


# --- multicall_erc20_balances ABI roundtrip -------------------------------

@pytest.fixture
def eth_client():
    return EthClient(DEFAULT_CHAINS[0])


def _fake_aggregate3_response(returns: list[tuple[bool, bytes]]) -> str:
    """Encode a Multicall3 aggregate3 response as the hex string EthClient
    would receive from the RPC."""
    return "0x" + encode(["(bool,bytes)[]"], [returns]).hex()


class TestMulticallBalances:
    def test_empty_input_returns_empty(self, eth_client):
        assert eth_client.multicall_erc20_balances([], "0xdead") == {}

    def test_happy_path(self, eth_client, monkeypatch):
        tokens = [
            "0xAAAAaaAAaaAAAAAaaaaaaaaAAaaaaaaaAaaAAAAA",
            "0xBbBbbbBBbbbBBBbbbbBBbbBBbbbBBbBbbbbbbBBb",
        ]
        balances = [12345, 67890]
        response = _fake_aggregate3_response([
            (True, b.to_bytes(32, "big")) for b in balances
        ])

        seen = {}
        def fake_call(self, tx, block="latest"):
            seen["tx"] = tx
            return response
        monkeypatch.setattr(EthClient, "call", fake_call)

        out = eth_client.multicall_erc20_balances(tokens, "0xC0FFee00C0FFee00C0FFee00C0FFee00C0FFee00")

        assert out == {tokens[0].lower(): 12345, tokens[1].lower(): 67890}
        # Verify it actually issued an aggregate3 call against multicall3
        assert seen["tx"]["to"].lower() == "0xca11bde05977b3631167028862bE2a173976CA11".lower()
        assert seen["tx"]["data"].startswith("0x" + _SEL_AGGREGATE3.hex())

    def test_failed_inner_calls_are_skipped(self, eth_client, monkeypatch):
        tokens = [
            "0xAAAAaaAAaaAAAAAaaaaaaaaAAaaaaaaaAaaAAAAA",
            "0xBbBbbbBBbbbBBBbbbbBBbbBBbbbBBbBbbbbbbBBb",
            "0xCCccCccCcCCccccCCCCcCCcCcCCCccccccCCccCC",
        ]
        response = _fake_aggregate3_response([
            (True, (100).to_bytes(32, "big")),
            (False, b""),                        # reverted
            (True, (300).to_bytes(32, "big")),
        ])
        monkeypatch.setattr(EthClient, "call", lambda self, tx, block="latest": response)
        out = eth_client.multicall_erc20_balances(tokens, "0xc0ffee")
        assert out == {tokens[0].lower(): 100, tokens[2].lower(): 300}

    def test_batching_splits_large_input(self, eth_client, monkeypatch):
        # 250 tokens with batch_size=100 -> 3 batches (100 + 100 + 50)
        tokens = [f"0x{i:040x}" for i in range(250)]
        # Return a response per batch with all-success entries.
        calls = []
        def fake_call(self, tx, block="latest"):
            data_hex = tx["data"][2:]
            # The first 4 bytes are the selector; the rest is the encoded
            # array. Decode just to find the batch size.
            from eth_abi import decode
            calldata = bytes.fromhex(data_hex)
            calls_arg = decode(["(address,bool,bytes)[]"], calldata[4:])[0]
            calls.append(len(calls_arg))
            return _fake_aggregate3_response([
                (True, (i + 1).to_bytes(32, "big")) for i in range(len(calls_arg))
            ])
        monkeypatch.setattr(EthClient, "call", fake_call)

        out = eth_client.multicall_erc20_balances(tokens, "0xc0ffee", batch_size=100)

        assert calls == [100, 100, 50]
        # We got 250 unique tokens back — order of integers per batch
        # restarts at 1, so duplicates would collapse the dict; verify
        # by counting:
        assert len(out) == 250

    def test_batch_exception_is_tolerated(self, eth_client, monkeypatch):
        tokens = [f"0x{i:040x}" for i in range(2)]
        def fake_call(self, tx, block="latest"):
            raise RuntimeError("RPC down")
        monkeypatch.setattr(EthClient, "call", fake_call)
        # Should NOT raise; just returns empty.
        assert eth_client.multicall_erc20_balances(tokens, "0xc0ffee") == {}


# --- multicall_erc20_metadata --------------------------------------------

class TestMulticallMetadata:
    def test_string_decode(self, eth_client, monkeypatch):
        tokens = ["0xAAAAaaAAaaAAAAAaaaaaaaaAAaaaaaaaAaaAAAAA"]
        name_bytes = encode(["string"], ["USD Coin"])
        symbol_bytes = encode(["string"], ["USDC"])
        decimals_bytes = (6).to_bytes(32, "big")
        response = _fake_aggregate3_response([
            (True, name_bytes), (True, symbol_bytes), (True, decimals_bytes),
        ])
        monkeypatch.setattr(EthClient, "call", lambda self, tx, block="latest": response)
        out = eth_client.multicall_erc20_metadata(tokens)
        assert out == {tokens[0].lower(): {"symbol": "USDC", "name": "USD Coin", "decimals": 6}}

    def test_bytes32_legacy_decode(self, eth_client, monkeypatch):
        """MKR-style: name/symbol returned as bytes32, NUL-padded right."""
        tokens = ["0xAAAAaaAAaaAAAAAaaaaaaaaAAaaaaaaaAaaAAAAA"]
        name_padded = b"Maker" + b"\x00" * (32 - 5)
        symbol_padded = b"MKR" + b"\x00" * (32 - 3)
        decimals_bytes = (18).to_bytes(32, "big")
        response = _fake_aggregate3_response([
            (True, name_padded), (True, symbol_padded), (True, decimals_bytes),
        ])
        monkeypatch.setattr(EthClient, "call", lambda self, tx, block="latest": response)
        out = eth_client.multicall_erc20_metadata(tokens)
        assert out == {tokens[0].lower(): {"symbol": "MKR", "name": "Maker", "decimals": 18}}

    def test_token_with_no_symbol_is_dropped(self, eth_client, monkeypatch):
        """Entries with empty symbol are unusable for display."""
        tokens = ["0xAAAAaaAAaaAAAAAaaaaaaaaAAaaaaaaaAaaAAAAA"]
        name_bytes = encode(["string"], ["No Symbol Coin"])
        symbol_bytes = encode(["string"], [""])
        decimals_bytes = (18).to_bytes(32, "big")
        response = _fake_aggregate3_response([
            (True, name_bytes), (True, symbol_bytes), (True, decimals_bytes),
        ])
        monkeypatch.setattr(EthClient, "call", lambda self, tx, block="latest": response)
        assert eth_client.multicall_erc20_metadata(tokens) == {}


# --- EthClient.rpc + the simple wrappers ---------------------------------

def _patch_rpc_response(monkeypatch, response: dict) -> dict:
    """Patch urllib.request.urlopen so EthClient sees ``response`` as the
    parsed JSON-RPC reply. Returns a dict that captures what the call
    actually sent (URL, headers, decoded body) so tests can assert on it.
    """
    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode())
        body = json.dumps(response).encode()

        class _R:
            def read(self): return body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        return _R()

    monkeypatch.setattr("qeth.chain.urllib.request.urlopen", fake_urlopen)
    return captured


class TestRpcDispatcher:
    def test_posts_jsonrpc_envelope(self, eth_client, monkeypatch):
        captured = _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "result": "0xdead",
        })
        out = eth_client.rpc("eth_someMethod", ["param1", 42])
        assert out == "0xdead"
        assert captured["method"] == "POST"
        assert captured["url"] == eth_client.chain.rpc_url
        assert captured["body"] == {
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_someMethod", "params": ["param1", 42],
        }

    def test_sends_qeth_user_agent(self, eth_client, monkeypatch):
        """DRPC's Cloudflare front 403s the default Python-urllib UA;
        this is exactly what the User-Agent override exists to prevent."""
        captured = _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "result": "0x1",
        })
        eth_client.rpc("eth_chainId")
        # urllib stores header names title-cased.
        assert any("qeth" in v.lower() for v in captured["headers"].values())

    def test_no_params_defaults_to_empty_list(self, eth_client, monkeypatch):
        captured = _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "result": "0x0",
        })
        eth_client.rpc("eth_blockNumber")
        assert captured["body"]["params"] == []

    def test_error_response_raises_ChainError(self, eth_client, monkeypatch):
        _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1,
            "error": {"code": -32000, "message": "broken"},
        })
        with pytest.raises(ChainError) as exc:
            eth_client.rpc("eth_blockNumber")
        assert exc.value.code == -32000
        assert "broken" in exc.value.message

    def test_error_response_with_missing_message(self, eth_client, monkeypatch):
        """Provider sends a code but no message — should still raise
        without crashing, with a default message."""
        _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "error": {"code": -32000},
        })
        with pytest.raises(ChainError) as exc:
            eth_client.rpc("eth_blockNumber")
        assert exc.value.code == -32000
        assert exc.value.message  # non-empty default


class TestEthMethodWrappers:
    """Each of these uses rpc() under the hood and parses the hex result."""

    def test_get_balance(self, eth_client, monkeypatch):
        captured = _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1,
            "result": "0x4b3b4ca85a86c47a098a224000000000",
        })
        bal = eth_client.get_balance("0xdead")
        assert bal == int("0x4b3b4ca85a86c47a098a224000000000", 16)
        assert captured["body"]["method"] == "eth_getBalance"
        assert captured["body"]["params"] == ["0xdead", "latest"]

    def test_get_balance_with_explicit_block(self, eth_client, monkeypatch):
        captured = _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "result": "0x0",
        })
        eth_client.get_balance("0xdead", block="0x100")
        assert captured["body"]["params"] == ["0xdead", "0x100"]

    def test_get_block_number(self, eth_client, monkeypatch):
        _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "result": "0x1234567",
        })
        assert eth_client.get_block_number() == 0x1234567

    def test_chain_id(self, eth_client, monkeypatch):
        captured = _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "result": "0xa",
        })
        assert eth_client.chain_id() == 10
        assert captured["body"]["method"] == "eth_chainId"

    def test_get_transaction_count_defaults_to_pending(self, eth_client, monkeypatch):
        """Nonce queries should default to 'pending' so back-to-back tx
        submissions don't collide on the same nonce."""
        captured = _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "result": "0x5",
        })
        n = eth_client.get_transaction_count("0xdead")
        assert n == 5
        assert captured["body"]["method"] == "eth_getTransactionCount"
        assert captured["body"]["params"] == ["0xdead", "pending"]

    def test_gas_price(self, eth_client, monkeypatch):
        _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "result": "0x3b9aca00",  # 1 gwei
        })
        assert eth_client.gas_price() == 1_000_000_000

    def test_max_priority_fee(self, eth_client, monkeypatch):
        captured = _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "result": "0x77359400",  # 2 gwei
        })
        assert eth_client.max_priority_fee() == 2_000_000_000
        assert captured["body"]["method"] == "eth_maxPriorityFeePerGas"

    def test_estimate_gas(self, eth_client, monkeypatch):
        captured = _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "result": "0x5208",  # 21000
        })
        tx = {"from": "0xdead", "to": "0xbeef", "value": "0x1"}
        assert eth_client.estimate_gas(tx) == 21000
        assert captured["body"]["method"] == "eth_estimateGas"
        assert captured["body"]["params"] == [tx]

    def test_call(self, eth_client, monkeypatch):
        captured = _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "result": "0xc0ffee",
        })
        out = eth_client.call({"to": "0xdead", "data": "0x1234"})
        assert out == "0xc0ffee"
        assert captured["body"]["method"] == "eth_call"
        assert captured["body"]["params"] == [
            {"to": "0xdead", "data": "0x1234"}, "latest",
        ]

    def test_send_raw_transaction_bytes(self, eth_client, monkeypatch):
        captured = _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1,
            "result": "0xaabbccddee00000000000000000000000000000000000000000000000000000000",
        })
        raw = bytes.fromhex("aabb")
        txh = eth_client.send_raw_transaction(raw)
        assert txh.startswith("0x")
        assert captured["body"]["method"] == "eth_sendRawTransaction"
        # Bytes should be hex-encoded with 0x prefix in the request.
        assert captured["body"]["params"] == ["0xaabb"]

    def test_send_raw_transaction_str_passthrough(self, eth_client, monkeypatch):
        """If the caller already hex-encoded the signed transaction, it
        should pass through unchanged."""
        captured = _patch_rpc_response(monkeypatch, {
            "jsonrpc": "2.0", "id": 1, "result": "0xdeadbeef",
        })
        eth_client.send_raw_transaction("0xf86b...")
        assert captured["body"]["params"] == ["0xf86b..."]


class TestDecodeStringOrBytes32:
    def test_string(self):
        assert _decode_string_or_bytes32(encode(["string"], ["hello"])) == "hello"

    def test_bytes32_padded(self):
        assert _decode_string_or_bytes32(b"USDC" + b"\x00" * 28) == "USDC"

    def test_garbage_returns_empty(self):
        assert _decode_string_or_bytes32(b"\x00" * 7) == ""
