"""Hermetic tests for qeth.simulate — the log-extraction logic.

A fake EVM class is injected so these never fork a real chain. The live
pyrevm-against-mainnet path is exercised manually (it's slow + networked).
"""

from types import SimpleNamespace

from qeth.simulate import simulate_logs

CHAIN = SimpleNamespace(chain_id=1, rpc_url="https://rpc.example/eth")
FROM = "0x7a16ff8270133f063aab6c9977183d9e72835428"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class _FakeLog:
    def __init__(self, address, topics, data_bytes):
        self.address = address
        self.topics = topics
        # pyrevm shape: .data is a (topics, data_bytes) tuple.
        self.data = (topics, data_bytes)


class _FakeEVM:
    """Records construction + the message_call, returns one Transfer."""
    seen: dict = {}

    def __init__(self, fork_url=None):
        _FakeEVM.seen["fork_url"] = fork_url

    def message_call(self, **kwargs):
        _FakeEVM.seen["call"] = kwargs
        self.result = SimpleNamespace(logs=[
            _FakeLog(USDC, [TRANSFER, "0x" + "00" * 31 + "01"],
                     b"\x00" * 31 + b"\x05"),
        ])


def test_returns_decode_ready_log_dicts():
    _FakeEVM.seen = {}
    logs = simulate_logs(CHAIN, FROM, USDC, "0xa9059cbb", 0, evm_cls=_FakeEVM)
    assert _FakeEVM.seen["fork_url"] == CHAIN.rpc_url
    assert len(logs) == 1
    lg = logs[0]
    assert lg["address"] == USDC
    assert lg["topics"][0] == TRANSFER
    assert lg["data"] == "0x" + "00" * 31 + "05"


def test_calldata_and_addresses_are_normalised():
    _FakeEVM.seen = {}
    simulate_logs(CHAIN, FROM, USDC, "0xa9059cbb00ff", 0, evm_cls=_FakeEVM)
    call = _FakeEVM.seen["call"]
    assert call["calldata"] == bytes.fromhex("a9059cbb00ff")
    # web3/pyrevm want checksum addresses — the lowercased inputs are fixed.
    assert call["caller"].lower() == FROM
    assert call["caller"] != FROM           # i.e. it got checksummed
    assert "value" not in call               # zero value omitted


def test_value_is_passed_when_nonzero():
    _FakeEVM.seen = {}
    simulate_logs(CHAIN, FROM, USDC, "0x", 10**18, evm_cls=_FakeEVM)
    assert _FakeEVM.seen["call"]["value"] == 10**18


def test_contract_creation_returns_none():
    assert simulate_logs(CHAIN, FROM, None, "0x", 0, evm_cls=_FakeEVM) is None


def test_simulation_error_returns_none():
    class _Boom:
        def __init__(self, fork_url=None): pass
        def message_call(self, **kw): raise RuntimeError("revm exploded")
    assert simulate_logs(CHAIN, FROM, USDC, "0x", 0, evm_cls=_Boom) is None


# --- revert-reason decoding (pyrevm raises RuntimeError with output bytes) ---

from qeth.simulate import _decode_revert


def test_decode_revert_error_string():
    # Error(string) "ERC20: transfer amount exceeds balance"
    out = ("0x08c379a0"
           "0000000000000000000000000000000000000000000000000000000000000020"
           "0000000000000000000000000000000000000000000000000000000000000026"
           "45524332303a207472616e7366657220616d6f756e7420657863656564732062"
           "616c616e63650000000000000000000000000000000000000000000000000000")
    msg = f"Revert {{ gas_used: 36085, output: {out} }}"
    assert _decode_revert(msg) == "ERC20: transfer amount exceeds balance"


def test_decode_revert_panic():
    msg = ("Revert { output: 0x4e487b71"
           "0000000000000000000000000000000000000000000000000000000000000011 }")
    assert _decode_revert(msg) == "panic 0x11"


def test_decode_revert_no_reason_and_unknown():
    assert _decode_revert("Revert { output: 0x }") == \
        "reverted without a reason string"
    assert "selector 0xdeadbeef" in _decode_revert(
        "Revert { output: 0xdeadbeef }")


