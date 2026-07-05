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


def test_revert_returns_a_note_without_retrying():
    # A genuine revert must fail fast — no backoff, single attempt — and come
    # back as a RevertNote (definitive; the UI warns) rather than None.
    from qeth.simulate import RevertNote
    world = _WorldReader(code=REVERT_CODE)
    delays: list = []
    out = simulate_logs(CHAIN, FROM, USDC, "0x", 0, fork_reader=world,
                        sleep=delays.append)
    assert isinstance(out, RevertNote)
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


def _hint_urlopen(handler):
    """Build a fake urllib.urlopen that routes each JSON-RPC POST through
    ``handler(method, params) -> result`` and records the methods seen."""
    import json
    seen = []

    def fake(req, timeout=None):
        item = json.loads(req.data)
        seen.append(item["method"])
        out = {"jsonrpc": "2.0", "id": item["id"],
               "result": handler(item["method"], item["params"])}

        class _R:
            def read(self): return json.dumps(out).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()

    return fake, seen


def test_rpc_reader_prefetch_seeds_the_memo(monkeypatch):
    """prestateTracer hint → concurrent seed → memo, with EXACTLY the key
    formats the lazy getters use (a mismatch turns the prefetch into dead
    weight). The hint and the seed both go to _hint_url / the reader's
    RPC via urllib; EthClient is only the lazy read fallback."""
    from qeth.pyevm_fork import RpcStateReader

    TOKEN = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    SLOT = "0x" + "00" * 31 + "05"

    def handler(method, params):
        if method == "debug_traceCall":          # prestateTracer hint
            return {TOKEN.lower(): {"balance": "0x0", "storage": {SLOT: "0x2a"}}}
        return ("0x2386f26fc10000" if method == "eth_getBalance"
                else "0x1" if method == "eth_getTransactionCount"
                else "0x6001" if method == "eth_getCode"
                else "0x" + "00" * 31 + "2a")   # eth_getStorageAt

    fake, seen = _hint_urlopen(handler)

    class _Client:
        calls: list = []
        def __init__(self, chain): pass
        def rpc(self, method, params):
            _Client.calls.append(method)   # lazy read fallback only
            return "0x0"

    monkeypatch.setattr("qeth.chain.EthClient", _Client)
    monkeypatch.setattr("urllib.request.urlopen", fake)

    reader = RpcStateReader(
        SimpleNamespace(chain_id=1, rpc_url="https://rpc.example"), "0x10")
    reader.prefetch(from_addr=FROM, to_addr=TOKEN, data="0xa9059cbb", value=0)
    assert seen[0] == "debug_traceCall"          # prestate hint went first
    # sender (3) + token (3) account calls + 1 slot = 7 seed fetches
    assert sum(1 for m in seen if m != "debug_traceCall") == 7

    _Client.calls.clear()
    bal, nonce, code = reader.get_account(TOKEN)
    assert (bal, nonce, code) == (10**16, 1, bytes.fromhex("6001"))
    assert reader.get_storage(TOKEN, 5) == 42
    assert reader.get_account(FROM)[0] == 10**16
    assert _Client.calls == []      # everything came from the seeded memo


def test_prefetch_falls_back_to_access_list(monkeypatch):
    """When the upstream lacks debug_traceCall, the hint falls back to
    eth_createAccessList (and still seeds the memo)."""
    from qeth.pyevm_fork import RpcStateReader

    TOKEN = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

    def handler(method, params):
        if method == "debug_traceCall":
            raise RuntimeError("the method debug_traceCall does not exist")
        if method == "eth_createAccessList":
            return {"accessList": [{"address": TOKEN.lower(),
                                    "storageKeys": []}]}
        return "0x1" if method == "eth_getTransactionCount" else "0x0"

    import json
    seen = []

    def fake(req, timeout=None):
        item = json.loads(req.data)
        seen.append(item["method"])
        try:
            res = handler(item["method"], item["params"])
        except RuntimeError as e:
            out = {"jsonrpc": "2.0", "id": item["id"],
                   "error": {"code": -32601, "message": str(e)}}
        else:
            out = {"jsonrpc": "2.0", "id": item["id"], "result": res}

        class _R:
            def read(self): return json.dumps(out).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()

    class _Client:
        def __init__(self, chain): pass
        def rpc(self, method, params): return "0x0"

    monkeypatch.setattr("qeth.chain.EthClient", _Client)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    reader = RpcStateReader(
        SimpleNamespace(chain_id=1, rpc_url="https://rpc.example"), "0x10")
    reader.prefetch(from_addr=FROM, to_addr=TOKEN, data="0xa9059cbb", value=0)
    assert "debug_traceCall" in seen and "eth_createAccessList" in seen


