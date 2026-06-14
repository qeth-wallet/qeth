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


def test_verified_or_fallback_prefers_strict_verified(monkeypatch):
    monkeypatch.setattr(v, "verified_chain",
                        lambda c, **k: _FakeChain("helios"))
    seen = []

    def read(url, strict):
        seen.append((url, strict))
        return "0xVERIFIED"            # truthy verified result → no fallback

    result, verified = v.verified_or_fallback(_FakeChain("eth"), read)
    assert (result, verified) == ("0xVERIFIED", True)
    assert seen == [("http://helios", True)]


def test_verified_or_fallback_falls_back_on_empty(monkeypatch):
    # Sidecar ready but the strict read is empty (e.g. offchain name) → fall
    # back to the plain chain, unverified.
    monkeypatch.setattr(v, "verified_chain",
                        lambda c, **k: _FakeChain("helios"))
    seen = []

    def read(url, strict):
        seen.append((url, strict))
        return None if strict else "0xUNVERIFIED"

    result, verified = v.verified_or_fallback(_FakeChain("eth"), read)
    assert (result, verified) == ("0xUNVERIFIED", False)
    assert seen == [("http://helios", True), ("http://eth", False)]


def test_verified_or_fallback_no_sidecar(monkeypatch):
    monkeypatch.setattr(v, "verified_chain", lambda c, **k: None)
    result, verified = v.verified_or_fallback(
        _FakeChain("eth"), lambda url, strict: "0xPLAIN")
    assert (result, verified) == ("0xPLAIN", False)
