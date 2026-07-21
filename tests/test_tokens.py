"""Hermetic tests for qeth.token_discovery.sources — the Etherscan v2 token source.

These never hit the network: ``urllib.request.urlopen`` is monkeypatched
to return a canned Etherscan response. See ``test_network_tokens.py`` for
the live Blockscout integration test.

The point of interest is the single-page ``offset`` cap. The token list
was once truncated at offset=100, hiding a real ~$9k holding that sat
past index 100 (the YB bug). These tests pin the offset we request and
assert we *warn* when a wallet lands exactly on the cap, instead of
silently returning a partial list that's indistinguishable from "this
wallet holds nothing past here".
"""

import json
import logging

import pytest

from qeth.chains import DEFAULT_CHAINS
from qeth.token_discovery import (
    ETHERSCAN_PAGE_CAP,
    EtherscanV2Source,
    UnsupportedChain,
)

ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
ADDR = "0x7a16ff8270133f063aab6c9977183d9e72835428"


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _install_fake(monkeypatch, *, result, status="1", capture=None):
    """Patch urlopen to return one canned Etherscan page. If ``capture``
    is a list, the requested URL is appended to it."""
    body = json.dumps(
        {"status": status, "message": "OK", "result": result}
    ).encode()

    def _fake_urlopen(req, timeout=None):
        if capture is not None:
            capture.append(req.full_url)
        return _FakeResp(body)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)


def _token_row(i: int) -> dict:
    return {
        "TokenAddress": f"0x{i:040x}",
        "TokenSymbol": f"T{i}",
        "TokenName": f"Token {i}",
        "TokenDivisor": "18",
        "TokenQuantity": "1000000000000000000",
    }


class TestEtherscanOffsetCap:
    def test_requests_the_full_page_cap(self, monkeypatch):
        """We must ask for the cap, not a smaller page — a smaller
        offset is exactly what hid the YB holding."""
        urls: list[str] = []
        _install_fake(monkeypatch, result=[_token_row(0)], capture=urls)
        EtherscanV2Source(lambda: "KEY").list_balances(ETH, ADDR)
        assert len(urls) == 1
        assert f"offset={ETHERSCAN_PAGE_CAP}" in urls[0]

    def test_warns_when_landing_on_the_cap(self, monkeypatch, caplog):
        """A full page back is a truncation signal, not a clean result."""
        full_page = [_token_row(i) for i in range(ETHERSCAN_PAGE_CAP)]
        _install_fake(monkeypatch, result=full_page)
        with caplog.at_level(logging.WARNING, logger="qeth.token_discovery.sources"):
            out = EtherscanV2Source(lambda: "KEY").list_balances(ETH, ADDR)
        # Everything fetched is still returned — we don't drop the page,
        # we just flag that there may be more beyond it.
        assert len(out) == ETHERSCAN_PAGE_CAP
        assert any(
            "page cap" in r.message and str(ETHERSCAN_PAGE_CAP) in r.message
            for r in caplog.records
        ), "expected a truncation warning when the page is full"

    def test_no_warning_below_the_cap(self, monkeypatch, caplog):
        partial = [_token_row(i) for i in range(5)]
        _install_fake(monkeypatch, result=partial)
        with caplog.at_level(logging.WARNING, logger="qeth.token_discovery.sources"):
            out = EtherscanV2Source(lambda: "KEY").list_balances(ETH, ADDR)
        assert len(out) == 5
        assert not any("page cap" in r.message for r in caplog.records)


class TestEtherscanSupportAndKey:
    def test_supports_requires_key_and_known_chain(self):
        with_key = EtherscanV2Source(lambda: "KEY")
        without_key = EtherscanV2Source(lambda: "")
        assert with_key.supports(ETH) is True
        assert without_key.supports(ETH) is False

    def test_list_balances_without_key_raises(self):
        with pytest.raises(UnsupportedChain):
            EtherscanV2Source(lambda: "").list_balances(ETH, ADDR)

    def test_empty_result_is_not_an_error(self, monkeypatch):
        _install_fake(
            monkeypatch, result=[], status="0",
        )
        # "no token found" style responses must yield [], not raise.
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout=None: _FakeResp(
                json.dumps(
                    {"status": "0", "message": "No token found", "result": []}
                ).encode()
            ),
        )
        out = EtherscanV2Source(lambda: "KEY").list_balances(ETH, ADDR)
        assert out == []


class _FakeSource:
    """Records calls and returns a sentinel list, or raises."""
    def __init__(self, supported, *, raises=None, tag="x"):
        self._supported = set(supported)
        self._raises = raises
        self.calls = 0
        self.tag = tag
    def supports(self, chain):
        return chain.chain_id in self._supported
    def list_balances(self, chain, address):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return [self.tag]


