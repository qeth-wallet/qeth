"""Verified-ENS orchestration (qeth.ens) — the Helios-first / strict-CCIP /
fallback logic, with the network calls and sidecar stubbed out. The Helios-first
policy lives in ``qeth.verified.verified_or_fallback``; these stub the sidecar by
patching ``qeth.verified.verified_chain`` (the shadow-chain source)."""

from types import SimpleNamespace

import qeth.ens as ens


def _chain():
    return SimpleNamespace(rpc_url="http://primary", chain_id=1)


def _sidecar(monkeypatch, shadow_url="http://helios"):
    """Pretend a Helios sidecar is ready, resolving to ``shadow_url``."""
    monkeypatch.setattr(
        "qeth.verified.verified_chain",
        lambda c, **k: SimpleNamespace(rpc_url=shadow_url))


def _no_sidecar(monkeypatch):
    monkeypatch.setattr("qeth.verified.verified_chain", lambda c, **k: None)


# --- forward (name → address) ---------------------------------------------

def test_forward_prefers_helios_strict(monkeypatch):
    """Helios ready + on-chain name → resolved strictly (ccip=False) through
    the sidecar, marked verified; the public RPC is never consulted."""
    _sidecar(monkeypatch)
    calls = []

    def fake(url, name, *, ccip=True):
        calls.append((url, ccip))
        return "0xVERIFIED" if url == "http://helios" else "0xUNTRUSTED"
    monkeypatch.setattr(ens, "resolve_ens_address", fake)

    addr, verified = ens.verified_resolve_address(_chain(), "vitalik.eth")
    assert (addr, verified) == ("0xVERIFIED", True)
    assert calls == [("http://helios", False)]      # strict only, no fallback


def test_forward_falls_back_unverified_for_ccip(monkeypatch):
    """An offchain (CCIP) name fails the strict verified attempt → falls back
    to the public RPC with CCIP allowed, marked unverified (no badge)."""
    _sidecar(monkeypatch)
    calls = []

    def fake(url, name, *, ccip=True):
        calls.append((url, ccip))
        return None if url == "http://helios" else "0xCCIP"
    monkeypatch.setattr(ens, "resolve_ens_address", fake)

    addr, verified = ens.verified_resolve_address(_chain(), "x.cb.id")
    assert (addr, verified) == ("0xCCIP", False)
    assert calls == [("http://helios", False), ("http://primary", True)]


def test_forward_unverified_when_no_helios(monkeypatch):
    _no_sidecar(monkeypatch)

    def fake(url, name, *, ccip=True):
        assert url == "http://primary" and ccip is True
        return "0xPLAIN"
    monkeypatch.setattr(ens, "resolve_ens_address", fake)

    assert ens.verified_resolve_address(_chain(), "x.eth") == ("0xPLAIN", False)


# --- reverse (address → name) ---------------------------------------------

def test_reverse_prefers_helios_strict(monkeypatch):
    _sidecar(monkeypatch)

    def fake(url, address, *, ccip=True):
        return "vitalik.eth" if url == "http://helios" else None
    monkeypatch.setattr(ens, "lookup_ens_name", fake)

    assert ens.verified_lookup_name(_chain(), "0xabc") == ("vitalik.eth", True)


def test_reverse_falls_back_unverified(monkeypatch):
    _sidecar(monkeypatch)

    def fake(url, address, *, ccip=True):
        return None if url == "http://helios" else "name.eth"
    monkeypatch.setattr(ens, "lookup_ens_name", fake)

    assert ens.verified_lookup_name(_chain(), "0xabc") == ("name.eth", False)
