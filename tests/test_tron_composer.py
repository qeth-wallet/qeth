"""The pieces behind the Send dialog's parity on Tron: the node-side
simulation (Events preview + revert banner), revert detection in constant
calls, and the Tronscan identity source (the recipient's identity row)."""

from __future__ import annotations

import io
import json

import pytest

from qeth.address import tron_from_hex
from qeth.chains import DEFAULT_CHAINS, TRON
from qeth.tron.client import TronClient, TronError, call_reverted, revert_reason

CHAIN = next(c for c in DEFAULT_CHAINS if c.family == TRON)
OWNER = "0x" + "d1" * 20
USDT = "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c"

OK_CALL = {
    "result": {"result": True},
    "energy_used": 64285,
    "constant_result": ["0" * 63 + "1"],
    "logs": [{"address": USDT[2:],
              "topics": ["ddf252ad" + "00" * 28, "00" * 32, "00" * 32],
              "data": "0f4240".rjust(64, "0")}],
    "transaction": {"ret": [{}]},
}
REVERTED = {
    "result": {"result": True, "message": "524556455254206f70636f6465206578656375746564"},
    "energy_used": 8624, "constant_result": [""],
    "transaction": {"ret": [{"ret": "FAILED"}]},
}
# Error(string) "insufficient balance"
_REASON = ("08c379a0" + (32).to_bytes(32, "big").hex()
           + (20).to_bytes(32, "big").hex()
           + b"insufficient balance".hex().ljust(64, "0"))
REVERTED_WITH_REASON = {**REVERTED, "constant_result": [_REASON]}


def _serve(monkeypatch, answer):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: io.BytesIO(json.dumps(answer).encode()))


def test_revert_detection_and_reason():
    assert not call_reverted(OK_CALL)
    assert call_reverted(REVERTED)
    assert revert_reason(REVERTED) == "REVERT opcode executed"
    assert revert_reason(REVERTED_WITH_REASON) == "insufficient balance"


def test_trigger_constant_refuses_a_reverting_call(monkeypatch):
    """The energy estimate must not price a call that reverts."""
    _serve(monkeypatch, REVERTED)
    with pytest.raises(TronError, match="REVERT"):
        TronClient(CHAIN).trigger_constant(OWNER, USDT, b"\x01")


class TestSimulation:
    def test_contract_call_events_in_receipt_shape(self, monkeypatch):
        from qeth.plugins.transactions.simulate import (
            simulate_logs, simulation_available)
        _serve(monkeypatch, OK_CALL)
        assert simulation_available(CHAIN)
        logs = simulate_logs(CHAIN, OWNER, USDT, "0xa9059cbb", 0)
        assert logs == [{
            "address": USDT,
            "topics": ["0xddf252ad" + "00" * 28, "0x" + "00" * 32, "0x" + "00" * 32],
            "data": "0x" + "0f4240".rjust(64, "0")}]

    def test_revert_is_a_revert_note(self, monkeypatch):
        from qeth.plugins.transactions.simulate import RevertNote, simulate_logs
        _serve(monkeypatch, REVERTED_WITH_REASON)
        out = simulate_logs(CHAIN, OWNER, USDT, "0xa9059cbb", 0)
        assert isinstance(out, RevertNote) and out.reason == "insufficient balance"

    def test_a_trx_transfer_runs_no_contract(self, monkeypatch):
        from qeth.plugins.transactions.simulate import simulate_logs

        def boom(*a, **k):
            raise AssertionError("no node call for a plain TRX transfer")
        monkeypatch.setattr("urllib.request.urlopen", boom)
        assert simulate_logs(CHAIN, OWNER, "0x" + "22" * 20, "0x", 5) == []


class TestTronIdentity:
    def _source(self, code, tronscan=None):
        from qeth.plugins.transactions.contract_identity import (
            ContractIdentitySource, TronIdentitySource)
        seen: list[str] = []

        def transport(url, timeout):
            seen.append(url)
            return json.dumps(tronscan or {}).encode()
        tron = TronIdentitySource(lambda cid, a: code, transport=transport)
        return ContractIdentitySource(lambda: None, tron=tron), seen

    def test_a_regular_account(self):
        src, seen = self._source("0x")
        idy = src.fetch(CHAIN.chain_id, OWNER)
        assert idy is not None and not idy.is_contract
        assert seen == []                     # no Tronscan call for an account

    def test_a_contract_from_tronscan(self):
        creator = tron_from_hex("0x" + "ab" * 20)
        src, seen = self._source("0x6080", {"data": [{
            "name": "TetherToken", "verify_status": 2, "tag1": "USDT Token",
            "date_created": 1555400628000, "creator": {"address": creator}}]})
        idy = src.fetch(CHAIN.chain_id, USDT)
        assert seen == [f"https://apilist.tronscanapi.com/api/contract?contract="
                        f"{tron_from_hex(USDT)}"]
        assert (idy.is_contract, idy.name, idy.verified, idy.name_tag) == (
            True, "TetherToken", True, "USDT Token")
        assert idy.deployer.lower() == "0x" + "ab" * 20
        assert idy.deployed_at == 1555400628

    def test_badge_shows_the_deployer_in_tron_form(self):
        from qeth.address import codec_for
        from qeth.plugins.transactions.contract_identity import (
            ContractIdentity, describe_identity)
        idy = ContractIdentity(address=USDT, is_contract=True, verified=False,
                               deployer="0x" + "ab" * 20, deployed_at=1)
        badge = describe_identity(idy, my_addresses=[], now_ts=10**10,
                                  short=codec_for(TRON).short)
        assert tron_from_hex("0x" + "ab" * 20)[:5] in badge.text
