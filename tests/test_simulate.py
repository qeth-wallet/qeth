"""Hermetic tests for qeth.simulate — the log-extraction logic.

The fork path injects a fake ``StateReader`` and runs the REAL py-evm
engine against it (pure Python, no network) — hand-rolled bytecodes
below stand in for contracts. The live fork-against-mainnet path is
exercised manually (it's slow + networked).
"""

from types import SimpleNamespace

from qeth.pyevm_fork import StateReader
from qeth.simulate import simulate_logs

CHAIN = SimpleNamespace(chain_id=1, rpc_url="https://rpc.example/eth")
FROM = "0x7a16ff8270133f063aab6c9977183d9e72835428"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# PUSH1 0xaa, PUSH1 0x20, PUSH1 0x00, LOG1, STOP — emit one log with
# topic 0xaa and 32 zero bytes of data.
LOG1_CODE = bytes.fromhex("60aa60206000a100")
# PUSH1 0, SLOAD, PUSH1 0, MSTORE, PUSH1 0x20, PUSH1 0, LOG0, STOP —
# log storage slot 0 as the data payload (proves SLOADs hit the reader).
SLOAD_LOG_CODE = bytes.fromhex("60005460005260206000a000")
# PUSH1 0, PUSH1 0, REVERT — revert with empty output.
REVERT_CODE = bytes.fromhex("60006000fd")


class _WorldReader(StateReader):
    """A tiny world: ``code``/``storage`` live at the call target, every
    other address is an EOA with ``balance`` wei. Records lookups."""

    def __init__(self, code=b"", storage=None, balance=10**20):
        self.code = code
        self.storage = storage or {}
        self.balance = balance
        self.calls: list = []

    def get_account(self, address):
        self.calls.append(("account", address.lower()))
        if address.lower() == USDC:
            return (0, 0, self.code)
        return (self.balance, 0, b"")

    def get_storage(self, address, slot):
        self.calls.append(("storage", address.lower(), slot))
        if address.lower() == USDC:
            return self.storage.get(slot, 0)
        return 0


def test_returns_decode_ready_log_dicts():
    world = _WorldReader(code=LOG1_CODE)
    logs = simulate_logs(CHAIN, FROM, USDC, "0xa9059cbb", 0,
                         fork_reader=world)
    assert len(logs) == 1
    lg = logs[0]
    assert lg["address"].lower() == USDC          # checksummed in output
    assert lg["address"] != USDC                  # i.e. NOT the lowercase
    assert lg["topics"] == ["0x" + "00" * 31 + "aa"]
    assert lg["data"] == "0x" + "00" * 32


def test_storage_reads_go_through_the_reader():
    world = _WorldReader(code=SLOAD_LOG_CODE, storage={0: 5})
    logs = simulate_logs(CHAIN, FROM, USDC, "0x", 0, fork_reader=world)
    assert logs and logs[0]["data"] == "0x" + "00" * 31 + "05"
    assert any(c[0] == "storage" and c[1] == USDC and c[2] == 0
               for c in world.calls)


def test_plain_value_transfer_has_no_logs():
    world = _WorldReader()       # target is a plain EOA
    logs = simulate_logs(CHAIN, FROM, USDC, "0x", 10**18, fork_reader=world)
    assert logs == []


def test_insufficient_funds_returns_none():
    world = _WorldReader(balance=1)   # sender can't cover the value
    assert simulate_logs(CHAIN, FROM, USDC, "0x", 10**18,
                         fork_reader=world) is None


def test_contract_creation_returns_none():
    assert simulate_logs(CHAIN, FROM, None, "0x", 0,
                         fork_reader=_WorldReader()) is None


def test_calldata_to_codeless_target_returns_note():
    """Calldata to an address with no code simulates as a clean no-op,
    but on chains with NATIVE system contracts (TAC 0x08xx) the node acts
    on it outside the EVM — an empty events list would falsely read as
    'this tx does nothing'. The fork path must return a SimulationNote."""
    from qeth.simulate import SimulationNote
    world = _WorldReader(code=b"")            # target has no bytecode
    out = simulate_logs(CHAIN, FROM, USDC, "0xb46a8d61ff", 0,
                        fork_reader=world)
    assert isinstance(out, SimulationNote)
    assert "no contract code" in out.text


