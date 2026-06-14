"""qeth.verified — the verified-read abstraction over Helios.

No sidecar / network: ``verified_chain`` and ``EthClient`` are injected so the
prefer-verified-else-fallback policy is tested in isolation.
"""

import qeth.verified as v


class _FakeChain:
    def __init__(self, name):
        self.name = name
        self.rpc_url = f"http://{name}"
        self.chain_id = 1


def test_verified_or_plain_prefers_sidecar(monkeypatch):
    shadow = _FakeChain("helios")
    monkeypatch.setattr(v, "verified_chain", lambda c, **k: shadow)
    chain, verified = v.verified_or_plain(_FakeChain("eth"))
    assert chain is shadow and verified is True


def test_verified_or_plain_falls_back(monkeypatch):
    monkeypatch.setattr(v, "verified_chain", lambda c, **k: None)
    base = _FakeChain("eth")
    chain, verified = v.verified_or_plain(base)
    assert chain is base and verified is False


def test_verified_client_uses_sidecar(monkeypatch):
    shadow = _FakeChain("helios")
    monkeypatch.setattr(v, "verified_chain", lambda c, **k: shadow)
    monkeypatch.setattr("qeth.chain.EthClient", lambda c: ("client", c))
    client, verified = v.verified_client(_FakeChain("eth"))
    assert client == ("client", shadow) and verified is True


def test_verified_client_falls_back_to_plain(monkeypatch):
    monkeypatch.setattr(v, "verified_chain", lambda c, **k: None)
    monkeypatch.setattr("qeth.chain.EthClient", lambda c: ("client", c))
    base = _FakeChain("eth")
    client, verified = v.verified_client(base)
    assert client == ("client", base) and verified is False


def test_verified_client_no_fallback_suppresses_unverified(monkeypatch):
    monkeypatch.setattr(v, "verified_chain", lambda c, **k: None)
    client, verified = v.verified_client(_FakeChain("eth"), fallback=False)
    assert client is None and verified is False
