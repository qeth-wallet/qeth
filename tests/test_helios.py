"""Hermetic tests for the Helios verified-simulation sidecar.

No real helios process is ever spawned (conftest sets QETH_HELIOS=0 for
the whole suite; these tests inject fakes and opt back in per-test).
"""


import pytest

import qeth.helios as hl
from qeth.chains import Chain


ETH = Chain("Ethereum", 1, "https://eth.example")


@pytest.fixture(autouse=True)
def _clean_registry():
    hl._sidecars.clear()
    yield
    hl._sidecars.clear()


def _enable(monkeypatch, binary="/fake/helios"):
    monkeypatch.setenv("QETH_HELIOS", "1")
    monkeypatch.setattr(hl.shutil, "which", lambda name: binary)


class _FakeProc:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.returncode = None
        self.stdout = None          # no output pipe → sidecar skips the pump thread

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


# --- discovery ----------------------------------------------------------------

def test_disabled_via_env(monkeypatch):
    monkeypatch.setenv("QETH_HELIOS", "0")
    assert hl.helios_binary() is None


def test_found_on_path(monkeypatch):
    _enable(monkeypatch, "/usr/bin/helios")
    assert hl.helios_binary() == "/usr/bin/helios"


def test_heliosup_fallback_when_not_on_path(monkeypatch):
    monkeypatch.setenv("QETH_HELIOS", "1")
    monkeypatch.delenv("QETH_HELIOS_BIN", raising=False)
    monkeypatch.setattr(hl.shutil, "which", lambda name: None)
    monkeypatch.setattr(hl.os, "access", lambda p, m: p == hl._HELIOSUP_BIN)
    assert hl.helios_binary() == hl._HELIOSUP_BIN


def test_explicit_bin_env_wins(monkeypatch):
    """QETH_HELIOS_BIN (set by the verify-variant launchers) takes
    precedence over PATH — and over a sandbox where PATH has nothing."""
    monkeypatch.setenv("QETH_HELIOS", "1")
    monkeypatch.setenv("QETH_HELIOS_BIN", "/app/bin/helios")
    monkeypatch.setattr(hl.os, "access",
                        lambda p, m: p == "/app/bin/helios")
    monkeypatch.setattr(hl.shutil, "which", lambda name: "/usr/bin/helios")
    assert hl.helios_binary() == "/app/bin/helios"


def test_explicit_bin_missing_falls_through(monkeypatch):
    # A stale/empty QETH_HELIOS_BIN must not shadow a real PATH helios.
    monkeypatch.setenv("QETH_HELIOS", "1")
    monkeypatch.setenv("QETH_HELIOS_BIN", "/nope/helios")
    monkeypatch.setattr(hl.os, "access", lambda p, m: False)
    monkeypatch.setattr(hl.shutil, "which", lambda name: "/usr/bin/helios")
    assert hl.helios_binary() == "/usr/bin/helios"


def test_network_map_covers_helios_chains_only():
    assert set(hl.HELIOS_NETWORKS) == {1, 10, 8453, 59144}
    # The unsupported majority must stay on the direct path.
    for cid in (137, 42161, 100, 56, 239, 324):
        assert cid not in hl.HELIOS_NETWORKS


# --- sidecar readiness gate ----------------------------------------------------

def test_wait_ready_polls_until_synced(monkeypatch):
    answers = iter([Exception("conn refused"), {"startingBlock": "0x0"}, False])

    def fake_rpc(url, method, params=None, timeout=5.0):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(hl, "_rpc", fake_rpc)
    sc = hl.HeliosSidecar(ETH, "/fake/helios", popen=_FakeProc)
    assert sc.wait_ready(timeout=10, sleep=lambda s: None) is True
    assert sc.wait_ready() is True            # sticky, no re-poll


def test_wait_ready_fails_when_process_dies(monkeypatch):
    monkeypatch.setattr(hl, "_rpc",
                        lambda *a, **k: {"startingBlock": "0x0"})
    sc = hl.HeliosSidecar(ETH, "/fake/helios", popen=_FakeProc)
    sc._proc.returncode = 1                   # died
    assert sc.wait_ready(timeout=10, sleep=lambda s: None) is False


def test_wait_ready_times_out(monkeypatch):
    monkeypatch.setattr(hl, "_rpc",
                        lambda *a, **k: {"startingBlock": "0x0"})
    sc = hl.HeliosSidecar(ETH, "/fake/helios", popen=_FakeProc)
    t = iter(range(100))
    assert sc.wait_ready(timeout=3, sleep=lambda s: None,
                         clock=lambda: next(t)) is False