def test_static_accounts_never_hit_the_network():
    """Precompiles and the Cancun/Prague system contracts answer
    (0, 0, b'') locally — py-evm overwrites system-contract code at
    State init and executes precompiles natively, so fetching their
    account fields was pure waste (profiled: ~9 round-trips, ~2s of
    every warm simulation)."""
    from qeth.pyevm_fork import RpcStateReader

    class _Exploding:
        def __init__(self, chain): pass
        def rpc(self, method, params):
            raise AssertionError("static account hit the network")

    import unittest.mock as mock
    with mock.patch("qeth.chain.EthClient", _Exploding):
        reader = RpcStateReader(
            SimpleNamespace(chain_id=1, rpc_url="https://rpc.example"), "0x1")
    for addr in ("0x" + "00" * 19 + "04",                       # identity
                 "0x000F3df6D732807Ef1319fB7B8bB8522d0Beac02",  # 4788
                 "0x0000F90827F1C53a10cb7A02335B175320002935"): # 2935
        assert reader.get_account(addr) == (0, 0, b"")


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
    # A reverted call → a RevertNote carrying the reason (definitive; the UI
    # warns in red), not None and not a fall-back trigger.
    from qeth.simulate import RevertNote
    reverted = [{"calls": [{"status": "0x0", "error": {"message": "boom"},
                            "logs": []}]}]
    note = _logs_from_simv1(reverted)
    assert isinstance(note, RevertNote) and note.reason == "boom"
    assert not note.verified


def test_simv1_revert_decodes_error_string_into_the_note():
    """The Error(string) envelope in returnData becomes the human reason."""
    from qeth.simulate import RevertNote
    err = ("0x08c379a0"
           "0000000000000000000000000000000000000000000000000000000000000020"
           "0000000000000000000000000000000000000000000000000000000000000026"
           "45524332303a207472616e7366657220616d6f756e7420657863656564732062"
           "616c616e63650000000000000000000000000000000000000000000000000000")
    reverted = [{"calls": [{"status": "0x0", "returnData": err, "logs": []}]}]
    note = _logs_from_simv1(reverted)
    assert isinstance(note, RevertNote)
    assert note.reason == "ERC20: transfer amount exceeds balance"


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
    # simulateV1 ran and the tx reverted — that's the answer; we must NOT
    # then fork (it'd just revert again, wasting requests). The RevertNote
    # is forwarded so the UI can warn.
    from qeth.simulate import RevertNote
    sim._SIMV1_SUPPORT.clear()
    monkeypatch.setattr(sim, "_simulate_via_rpc",
                        lambda *a, **k: RevertNote("nope"))
    monkeypatch.setattr(sim, "_simulate_via_fork",
                        lambda *a, **k: pytest.fail("revert must not fork"))
    out = simulate_logs(CHAIN, FROM, USDC, "0x", 0)
    assert isinstance(out, RevertNote) and out.reason == "nope"
    assert sim._SIMV1_SUPPORT[CHAIN.rpc_url] is True


def test_verified_revert_is_flagged(monkeypatch):
    """A revert proven over Helios state comes back as a RevertNote with
    verified=True so the warning can say the RPC couldn't have faked it."""
    import qeth.helios as helios_mod
    from qeth.simulate import RevertNote
    monkeypatch.setattr(sim, "fork_available", lambda: True)
    monkeypatch.setattr(helios_mod, "verified_chain", lambda chain: chain)
    monkeypatch.setattr(sim, "_simulate_via_fork",
                        lambda *a, **k: RevertNote("boom"))
    out = simulate_logs(CHAIN, FROM, USDC, "0x1234", 0)
    assert isinstance(out, RevertNote) and out.reason == "boom"
    assert out.verified is True