def test_plain_send_to_codeless_target_is_not_a_note():
    # No calldata → a no-op preview is the truth, not a trap.
    world = _WorldReader(code=b"")
    assert simulate_logs(CHAIN, FROM, USDC, "0x", 10**18,
                         fork_reader=world) == []


def test_simulation_error_returns_none():
    class _Boom(StateReader):
        def get_account(self, address): raise RuntimeError("reader exploded")
        def get_storage(self, address, slot): raise RuntimeError("boom")
    assert simulate_logs(CHAIN, FROM, USDC, "0x", 0,
                         fork_reader=_Boom()) is None


def test_revert_returns_none_without_retrying():
    # A genuine revert must fail fast — no backoff, single attempt.
    world = _WorldReader(code=REVERT_CODE)
    delays: list = []
    assert simulate_logs(CHAIN, FROM, USDC, "0x", 0, fork_reader=world,
                         sleep=delays.append) is None
    assert delays == []


def test_engine_invokes_reader_prefetch():
    """run_fork_call must offer the reader its warm-up hint exactly once,
    with the call's own params, before execution."""
    calls = []

    class _Recorder(_WorldReader):
        def prefetch(self, **kw):
            calls.append(kw)

    world = _Recorder(code=LOG1_CODE)
    simulate_logs(CHAIN, FROM, USDC, "0xa9059cbb", 7, fork_reader=world)
    assert len(calls) == 1
    assert calls[0]["to_addr"] == USDC and calls[0]["value"] == 7


def test_rpc_reader_prefetch_seeds_the_memo(monkeypatch):
    """The batched prefetch must seed EXACTLY the memo keys the lazy
    getters use — a key-format mismatch would silently turn the prefetch
    into dead weight (everything re-fetched serially)."""
    import json
    from io import BytesIO
    from qeth.pyevm_fork import RpcStateReader

    TOKEN = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    # web3 wraps results in AttributeDict (a Mapping, NOT a dict
    # subclass) — the prefetch must duck-type, or it silently discards
    # the whole hint (the live bug this test pins).
    from web3.datastructures import AttributeDict
    hint = AttributeDict({"accessList": [
        AttributeDict({"address": TOKEN.lower(),
                       "storageKeys": ["0x" + "00" * 31 + "05"]}),
    ], "gasUsed": "0x5208"})

    class _Client:
        calls: list = []
        def __init__(self, chain): pass
        def rpc(self, method, params):
            _Client.calls.append(method)
            assert method == "eth_createAccessList"
            return hint

    batch_seen = {}

    def fake_urlopen(req, timeout=None):
        payload = json.loads(req.data)
        batch_seen["n"] = len(payload)
        out = []
        for item in payload:
            m = item["method"]
            res = ("0x2386f26fc10000" if m == "eth_getBalance"
                   else "0x1" if m == "eth_getTransactionCount"
                   else "0x6001" if m == "eth_getCode"
                   else "0x" + "00" * 31 + "2a")
            out.append({"jsonrpc": "2.0", "id": item["id"], "result": res})

        class _R:
            def read(self): return json.dumps(out).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()

    monkeypatch.setattr("qeth.chain.EthClient", _Client)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    reader = RpcStateReader(
        SimpleNamespace(chain_id=1, rpc_url="https://rpc.example"), "0x10")
    reader.prefetch(from_addr=FROM, to_addr=TOKEN, data="0xa9059cbb", value=0)
    # sender (3) + token (3) account calls + 1 slot = 7 batched requests
    assert batch_seen["n"] == 7

    _Client.calls.clear()
    bal, nonce, code = reader.get_account(TOKEN)
    assert (bal, nonce, code) == (10**16, 1, bytes.fromhex("6001"))
    assert reader.get_storage(TOKEN, 5) == 42
    assert reader.get_account(FROM)[0] == 10**16
    assert _Client.calls == []      # everything came from the seeded memo