def test_rate_limited_retries_then_succeeds():
    # message_call raises a rate-limit twice, then succeeds; the helper
    # should back off (injected no-op sleep) and return the logs.
    class _Flaky:
        attempts = 0
        def __init__(self, fork_url=None): pass
        def message_call(self, **kw):
            _Flaky.attempts += 1
            if _Flaky.attempts < 3:
                raise RuntimeError(
                    'JsonRpcError { code: 15, message: "Too many request" }')
            self.result = SimpleNamespace(logs=[
                _FakeLog(USDC, [TRANSFER], b"\x01")])
    delays = []
    logs = simulate_logs(CHAIN, FROM, USDC, "0x", 0, evm_cls=_Flaky,
                         sleep=delays.append)
    assert _Flaky.attempts == 3        # two failures + one success
    assert len(delays) == 2            # backed off before each retry
    assert logs and len(logs) == 1


def test_rate_limited_gives_up_after_retries():
    class _AlwaysLimited:
        def __init__(self, fork_url=None): pass
        def message_call(self, **kw):
            raise RuntimeError('code: 15, message: "Too many request"')
    logs = simulate_logs(CHAIN, FROM, USDC, "0x", 0, evm_cls=_AlwaysLimited,
                         retries=3, sleep=lambda d: None)
    assert logs is None


def test_revert_is_not_retried():
    # A genuine revert must fail fast — no backoff, single attempt.
    class _Reverter:
        attempts = 0
        def __init__(self, fork_url=None): pass
        def message_call(self, **kw):
            _Reverter.attempts += 1
            raise RuntimeError("Revert { output: 0x }")
    delays = []
    assert simulate_logs(CHAIN, FROM, USDC, "0x", 0, evm_cls=_Reverter,
                         sleep=delays.append) is None
    assert _Reverter.attempts == 1 and delays == []


def test_injected_evm_skips_networked_block_env():
    # With evm_cls injected the helper must not touch the network for a
    # block env — _FakeEVM has no set_block_env and a fork_url to nowhere.
    _FakeEVM.seen = {}
    logs = simulate_logs(CHAIN, FROM, USDC, "0xa9059cbb", 0, evm_cls=_FakeEVM)
    assert logs and "block" not in _FakeEVM.seen   # no set_block_env call


# --- eth_simulateV1 fast path + orchestrator routing -------------------------

import pytest
import qeth.simulate as sim
from qeth.simulate import (
    _SimV1Unsupported, _decode_revert_output, _is_method_unsupported,
    _logs_from_simv1, simulation_available,
)


def test_logs_from_simv1_success_and_revert():
    log = {"address": USDC, "topics": [TRANSFER, "0x" + "00" * 31 + "01"],
           "data": "0x" + "00" * 31 + "05"}
    ok = [{"calls": [{"status": "0x1", "logs": [log]}]}]
    out = _logs_from_simv1(ok)
    assert len(out) == 1 and out[0]["address"] == USDC
    assert out[0]["topics"][0] == TRANSFER and out[0]["data"].endswith("05")
    # A reverted call → None (definitive), not a fall-back trigger.
    reverted = [{"calls": [{"status": "0x0", "error": {"message": "boom"},
                            "logs": []}]}]
    assert _logs_from_simv1(reverted) is None


def test_decode_revert_output_direct():
    out = ("0x08c379a0"
           "0000000000000000000000000000000000000000000000000000000000000020"
           "0000000000000000000000000000000000000000000000000000000000000026"
           "45524332303a207472616e7366657220616d6f756e7420657863656564732062"
           "616c616e63650000000000000000000000000000000000000000000000000000")
    assert _decode_revert_output(out) == "ERC20: transfer amount exceeds balance"


def test_is_method_unsupported():
    from qeth.chain import ChainError
    assert _is_method_unsupported(ChainError(-32601, "method not found"))
    assert _is_method_unsupported(Exception("HTTP Error 400: Bad Request"))
    assert not _is_method_unsupported(ChainError(15, "Too many request"))
    assert not _is_method_unsupported(Exception("Revert { output: 0x }"))


def test_orchestrator_uses_simv1_when_supported(monkeypatch):
    sim._SIMV1_SUPPORT.clear()
    monkeypatch.setattr(sim, "_simulate_via_rpc",
                        lambda *a, **k: [{"address": USDC}])
    monkeypatch.setattr(sim, "_simulate_via_fork",
                        lambda *a, **k: pytest.fail("should not fork"))
    out = simulate_logs(CHAIN, FROM, USDC, "0x", 0)
    assert out == [{"address": USDC}]
    assert sim._SIMV1_SUPPORT[CHAIN.rpc_url] is True   # learned + cached


def test_orchestrator_falls_back_when_simv1_unsupported(monkeypatch):
    sim._SIMV1_SUPPORT.clear()
    def boom(*a, **k):
        raise _SimV1Unsupported()
    monkeypatch.setattr(sim, "_simulate_via_rpc", boom)
    monkeypatch.setattr(sim, "_simulate_via_fork", lambda *a, **k: ["forked"])
    assert simulate_logs(CHAIN, FROM, USDC, "0x", 0) == ["forked"]
    assert sim._SIMV1_SUPPORT[CHAIN.rpc_url] is False   # remembered