def test_verified_proof_window_reroutes_and_retries(monkeypatch):
    """A verified fork that fails because Helios's execution-rpc can't serve
    historical proofs (a send-only RPC like mevblocker) reroutes Helios and
    retries — the proof-capable second attempt's logs come back tagged verified,
    not blank."""
    import qeth.helios as helios_mod
    from qeth.simulate import VerifiedLogs, _ProofWindowUnavailable
    monkeypatch.setattr(sim, "fork_available", lambda: True)
    monkeypatch.setattr(helios_mod, "verified_chain", lambda chain: chain)
    noted = []
    monkeypatch.setattr(helios_mod, "note_execution_rpc_incapable",
                        lambda chain: noted.append(chain) or True)
    calls = {"n": 0}

    def fork(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:      # first attempt: mevblocker can't prove the block
            raise _ProofWindowUnavailable(
                "distance to target block exceeds maximum proof window")
        return ["evt"]           # retry against the proof-capable endpoint
    monkeypatch.setattr(sim, "_simulate_via_fork", fork)

    out = simulate_logs(CHAIN, FROM, USDC, "0x1234", 0)
    assert isinstance(out, VerifiedLogs) and list(out) == ["evt"]
    assert calls["n"] == 2 and noted    # retried once, after recording incapable


def test_verified_proof_window_falls_through_to_unverified(monkeypatch):
    """If the reroute retry still can't prove (or there's nowhere to reroute),
    the user still gets a preview: fall through to the unverified fast path,
    tagged RemoteLogs so the UI badges it rather than showing a blank."""
    import qeth.helios as helios_mod
    from qeth.simulate import RemoteLogs, _ProofWindowUnavailable
    monkeypatch.setattr(sim, "fork_available", lambda: True)
    monkeypatch.setattr(helios_mod, "verified_chain", lambda chain: chain)
    monkeypatch.setattr(helios_mod, "note_execution_rpc_incapable",
                        lambda chain: False)   # no bundled fallback to switch to
    monkeypatch.setattr(sim, "_simulate_via_fork",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _ProofWindowUnavailable("proof window")))
    # the unverified fast path works (mevblocker supports eth_simulateV1)
    monkeypatch.setattr(sim, "_simulate_via_rpc", lambda *a, **k: ["remote-evt"])

    out = simulate_logs(CHAIN, FROM, USDC, "0x1234", 0)
    assert isinstance(out, RemoteLogs) and list(out) == ["remote-evt"]


def test_floor_ahead_of_head(monkeypatch):
    """The helper that detects when the wallet's just-confirmed tx is at a
    block the (Helios) head hasn't reached yet — the window where a verified
    fork would miss it. The pending-tx sentinel must be excluded."""
    from qeth.simulate import _REAL_BLOCK_CEILING, _floor_ahead_of_head

    class _Client:
        def __init__(self, chain): pass
        def rpc(self, method, params): return hex(100)   # head = block 100
    monkeypatch.setattr("qeth.chain.EthClient", _Client)

    assert _floor_ahead_of_head(CHAIN, None) is False               # no floor
    assert _floor_ahead_of_head(CHAIN, _REAL_BLOCK_CEILING) is False  # pending sentinel
    assert _floor_ahead_of_head(CHAIN, 105) is True                 # tx ahead of head
    assert _floor_ahead_of_head(CHAIN, 100) is False                # tx at head
    assert _floor_ahead_of_head(CHAIN, 90) is False                 # tx behind head


def test_await_floor_waits_until_head_reaches_the_floor(monkeypatch):
    """The unverified preview waits for the load-balanced head to import the
    wallet's latest confirmed tx before forking at 'latest', so an approve fired
    just before its swap isn't missed (false revert). Stops the instant the head
    catches up."""
    from qeth.simulate import _HEAD_POLL_S, _await_floor
    heads = iter([98, 99, 100, 101])    # LB node imports our block on the 3rd poll
    polls: list = []

    class _Client:
        def __init__(self, chain): pass
        def rpc(self, method, params):
            polls.append(method)
            return hex(next(heads))
    monkeypatch.setattr("qeth.chain.EthClient", _Client)
    slept: list = []
    _await_floor(CHAIN, 100, deadline=None, sleep=slept.append)
    assert polls == ["eth_blockNumber"] * 3         # polled until head >= 100
    assert slept == [_HEAD_POLL_S, _HEAD_POLL_S]     # waited between, not after