class _Clock:
    def __init__(self):
        self.t = 1000.0
    def __call__(self):
        return self.t


class TestRoutedTokenSourceCooldown:
    def test_first_call_uses_primary(self):
        from qeth.token_discovery import RoutedTokenSource
        clk = _Clock()
        primary = _FakeSource({1}, tag="ether")
        secondary = _FakeSource({1}, tag="blockscout")
        r = RoutedTokenSource(primary, secondary, cooldown=2.0, clock=clk)
        assert r.list_balances(ETH, ADDR) == ["ether"]
        assert primary.calls == 1 and secondary.calls == 0

    def test_burst_within_cooldown_diverts_to_secondary(self):
        from qeth.token_discovery import RoutedTokenSource
        clk = _Clock()
        primary = _FakeSource({1}, tag="ether")
        secondary = _FakeSource({1}, tag="blockscout")
        r = RoutedTokenSource(primary, secondary, cooldown=2.0, clock=clk)
        r.list_balances(ETH, ADDR)          # primary, stamps clock
        clk.t += 0.5                         # still inside the window
        assert r.list_balances(ETH, ADDR) == ["blockscout"]
        assert primary.calls == 1 and secondary.calls == 1

    def test_primary_resumes_after_cooldown(self):
        from qeth.token_discovery import RoutedTokenSource
        clk = _Clock()
        primary = _FakeSource({1}, tag="ether")
        secondary = _FakeSource({1}, tag="blockscout")
        r = RoutedTokenSource(primary, secondary, cooldown=2.0, clock=clk)
        r.list_balances(ETH, ADDR)
        clk.t += 3.0                         # window elapsed
        assert r.list_balances(ETH, ADDR) == ["ether"]
        assert primary.calls == 2

    def test_falls_back_to_secondary_on_primary_error(self):
        # Etherscan's addresstokenbalance is PRO-only → TokenSourceError on a
        # free key. Must fall back to Blockscout, not propagate (which collapsed
        # the Tokens tab to the top-N pass and dropped real holdings).
        from qeth.token_discovery import RoutedTokenSource, TokenSourceError
        primary = _FakeSource({1}, raises=TokenSourceError("PRO endpoint"), tag="ether")
        secondary = _FakeSource({1}, tag="blockscout")
        r = RoutedTokenSource(primary, secondary)
        assert r.list_balances(ETH, ADDR) == ["blockscout"]
        assert primary.calls == 1 and secondary.calls == 1

    def test_falls_back_on_any_primary_exception(self):
        from qeth.token_discovery import RoutedTokenSource
        primary = _FakeSource({1}, raises=OSError("connection reset"), tag="ether")
        secondary = _FakeSource({1}, tag="blockscout")
        r = RoutedTokenSource(primary, secondary)
        assert r.list_balances(ETH, ADDR) == ["blockscout"]

    def test_primary_error_propagates_when_no_secondary_for_chain(self):
        import pytest
        from qeth.token_discovery import RoutedTokenSource, TokenSourceError
        BNB = next(c for c in DEFAULT_CHAINS if c.chain_id == 56)
        primary = _FakeSource({56}, raises=TokenSourceError("boom"), tag="ether")
        secondary = _FakeSource(set(), tag="blockscout")   # can't serve BNB
        with pytest.raises(TokenSourceError):
            RoutedTokenSource(primary, secondary).list_balances(BNB, ADDR)

    def test_cooldown_ignored_when_secondary_cant_serve_chain(self):
        # BNB-style: secondary doesn't support the chain, so even inside
        # the window we must keep using the primary (no alternative).
        from qeth.token_discovery import RoutedTokenSource
        BNB = next(c for c in DEFAULT_CHAINS if c.chain_id == 56)
        clk = _Clock()
        primary = _FakeSource({56}, tag="ether")
        secondary = _FakeSource(set(), tag="blockscout")
        r = RoutedTokenSource(primary, secondary, cooldown=2.0, clock=clk)
        r.list_balances(BNB, ADDR)
        clk.t += 0.1
        assert r.list_balances(BNB, ADDR) == ["ether"]
        assert primary.calls == 2 and secondary.calls == 0

    def test_rate_limited_primary_falls_back_to_secondary(self):
        from qeth.token_discovery import RoutedTokenSource, RateLimited
        clk = _Clock()
        primary = _FakeSource({1}, raises=RateLimited("slow down"), tag="ether")
        secondary = _FakeSource({1}, tag="blockscout")
        r = RoutedTokenSource(primary, secondary, cooldown=2.0, clock=clk)
        assert r.list_balances(ETH, ADDR) == ["blockscout"]
        assert primary.calls == 1 and secondary.calls == 1