def test_output_captured_ansi_stripped_and_dumped_on_timeout(monkeypatch, caplog):
    # Helios logs its sync progress + failures to STDOUT; the sidecar captures it
    # (ANSI stripped) into a tail so a sidecar that never reaches ready is
    # diagnosable — the tail is dumped when a real readiness wait times out.
    import io
    import logging
    monkeypatch.setattr(hl, "_rpc",
                        lambda *a, **k: {"startingBlock": "0x0"})
    sc = hl.HeliosSidecar(ETH, "/fake/helios", popen=_FakeProc)
    sc._pump_output(io.StringIO(
        "\x1b[32m INFO\x1b[0m helios::client: latest block number=1\n"
        "ERROR helios::consensus: checkpoint unavailable\n"))
    assert any("latest block" in ln for ln in sc._log_tail)
    assert not any("\x1b" in ln for ln in sc._log_tail)        # ANSI stripped
    with caplog.at_level(logging.WARNING, logger="qeth.helios"):
        t = iter(range(100))
        assert sc.wait_ready(timeout=3, sleep=lambda s: None,
                             clock=lambda: next(t)) is False
    assert "checkpoint unavailable" in caplog.text             # tail surfaced


def test_spawn_argv_shape():
    sc = hl.HeliosSidecar(
        Chain("Base", 8453, "https://base.example"), "/fake/helios",
        popen=_FakeProc)
    argv = sc._proc.argv
    assert argv[0:2] == ["/fake/helios", "opstack"]
    assert argv[2:4] == ["--network", "base"]
    assert "--ethereum-load-external-fallback" in argv  # self-heal stale checkpoint
    assert "--execution-rpc" in argv
    assert argv[argv.index("--execution-rpc") + 1] == "https://base.example"
    assert "--rpc-port" in argv


def test_spawn_argv_includes_checkpoint_fallback_per_module():
    """Every Helios module that CAN self-heal a stale weak-subjectivity
    checkpoint gets the (module-specific) external-fallback flag; the one that
    can't (linea) gets none. Without it an aged-out checkpoint returns '404 LC
    bootstrap unavailable' from the consensus RPC and the sidecar never syncs."""
    cases = {
        1:     "--load-external-fallback",           # ethereum
        8453:  "--ethereum-load-external-fallback",  # opstack
        59144: None,                                 # linea: no such flag
    }
    for chain_id, flag in cases.items():
        sc = hl.HeliosSidecar(
            Chain("C", chain_id, "https://rpc.example"), "/fake/helios",
            popen=_FakeProc)
        argv = sc._proc.argv
        if flag is None:
            assert not any("external-fallback" in a for a in argv), argv
        else:
            assert flag in argv, argv


# --- verified_chain ------------------------------------------------------------

def test_unsupported_chain_returns_none(monkeypatch):
    _enable(monkeypatch)
    tac = Chain("TAC", 239, "https://rpc.tac.build")
    assert hl.verified_chain(tac) is None


def test_no_binary_returns_none(monkeypatch):
    monkeypatch.setenv("QETH_HELIOS", "0")
    assert hl.verified_chain(ETH) is None


def test_verified_chain_shadow_reads_helios_only(monkeypatch):
    """THE trust property: the shadow chain's resolved RPC list must be
    exactly the sidecar URL. An empty fallback tuple would make
    _rpc_urls inherit DEFAULT_CHAINS' public fallbacks for chain 1 —
    silently failing verified reads over to unverified endpoints."""
    _enable(monkeypatch)
    monkeypatch.setattr(hl, "_rpc", lambda *a, **k: False)   # synced
    monkeypatch.setattr(hl.subprocess, "Popen", _FakeProc)
    shadow = hl.verified_chain(ETH, wait_s=5)
    assert shadow is not None
    assert shadow.chain_id == 1
    assert shadow.rpc_url.startswith("http://127.0.0.1:")
    from qeth.chain import _rpc_urls
    assert _rpc_urls(shadow) == [shadow.rpc_url]