def test_await_floor_is_bounded_when_head_never_catches_up(monkeypatch):
    """A node that never imports our block must not hang the preview — the wait
    gives up at the cap and lets the (possibly stale) sim proceed."""
    from qeth import simulate
    monkeypatch.setattr(simulate, "_HEAD_CATCHUP_CAP_S", 0.0)

    class _Client:
        def __init__(self, chain): pass
        def rpc(self, method, params): return hex(50)   # forever behind floor 100
    monkeypatch.setattr("qeth.chain.EthClient", _Client)
    slept: list = []
    simulate._await_floor(CHAIN, 100, deadline=None, sleep=slept.append)
    assert slept == []                               # capped out, never slept


def test_await_floor_noop_for_sentinel_and_unknown(monkeypatch):
    """No floor (None) or the pending-tx sentinel means 'fork at head' already —
    nothing to wait for, and we don't even hit the network."""
    from qeth.simulate import _REAL_BLOCK_CEILING, _await_floor
    called: list = []

    class _Client:
        def __init__(self, chain): pass
        def rpc(self, method, params): called.append(method); return hex(1)
    monkeypatch.setattr("qeth.chain.EthClient", _Client)
    _await_floor(CHAIN, None, deadline=None, sleep=lambda s: None)
    _await_floor(CHAIN, _REAL_BLOCK_CEILING, deadline=None, sleep=lambda s: None)
    assert called == []


def test_verified_skipped_when_helios_behind_floor(monkeypatch):
    """Right after a tx confirms, the Helios head can still be behind it. The
    verified fork (capped at that head) would run before the tx and miss its
    state, so the orchestrator falls through to the unverified sim until Helios
    catches up — a correct preview beats a wrong verified one."""
    import qeth.helios as helios_mod
    monkeypatch.setattr(sim, "_SIMV1_SUPPORT", {})
    monkeypatch.setattr(sim, "fork_available", lambda: True)
    monkeypatch.setattr(helios_mod, "verified_chain", lambda chain: chain)
    # This one mock stands in for BOTH the (lagging) Helios head and the
    # execution head that _await_floor polls; in production they're different
    # chains (execution is ahead of the floor). Pin the catch-up cap to 0 so the
    # conflation doesn't make _await_floor wait out its real-time budget here.
    monkeypatch.setattr(sim, "_HEAD_CATCHUP_CAP_S", 0.0)

    class _Client:
        def __init__(self, chain): pass
        def rpc(self, method, params): return hex(100)   # helios head = 100
    monkeypatch.setattr("qeth.chain.EthClient", _Client)

    used = []
    monkeypatch.setattr(sim, "_simulate_via_fork",
                        lambda *a, **k: used.append("fork") or ["verified"])
    monkeypatch.setattr(sim, "_simulate_via_rpc",
                        lambda *a, **k: used.append("rpc") or ["unverified"])

    # Wallet's latest confirmed tx is at block 105 — ahead of the helios head.
    out = simulate_logs(CHAIN, FROM, USDC, "0x1234", 0, floor_block=105)
    assert used == ["rpc"]          # unverified path taken, verified fork skipped
    assert out == ["unverified"]
    # ...and tagged RemoteLogs so the UI badges it 'verifying…' (Helios behind,
    # not a plain unverified chain).
    assert isinstance(out, sim.RemoteLogs)


def test_unverified_without_helios_is_not_tagged_remote(monkeypatch):
    """No Helios on this chain → a plain unverified simV1 preview, NOT the
    'verifying…' remote tag (which is only for a Helios that's merely behind
    the floor). Otherwise every unverified chain would falsely claim to be
    verifying."""
    import qeth.helios as helios_mod
    from qeth.simulate import RemoteLogs
    monkeypatch.setattr(sim, "_SIMV1_SUPPORT", {})
    monkeypatch.setattr(sim, "fork_available", lambda: True)
    monkeypatch.setattr(helios_mod, "verified_chain", lambda chain: None)
    monkeypatch.setattr(sim, "_simulate_via_rpc", lambda *a, **k: ["plain"])
    out = simulate_logs(CHAIN, FROM, USDC, "0x1234", 0, floor_block=105)
    assert out == ["plain"]
    assert not isinstance(out, RemoteLogs)


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


