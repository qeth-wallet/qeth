"""Tests for qeth.chain — pure (no network)."""

from decimal import Decimal

import pytest
from eth_abi import encode

from qeth.chain import (
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


class TestDecodeStringOrBytes32:
    def test_string(self):
        assert _decode_string_or_bytes32(encode(["string"], ["hello"])) == "hello"

    def test_bytes32_padded(self):
        assert _decode_string_or_bytes32(b"USDC" + b"\x00" * 28) == "USDC"

    def test_garbage_returns_empty(self):
        assert _decode_string_or_bytes32(b"\x00" * 7) == ""