def test_sidecar_is_reused_across_calls(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(hl, "_rpc", lambda *a, **k: False)
    spawned = []

    def popen(argv, **kw):
        spawned.append(argv)
        return _FakeProc(argv)

    monkeypatch.setattr(hl.subprocess, "Popen", popen)
    a = hl.verified_chain(ETH, wait_s=5)
    b = hl.verified_chain(ETH, wait_s=5)
    assert len(spawned) == 1
    assert a.rpc_url == b.rpc_url


def test_dead_sidecar_is_respawned(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(hl, "_rpc", lambda *a, **k: False)
    monkeypatch.setattr(hl.subprocess, "Popen", _FakeProc)
    first = hl.verified_chain(ETH, wait_s=5)
    hl._sidecars[1]._proc.returncode = 1      # crashed since
    second = hl.verified_chain(ETH, wait_s=5)
    assert second is not None
    assert second.rpc_url != first.rpc_url    # fresh port, fresh process


def test_prewarm_spawns_once_and_never_blocks(monkeypatch):
    """prewarm = one instant Popen, NO readiness polling (that's the
    whole point — sync overlaps with the user looking at the wallet).
    A later verified_chain must reuse the prewarmed process."""
    _enable(monkeypatch)
    spawned = []

    def popen(argv, **kw):
        spawned.append(argv)
        return _FakeProc(argv)

    monkeypatch.setattr(hl.subprocess, "Popen", popen)
    monkeypatch.setattr(hl, "_rpc",
                        lambda *a, **k: pytest.fail("prewarm must not poll"))
    hl.prewarm(ETH)
    hl.prewarm(ETH)                       # idempotent while alive
    assert len(spawned) == 1
    monkeypatch.setattr(hl, "_rpc", lambda *a, **k: False)   # now synced
    shadow = hl.verified_chain(ETH, wait_s=5)
    assert shadow is not None
    assert len(spawned) == 1              # reused, not respawned


def test_prewarm_is_noop_when_unsupported_or_disabled(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(hl.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("must not spawn"))
    hl.prewarm(Chain("TAC", 239, "https://rpc.tac.build"))
    monkeypatch.setenv("QETH_HELIOS", "0")
    hl.prewarm(ETH)
    assert hl._sidecars == {}


# --- orchestrator routing -------------------------------------------------------

def test_simulate_logs_prefers_verified_fork(monkeypatch):
    import qeth.simulate as sim
    shadow = Chain("Ethereum (helios)", 1, "http://127.0.0.1:9999",
                   fallback_rpcs=("http://127.0.0.1:9999",))
    monkeypatch.setattr(hl, "verified_chain", lambda chain, **kw: shadow)
    monkeypatch.setattr(sim, "_simulate_via_rpc",
                        lambda *a, **k: pytest.fail(
                            "must not touch the unverified fast path"))
    seen = {}

    def fake_fork(chain, *a, **k):
        seen["chain"] = chain
        return ["verified"]

    monkeypatch.setattr(sim, "_simulate_via_fork", fake_fork)
    out = sim.simulate_logs(ETH, "0x" + "11" * 20, "0x" + "22" * 20,
                            "0xdead", 0)
    assert out == ["verified"]
    assert seen["chain"] is shadow
    # The marker type carries "this preview is proof-verified" to the UI.
    assert isinstance(out, sim.VerifiedLogs)


def test_no_fork_engine_skips_verified_mode_entirely(monkeypatch):
    """helios binary present but py-evm absent (a package without the
    simulate extra): the verified branch must not hijack the flow into a
    None — previews fall through to eth_simulateV1. It must not even
    probe/spawn the sidecar."""
    import qeth.simulate as sim
    monkeypatch.setattr(sim, "fork_available", lambda: False)
    monkeypatch.setattr(hl, "verified_chain",
                        lambda chain, **kw: pytest.fail(
                            "must not probe helios without an engine"))
    monkeypatch.setattr(sim, "_simulate_via_rpc",
                        lambda *a, **k: [{"address": "0xab"}])
    sim._SIMV1_SUPPORT.clear()
    out = sim.simulate_logs(ETH, "0x" + "11" * 20, "0x" + "22" * 20,
                            "0xdead", 0)
    assert out == [{"address": "0xab"}]
    sim._SIMV1_SUPPORT.clear()


def test_simulate_logs_falls_back_without_helios(monkeypatch):
    import qeth.simulate as sim
    monkeypatch.setattr(hl, "verified_chain", lambda chain, **kw: None)
    monkeypatch.setattr(sim, "_simulate_via_rpc",
                        lambda *a, **k: [{"address": "0xab"}])
    sim._SIMV1_SUPPORT.clear()
    out = sim.simulate_logs(ETH, "0x" + "11" * 20, "0x" + "22" * 20,
                            "0xdead", 0)
    assert out == [{"address": "0xab"}]
    sim._SIMV1_SUPPORT.clear()
