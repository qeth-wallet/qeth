"""Tests for qeth.chain — pure (no network).

EthClient is web3.py-backed; offline tests mock at the provider's
``make_request`` method or at ``EthClient.call`` (which the Multicall
context manager calls into).
"""

from decimal import Decimal

import pytest
from eth_abi import encode
from web3 import Web3


def _addr(seed: str) -> str:
    """Build a valid checksummed test address. web3 7.x rejects pure
    lowercase / random-case addresses — they must round-trip through
    Web3.to_checksum_address."""
    h = (seed * 40)[:40]
    return Web3.to_checksum_address("0x" + h)

from qeth.chain import (
    ChainError,
    EthClient,
    Multicall,
    _SEL_AGGREGATE3,
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
        wei = 123_456_789_012_345_678
        assert wei_to_ether(wei) == Decimal("0.123456789012345678")

    def test_huge_balance(self):
        assert wei_to_ether(10**27) == Decimal(10**9)

    def test_returns_decimal_not_float(self):
        assert isinstance(wei_to_ether(1), Decimal)


# --- fixtures -------------------------------------------------------------

@pytest.fixture
def eth_client():
    return EthClient(DEFAULT_CHAINS[0])


def _patch_provider(monkeypatch, client, responder):
    """Stub the underlying HTTPProvider's make_request.

    ``responder`` may be either a dict (used verbatim) or a callable
    ``(method, params) -> RPCResponse dict``. Captures invocations so
    tests can assert what was sent."""
    captured: list[tuple[str, list]] = []

    def fake(method, params):
        captured.append((method, list(params)))
        return responder(method, params) if callable(responder) else responder

    monkeypatch.setattr(client._w3.provider, "make_request", fake)
    return captured


def _aggregate3_response(returns: list[tuple[bool, bytes]]) -> str:
    """Encode a Multicall3 aggregate3 response as the 0x-hex string an
    eth_call would return."""
    return "0x" + encode(["(bool,bytes)[]"], [returns]).hex()


# --- EthClient.rpc + the simple wrappers ----------------------------------

class TestRpcDispatcher:
    def test_envelope_passed_through(self, eth_client, monkeypatch):
        captured = _patch_provider(monkeypatch, eth_client, {
            "jsonrpc": "2.0", "id": 1, "result": "0xdead",
        })
        out = eth_client.rpc("eth_someMethod", ["param1", 42])
        assert out == "0xdead"
        assert captured == [("eth_someMethod", ["param1", 42])]

    def test_no_params_defaults_to_empty_list(self, eth_client, monkeypatch):
        captured = _patch_provider(monkeypatch, eth_client, {
            "jsonrpc": "2.0", "id": 1, "result": "0x0",
        })
        eth_client.rpc("eth_blockNumber")
        assert captured == [("eth_blockNumber", [])]

    def test_qeth_user_agent_on_session(self, eth_client):
        """DRPC's Cloudflare front 403s the default UA; the override
        lives on the requests Session that HTTPProvider hands off to."""
        ua = eth_client._session.headers.get("User-Agent", "")
        assert "qeth" in ua.lower()

    def test_error_response_raises_ChainError(self, eth_client, monkeypatch):
        _patch_provider(monkeypatch, eth_client, {
            "jsonrpc": "2.0", "id": 1,
            "error": {"code": -32000, "message": "broken"},
        })
        with pytest.raises(ChainError) as exc:
            eth_client.rpc("eth_blockNumber")
        assert exc.value.code == -32000
        assert "broken" in exc.value.message


class TestEthMethodWrappers:
    """Each wrapper goes through web3.eth.* which in turn calls
    make_request on the provider — assert on the right method name."""

    def test_get_balance(self, eth_client, monkeypatch):
        addr = _addr("a")
        captured = _patch_provider(monkeypatch, eth_client, {
            "jsonrpc": "2.0", "id": 1,
            "result": "0x4b3b4ca85a86c47a098a224000000000",
        })
        assert eth_client.get_balance(addr) == int(
            "0x4b3b4ca85a86c47a098a224000000000", 16
        )
        assert captured[0][0] == "eth_getBalance"
        assert captured[0][1][0].lower() == addr.lower()
        assert captured[0][1][1] == "latest"

    def test_get_balance_with_explicit_block(self, eth_client, monkeypatch):
        addr = _addr("a")
        captured = _patch_provider(monkeypatch, eth_client, {
            "jsonrpc": "2.0", "id": 1, "result": "0x0",
        })
        eth_client.get_balance(addr, block="0x1000")
        assert captured[0][0] == "eth_getBalance"

    def test_get_balance_checksums_lowercase_address(self, eth_client,
                                                     monkeypatch):
        """A lowercase address (how watch-only/paste accounts are stored)
        must be checksummed before web3 — web3.eth.get_balance EIP-55
        validates and *raises* on mixed/lower case, which silently zeroed
        the native balance in the flatpak. It must reach the node already
        checksummed, not raise."""
        checksummed = _addr("a")
        captured = _patch_provider(monkeypatch, eth_client, {
            "jsonrpc": "2.0", "id": 1, "result": "0x10",
        })
        assert eth_client.get_balance(checksummed.lower()) == 0x10
        assert captured[0][1][0] == checksummed   # not the lowercase form

    def test_get_transaction_count_checksums_lowercase_address(
            self, eth_client, monkeypatch):
        checksummed = _addr("b")
        captured = _patch_provider(monkeypatch, eth_client, {
            "jsonrpc": "2.0", "id": 1, "result": "0x5",
        })
        assert eth_client.get_transaction_count(checksummed.lower()) == 5
        assert captured[0][1][0] == checksummed

    def test_get_block_number(self, eth_client, monkeypatch):
        _patch_provider(monkeypatch, eth_client, {
            "jsonrpc": "2.0", "id": 1, "result": "0x1234567",
        })
        assert eth_client.get_block_number() == 0x1234567

    def test_chain_id(self, eth_client, monkeypatch):
        captured = _patch_provider(monkeypatch, eth_client, {
            "jsonrpc": "2.0", "id": 1, "result": "0xa",
        })
        assert eth_client.chain_id() == 10
        assert captured[0][0] == "eth_chainId"

    def test_get_transaction_count_defaults_to_pending(self, eth_client, monkeypatch):
        """Nonce queries default to 'pending' so back-to-back tx
        submissions don't collide on the same nonce."""
        addr = _addr("a")
        captured = _patch_provider(monkeypatch, eth_client, {
            "jsonrpc": "2.0", "id": 1, "result": "0x5",
        })
        assert eth_client.get_transaction_count(addr) == 5
        assert captured[0][0] == "eth_getTransactionCount"
        assert captured[0][1][1] == "pending"

    def test_gas_price(self, eth_client, monkeypatch):
        _patch_provider(monkeypatch, eth_client, {
            "jsonrpc": "2.0", "id": 1, "result": "0x3b9aca00",
        })
        assert eth_client.gas_price() == 1_000_000_000

    def test_estimate_gas(self, eth_client, monkeypatch):
        # web3 may issue an extra eth_chainId for middleware checks
        # before the real eth_estimateGas; look it up in the capture
        # rather than assuming index 0.
        captured = _patch_provider(monkeypatch, eth_client, {
            "jsonrpc": "2.0", "id": 1, "result": "0x5208",
        })
        tx = {"from": _addr("a"), "to": _addr("b"), "value": "0x1"}
        assert eth_client.estimate_gas(tx) == 21000
        methods = [m for m, _ in captured]
        assert "eth_estimateGas" in methods

    def test_call_returns_hex(self, eth_client, monkeypatch):
        _patch_provider(monkeypatch, eth_client, {
            "jsonrpc": "2.0", "id": 1, "result": "0xc0ffee",
        })
        out = eth_client.call({"to": _addr("b"), "data": "0x"})
        assert out.startswith("0x") and "c0ffee" in out


# --- Multicall context manager --------------------------------------------

class TestMulticallContextManager:
    def test_empty_context_doesnt_call(self, eth_client, monkeypatch):
        """No queued calls → no eth_call issued."""
        seen = []
        monkeypatch.setattr(eth_client, "call",
                            lambda *a, **kw: seen.append(a) or "0x")
        with eth_client.multicall() as mc:
            pass
        assert seen == []

    def test_balance_of(self, eth_client, monkeypatch):
        token = "0x" + "a" * 40
        holder = "0x" + "b" * 40
        response = _aggregate3_response([(True, (12345).to_bytes(32, "big"))])
        monkeypatch.setattr(eth_client, "call",
                            lambda tx, block="latest": response)
        with eth_client.multicall() as mc:
            p = mc.balance_of(token, holder)
        assert p.success is True
        assert p.value == 12345

    def test_value_unreadable_before_exit(self, eth_client, monkeypatch):
        token = "0x" + "a" * 40
        holder = "0x" + "b" * 40
        # Don't patch — won't be flushed mid-context anyway
        with eth_client.multicall() as mc:
            p = mc.balance_of(token, holder)
            assert p.success is None    # not yet flushed
            assert p.value is None

    def test_failed_inner_call_marked_unsuccessful(self, eth_client, monkeypatch):
        tokens = ["0x" + c * 40 for c in "ab"]
        holder = "0x" + "c" * 40
        response = _aggregate3_response([
            (True, (100).to_bytes(32, "big")),
            (False, b""),
        ])
        monkeypatch.setattr(eth_client, "call",
                            lambda tx, block="latest": response)
        with eth_client.multicall() as mc:
            ok = mc.balance_of(tokens[0], holder)
            bad = mc.balance_of(tokens[1], holder)
        assert ok.success and ok.value == 100
        assert bad.success is False and bad.value is None

    def test_metadata_helpers(self, eth_client, monkeypatch):
        token = "0x" + "a" * 40
        response = _aggregate3_response([
            (True, encode(["string"], ["USD Coin"])),
            (True, encode(["string"], ["USDC"])),
            (True, (6).to_bytes(32, "big")),
        ])
        monkeypatch.setattr(eth_client, "call",
                            lambda tx, block="latest": response)
        with eth_client.multicall() as mc:
            name_f = mc.name(token)
            sym_f = mc.symbol(token)
            dec_f = mc.decimals(token)
        assert name_f.value == "USD Coin"
        assert sym_f.value == "USDC"
        assert dec_f.value == 6

    def test_legacy_bytes32_metadata(self, eth_client, monkeypatch):
        """MKR returns bytes32 padded with NULs; the string decoder
        falls back to bytes32 and strips."""
        token = "0x" + "a" * 40
        response = _aggregate3_response([
            (True, b"Maker" + b"\x00" * 27),
            (True, b"MKR" + b"\x00" * 29),
            (True, (18).to_bytes(32, "big")),
        ])
        monkeypatch.setattr(eth_client, "call",
                            lambda tx, block="latest": response)
        with eth_client.multicall() as mc:
            name_f = mc.name(token)
            sym_f = mc.symbol(token)
            dec_f = mc.decimals(token)
        assert name_f.value == "Maker"
        assert sym_f.value == "MKR"
        assert dec_f.value == 18

    def test_add_with_custom_decoder(self, eth_client, monkeypatch):
        token = "0x" + "a" * 40
        response = _aggregate3_response([(True, b"\x00" * 31 + b"\x2a")])  # 42
        monkeypatch.setattr(eth_client, "call",
                            lambda tx, block="latest": response)
        with eth_client.multicall() as mc:
            p = mc.add(token, b"\x00\x00\x00\x00",
                       decoder=lambda b: int.from_bytes(b[:32], "big"))
        assert p.value == 42

    def test_add_without_decoder_returns_raw(self, eth_client, monkeypatch):
        token = "0x" + "a" * 40
        response = _aggregate3_response([(True, b"\xff\xee")])
        monkeypatch.setattr(eth_client, "call",
                            lambda tx, block="latest": response)
        with eth_client.multicall() as mc:
            p = mc.add(token, b"\x00\x00\x00\x00")
        assert p.value == b"\xff\xee"

    def test_batching_splits_into_chunks(self, eth_client, monkeypatch):
        tokens = [f"0x{i:040x}" for i in range(75)]
        holder = "0x" + "c" * 40
        chunks = []
        def fake_call(tx, block="latest"):
            from eth_abi import decode as _decode
            data = bytes.fromhex(tx["data"][2:])
            calls_arg = _decode(["(address,bool,bytes)[]"], data[4:])[0]
            chunks.append(len(calls_arg))
            return _aggregate3_response([
                (True, (i + 1).to_bytes(32, "big")) for i in range(len(calls_arg))
            ])
        monkeypatch.setattr(eth_client, "call", fake_call)
        with eth_client.multicall(batch_size=30) as mc:
            futures = [mc.balance_of(t, holder) for t in tokens]
        # 75 / 30 -> 30 + 30 + 15
        assert chunks == [30, 30, 15]
        assert all(f.success for f in futures)

    def test_batch_exception_marks_all_failed(self, eth_client, monkeypatch):
        tokens = ["0x" + c * 40 for c in "ab"]
        def boom(*a, **kw):
            raise RuntimeError("network blew up")
        monkeypatch.setattr(eth_client, "call", boom)
        with eth_client.multicall() as mc:
            futs = [mc.balance_of(t, "0x" + "c" * 40) for t in tokens]
        # No exception leaks out of the context; all calls just failed.
        assert all(f.success is False for f in futs)
        assert all(f.value is None for f in futs)

    def test_decoder_exception_marks_call_failed(self, eth_client, monkeypatch):
        """If a decoder raises (e.g. malformed return data) the call's
        ``success`` should flip to False rather than the exception
        propagating."""
        token = "0x" + "a" * 40
        response = _aggregate3_response([(True, b"\x01\x02\x03")])  # too short
        monkeypatch.setattr(eth_client, "call",
                            lambda tx, block="latest": response)
        def explode(b):
            raise ValueError("nope")
        with eth_client.multicall() as mc:
            p = mc.add(token, b"\x00\x00\x00\x00", decoder=explode)
        assert p.success is False
        assert p.value is None

    def test_context_with_exception_doesnt_flush(self, eth_client, monkeypatch):
        """If the with-block raises, we shouldn't try to issue the
        multicall — the caller is bailing out."""
        called = []
        monkeypatch.setattr(eth_client, "call",
                            lambda *a, **kw: called.append(a) or "0x")
        token = "0x" + "a" * 40
        holder = "0x" + "b" * 40
        with pytest.raises(RuntimeError):
            with eth_client.multicall() as mc:
                mc.balance_of(token, holder)
                raise RuntimeError("caller bailed")
        assert called == []


# --- multicall_erc20_balances / _metadata (built on Multicall) ------------

class TestMulticallERC20Balances:
    def test_empty_input(self, eth_client):
        assert eth_client.multicall_erc20_balances([], "0x" + "c" * 40) == {}

    def test_happy_path(self, eth_client, monkeypatch):
        tokens = ["0x" + c * 40 for c in "ab"]
        holder = "0x" + "c" * 40
        response = _aggregate3_response([
            (True, b.to_bytes(32, "big")) for b in [12345, 67890]
        ])
        monkeypatch.setattr(eth_client, "call",
                            lambda tx, block="latest": response)
        out = eth_client.multicall_erc20_balances(tokens, holder)
        assert out == {tokens[0].lower(): 12345, tokens[1].lower(): 67890}

    def test_revert_omitted_from_result(self, eth_client, monkeypatch):
        tokens = ["0x" + c * 40 for c in "abc"]
        holder = "0x" + "c" * 40
        response = _aggregate3_response([
            (True, (100).to_bytes(32, "big")),
            (False, b""),
            (True, (300).to_bytes(32, "big")),
        ])
        monkeypatch.setattr(eth_client, "call",
                            lambda tx, block="latest": response)
        out = eth_client.multicall_erc20_balances(tokens, holder)
        assert out == {tokens[0].lower(): 100, tokens[2].lower(): 300}


class TestMulticallERC20Metadata:
    def test_string_decode(self, eth_client, monkeypatch):
        tokens = ["0x" + "a" * 40]
        response = _aggregate3_response([
            (True, encode(["string"], ["USD Coin"])),
            (True, encode(["string"], ["USDC"])),
            (True, (6).to_bytes(32, "big")),
        ])
        monkeypatch.setattr(eth_client, "call",
                            lambda tx, block="latest": response)
        out = eth_client.multicall_erc20_metadata(tokens)
        assert out == {tokens[0].lower(): {
            "symbol": "USDC", "name": "USD Coin", "decimals": 6,
        }}

    def test_empty_symbol_drops_entry(self, eth_client, monkeypatch):
        tokens = ["0x" + "a" * 40]
        response = _aggregate3_response([
            (True, encode(["string"], ["Some Coin"])),
            (True, encode(["string"], [""])),       # empty symbol
            (True, (18).to_bytes(32, "big")),
        ])
        monkeypatch.setattr(eth_client, "call",
                            lambda tx, block="latest": response)
        assert eth_client.multicall_erc20_metadata(tokens) == {}


class TestDecodeStringOrBytes32:
    def test_string(self):
        assert _decode_string_or_bytes32(encode(["string"], ["hello"])) == "hello"

    def test_bytes32_padded(self):
        assert _decode_string_or_bytes32(b"USDC" + b"\x00" * 28) == "USDC"

    def test_garbage_returns_empty(self):
        assert _decode_string_or_bytes32(b"\x00" * 7) == ""