def test_orchestrator_skips_simv1_once_known_unsupported(monkeypatch):
    sim._SIMV1_SUPPORT.clear()
    sim._SIMV1_SUPPORT[CHAIN.rpc_url] = False
    monkeypatch.setattr(sim, "_simulate_via_rpc",
                        lambda *a, **k: pytest.fail("should skip simulateV1"))
    monkeypatch.setattr(sim, "_simulate_via_fork", lambda *a, **k: ["forked"])
    assert simulate_logs(CHAIN, FROM, USDC, "0x", 0) == ["forked"]


def test_orchestrator_revert_is_definitive_no_fork(monkeypatch):
    # simulateV1 ran and the tx reverted (None) — that's the answer; we
    # must NOT then fork (it'd just revert again, wasting requests).
    sim._SIMV1_SUPPORT.clear()
    monkeypatch.setattr(sim, "_simulate_via_rpc", lambda *a, **k: None)
    monkeypatch.setattr(sim, "_simulate_via_fork",
                        lambda *a, **k: pytest.fail("revert must not fork"))
    assert simulate_logs(CHAIN, FROM, USDC, "0x", 0) is None
    assert sim._SIMV1_SUPPORT[CHAIN.rpc_url] is True


def test_simv1_rpc_raises_unsupported_on_minus_32601(monkeypatch):
    from qeth.chain import ChainError
    class _Client:
        def __init__(self, chain): pass
        def rpc(self, method, params):
            raise ChainError(-32601, "the method eth_simulateV1 does not exist")
    monkeypatch.setattr("qeth.chain.EthClient", _Client)
    with pytest.raises(_SimV1Unsupported):
        sim._simulate_via_rpc(CHAIN, FROM, USDC, "0x", 0, sleep=lambda d: None)


def test_simv1_rpc_returns_logs(monkeypatch):
    log = {"address": USDC, "topics": [TRANSFER], "data": "0x" + "00" * 31 + "07"}
    class _Client:
        def __init__(self, chain): pass
        def rpc(self, method, params):
            assert method == "eth_simulateV1"
            return [{"calls": [{"status": "0x1", "logs": [log]}]}]
    monkeypatch.setattr("qeth.chain.EthClient", _Client)
    out = sim._simulate_via_rpc(CHAIN, FROM, USDC, "0xa9059cbb", 0)
    assert len(out) == 1 and out[0]["data"].endswith("07")


def test_simulation_available(monkeypatch):
    sim._SIMV1_SUPPORT.clear()
    monkeypatch.setattr(sim, "pyrevm_available", lambda: True)
    assert simulation_available(CHAIN)                 # pyrevm present
    monkeypatch.setattr(sim, "pyrevm_available", lambda: False)
    assert simulation_available(CHAIN)                 # endpoint unprobed → try
    sim._SIMV1_SUPPORT[CHAIN.rpc_url] = False
    assert not simulation_available(CHAIN)             # neither route works
    sim._SIMV1_SUPPORT[CHAIN.rpc_url] = True
    assert simulation_available(CHAIN)                 # simulateV1 works
    sim._SIMV1_SUPPORT.clear()


def test_fork_deadline_stops_retries():
    # A rate-limited fork past its deadline must not start another attempt.
    import time as _t
    class _Limited:
        n = 0
        def __init__(self, fork_url=None): pass
        def message_call(self, **kw):
            _Limited.n += 1
            raise RuntimeError('JsonRpcError { code: 15, message: "Too many request" }')
    out = sim._simulate_via_fork(
        CHAIN, FROM, USDC, "0x", 0, evm_cls=_Limited,
        deadline=_t.monotonic() - 1, sleep=lambda d: None,
    )
    assert out is None and _Limited.n == 1   # no retry past the deadline


def test_budget_threads_a_deadline_into_the_fork(monkeypatch):
    seen = {}
    def fake_fork(*a, **k):
        seen["deadline"] = k.get("deadline")
        return ["ok"]
    monkeypatch.setattr(sim, "_simulate_via_fork", fake_fork)
    sim._SIMV1_SUPPORT[CHAIN.rpc_url] = False   # force fork
    simulate_logs(CHAIN, FROM, USDC, "0x", 0, budget_s=12.0)
    assert seen["deadline"] is not None        # a concrete monotonic deadline
    sim._SIMV1_SUPPORT.clear()