# --- verified-fork block backoff (dodges the head-boundary proof mismatch) ---

class _RecordingEthClient:
    """Stands in for EthClient: serves eth_blockNumber and a block dict,
    recording which block tag _latest_block asked eth_getBlockByNumber for."""
    head = 1000

    def __init__(self, chain):
        self.requested_tag = None

    def rpc(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            self.requested_tag = params[0]
            num = self.head if params[0] == "latest" else int(params[0], 16)
            return {"number": hex(num), "timestamp": "0x100",
                    "baseFeePerGas": "0x7", "gasLimit": "0x1c9c380",
                    "miner": "0x" + "11" * 20}
        raise AssertionError(f"unexpected rpc {method}")


def _patch_client(monkeypatch):
    import qeth.chain as chain_mod
    holder = {}
    def factory(chain):
        holder["client"] = _RecordingEthClient(chain)
        return holder["client"]
    monkeypatch.setattr(chain_mod, "EthClient", factory)
    return holder


def test_latest_block_no_lag_uses_latest(monkeypatch):
    holder = _patch_client(monkeypatch)
    blk = sim._latest_block(CHAIN)              # lag defaults to 0
    assert blk["number"] == 1000
    assert holder["client"].requested_tag == "latest"


def test_latest_block_forks_lag_blocks_behind_head(monkeypatch):
    holder = _patch_client(monkeypatch)
    blk = sim._latest_block(CHAIN, lag=5)
    assert blk["number"] == 995               # 1000 - 5
    assert holder["client"].requested_tag == hex(995)


def test_floor_clamps_the_backoff_forward_to_include_our_tx(monkeypatch):
    """If the wallet's last tx is newer than (head - lag), fork at that tx's
    block instead — otherwise the preview would miss our own approval."""
    holder = _patch_client(monkeypatch)
    # head 1000, lag 5 would fork at 995, but our last tx is at 998.
    blk = sim._latest_block(CHAIN, lag=5, floor_block=998)
    assert blk["number"] == 998
    assert holder["client"].requested_tag == hex(998)


def test_floor_below_the_backoff_target_is_a_noop(monkeypatch):
    """An old last-tx doesn't pull the fork forward — the full lag margin is
    kept for proof convergence."""
    holder = _patch_client(monkeypatch)
    blk = sim._latest_block(CHAIN, lag=5, floor_block=990)
    assert blk["number"] == 995               # head - lag wins
    assert holder["client"].requested_tag == hex(995)


def test_floor_never_forks_past_the_head(monkeypatch):
    """A floor at/above the head clamps to the head, not beyond — forking at
    the head is the unavoidable edge when our tx is the newest block."""
    holder = _patch_client(monkeypatch)
    blk = sim._latest_block(CHAIN, lag=5, floor_block=10_000)
    assert blk["number"] == 1000              # min(floor, head)
    assert holder["client"].requested_tag == hex(1000)


def test_latest_block_lag_below_genesis_falls_back_to_latest(monkeypatch):
    """A lag deeper than the chain's height (a brand-new testnet) must not
    request a negative block — fall back to latest."""
    holder = _patch_client(monkeypatch)
    _RecordingEthClient.head = 3
    try:
        sim._latest_block(CHAIN, lag=10)
        assert holder["client"].requested_tag == "latest"
    finally:
        _RecordingEthClient.head = 1000


def test_verified_branch_passes_per_chain_lag_to_the_fork(monkeypatch):
    """The verified simulation path forks behind the head by the chain's
    configured lag (mainnet here) — the actual fix for the -32000 proof
    mismatch at the bleeding edge."""
    seen = {}
    def fake_fork(*a, **k):
        seen["fork_lag"] = k.get("fork_lag")
        return ["ok"]
    import qeth.helios as helios_mod
    monkeypatch.setattr(sim, "_simulate_via_fork", fake_fork)
    monkeypatch.setattr(sim, "fork_available", lambda: True)
    # verified_chain is imported locally inside simulate_logs (from .helios),
    # so patch it at the source module, not on sim.
    monkeypatch.setattr(helios_mod, "verified_chain", lambda chain: chain)
    simulate_logs(CHAIN, FROM, USDC, "0x1234", 0)
    assert seen["fork_lag"] == sim._VERIFIED_FORK_LAG[1]