def test_rpc_reader_prefetch_failure_is_silent(monkeypatch):
    from qeth.pyevm_fork import RpcStateReader

    class _NoAccessList:
        def __init__(self, chain): pass
        def rpc(self, method, params):
            if method == "eth_createAccessList":
                raise RuntimeError("-32601 method not found")
            return "0x6001" if method == "eth_getCode" else "0x1"

    monkeypatch.setattr("qeth.chain.EthClient", _NoAccessList)
    reader = RpcStateReader(
        SimpleNamespace(chain_id=1, rpc_url="https://rpc.example"), "0x10")
    reader.prefetch(from_addr=FROM, to_addr=USDC, data="0xdead", value=0)
    # …and the lazy path still works afterwards.
    assert reader.get_account(USDC)[1] == 1


# --- revert-reason decoding ---------------------------------------------------

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


def test_simulation_revert_carries_decodable_output():
    # The engine's revert exception → the decoded reason, end to end.
    from qeth.pyevm_fork import SimulationRevert
    from qeth.simulate import _decode_revert_output
    e = SimulationRevert(bytes.fromhex(
        "08c379a0"
        "0000000000000000000000000000000000000000000000000000000000000020"
        "0000000000000000000000000000000000000000000000000000000000000003"
        "6e6f700000000000000000000000000000000000000000000000000000000000"
    ), error=None)
    assert _decode_revert_output("0x" + e.output.hex()) == "nop"


# --- retry / rate-limit handling ----------------------------------------------

class _FlakyReader(_WorldReader):
    """Raises a rate-limit error from the first ``fail`` reader touches —
    each raise aborts exactly one run_fork_call attempt, so ``failures``
    counts failed attempts."""

    def __init__(self, fail, **kwargs):
        super().__init__(**kwargs)
        self.failures = 0
        self._fail = fail

    def get_account(self, address):
        if self.failures < self._fail:
            self.failures += 1
            raise RuntimeError(
                'JsonRpcError { code: 15, message: "Too many request" }')
        return super().get_account(address)


def test_rate_limited_retries_then_succeeds():
    world = _FlakyReader(fail=2, code=LOG1_CODE)
    delays: list = []
    logs = simulate_logs(CHAIN, FROM, USDC, "0x", 0, fork_reader=world,
                         sleep=delays.append)
    assert world.failures == 2         # two failed attempts, then success
    assert len(delays) == 2            # backed off before each retry
    assert logs and len(logs) == 1


def test_rate_limited_gives_up_after_retries():
    world = _FlakyReader(fail=99)
    logs = simulate_logs(CHAIN, FROM, USDC, "0x", 0, fork_reader=world,
                         retries=3, sleep=lambda d: None)
    assert logs is None
    assert world.failures == 3


def test_fork_deadline_stops_retries():
    # A rate-limited fork past its deadline must not start another attempt.
    import time as _t
    import qeth.simulate as sim
    world = _FlakyReader(fail=99)
    out = sim._simulate_via_fork(
        CHAIN, FROM, USDC, "0x", 0, fork_reader=world,
        deadline=_t.monotonic() - 1, sleep=lambda d: None,
    )
    assert out is None and world.failures == 1   # no retry past the deadline


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


def test_simv1_empty_logs_to_codeless_target_returns_note(monkeypatch):
    """Same trap through the FAST path: simulateV1 says 'success, no
    logs' for calldata to a code-less address — one follow-up getCode
    turns that into the honest note."""
    from qeth.simulate import SimulationNote
    class _Client:
        def __init__(self, chain): pass
        def rpc(self, method, params):
            if method == "eth_simulateV1":
                return [{"calls": [{"status": "0x1", "logs": []}]}]
            assert method == "eth_getCode"
            return "0x"
    monkeypatch.setattr("qeth.chain.EthClient", _Client)
    out = sim._simulate_via_rpc(CHAIN, FROM, USDC, "0xb46a8d61ff", 0)
    assert isinstance(out, SimulationNote)


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
    monkeypatch.setattr(sim, "fork_available", lambda: True)
    assert simulation_available(CHAIN)                 # fork engine present
    monkeypatch.setattr(sim, "fork_available", lambda: False)
    assert simulation_available(CHAIN)                 # endpoint unprobed → try
    sim._SIMV1_SUPPORT[CHAIN.rpc_url] = False
    assert not simulation_available(CHAIN)             # neither route works
    sim._SIMV1_SUPPORT[CHAIN.rpc_url] = True
    assert simulation_available(CHAIN)                 # simulateV1 works
    sim._SIMV1_SUPPORT.clear()


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
