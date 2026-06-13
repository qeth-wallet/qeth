"""Verified-ENS orchestration (qeth.ens) — the Helios-first / strict-CCIP /
fallback logic, with the network calls and sidecar stubbed out."""

from types import SimpleNamespace

import qeth.ens as ens


def _chain():
    return SimpleNamespace(rpc_url="http://primary", chain_id=1)


# --- forward (name → address) ---------------------------------------------

def test_forward_prefers_helios_strict(monkeypatch):
    """Helios ready + on-chain name → resolved strictly (ccip=False) through
    the sidecar, marked verified; the public RPC is never consulted."""
    monkeypatch.setattr(ens, "_verified_mainnet",
                        lambda c, w: SimpleNamespace(rpc_url="http://helios"))
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
    monkeypatch.setattr(ens, "_verified_mainnet",
                        lambda c, w: SimpleNamespace(rpc_url="http://helios"))
    calls = []

    def fake(url, name, *, ccip=True):
        calls.append((url, ccip))
        return None if url == "http://helios" else "0xCCIP"
    monkeypatch.setattr(ens, "resolve_ens_address", fake)

    addr, verified = ens.verified_resolve_address(_chain(), "x.cb.id")
    assert (addr, verified) == ("0xCCIP", False)
    assert calls == [("http://helios", False), ("http://primary", True)]


def test_forward_unverified_when_no_helios(monkeypatch):
    monkeypatch.setattr(ens, "_verified_mainnet", lambda c, w: None)

    def fake(url, name, *, ccip=True):
        assert url == "http://primary" and ccip is True
        return "0xPLAIN"
    monkeypatch.setattr(ens, "resolve_ens_address", fake)

    assert ens.verified_resolve_address(_chain(), "x.eth") == ("0xPLAIN", False)


# --- reverse (address → name) ---------------------------------------------

def test_reverse_prefers_helios_strict(monkeypatch):
    monkeypatch.setattr(ens, "_verified_mainnet",
                        lambda c, w: SimpleNamespace(rpc_url="http://helios"))

    def fake(url, address, *, ccip=True):
        return "vitalik.eth" if url == "http://helios" else None
    monkeypatch.setattr(ens, "lookup_ens_name", fake)

    assert ens.verified_lookup_name(_chain(), "0xabc") == ("vitalik.eth", True)


def test_reverse_falls_back_unverified(monkeypatch):
    monkeypatch.setattr(ens, "_verified_mainnet",
                        lambda c, w: SimpleNamespace(rpc_url="http://helios"))

    def fake(url, address, *, ccip=True):
        return None if url == "http://helios" else "name.eth"
    monkeypatch.setattr(ens, "lookup_ens_name", fake)

    assert ens.verified_lookup_name(_chain(), "0xabc") == ("name.eth", False)


def test_verified_mainnet_swallows_helios_errors(monkeypatch):
    """A broken/absent Helios import degrades to None (unverified path), never
    raises into the worker."""
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "qeth.helios" or name.endswith(".helios"):
            raise RuntimeError("no helios")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", boom)
    assert ens._verified_mainnet(_chain(), 0.0) is None
