"""Tests for qeth.risk — RiskReport thresholds, parsing, cache TTL."""

import time

import pytest

from qeth.risk import (
    HIGH_SELL_TAX,
    GoPlusRisk,
    RiskCache,
    RiskReport,
    _parse_report,
    _to_bool,
    _to_float,
)


# --- string→bool / float coercion (GoPlus sends "0"/"1") -----------------

class TestToBool:
    @pytest.mark.parametrize("inp,expected", [
        ("1", True), ("0", False), ("", False), (None, False),
        ("true", True), ("True", True), ("false", False),
        ("null", False), (True, True), (False, False),
    ])
    def test_cases(self, inp, expected):
        assert _to_bool(inp) is expected


class TestToFloat:
    @pytest.mark.parametrize("inp,expected", [
        ("0.5", 0.5), ("1", 1.0), ("", 0.0), (None, 0.0),
        ("null", 0.0), ("not-a-number", 0.0),
    ])
    def test_cases(self, inp, expected):
        assert _to_float(inp) == expected


# --- _parse_report --------------------------------------------------------

def test_parse_report_from_goplus_shape():
    info = {
        "is_honeypot": "1", "is_blacklisted": "0", "hidden_owner": "1",
        "cannot_buy": "0", "cannot_sell_all": "0",
        "can_take_back_ownership": "0",
        "is_proxy": "1", "is_in_dex": "1",
        "buy_tax": "0.05", "sell_tax": "",
    }
    r = _parse_report(info, now=123456)
    assert r.is_honeypot is True
    assert r.hidden_owner is True
    assert r.is_proxy is True
    assert r.buy_tax == pytest.approx(0.05)
    assert r.sell_tax == 0.0  # empty → 0.0
    assert r.fetched_at == 123456


# --- RiskReport.is_high_risk -----------------------------------------------

class TestIsHighRisk:
    def test_clean_report_is_not_high_risk(self):
        assert not RiskReport().is_high_risk()

    @pytest.mark.parametrize("kwargs", [
        {"is_honeypot": True},
        {"is_blacklisted": True},
        {"hidden_owner": True},
        {"cannot_buy": True},
        {"cannot_sell_all": True},
        {"can_take_back_ownership": True},
    ])
    def test_any_smoking_gun_flag_is_high(self, kwargs):
        assert RiskReport(**kwargs).is_high_risk()

    def test_high_sell_tax(self):
        assert RiskReport(sell_tax=HIGH_SELL_TAX + 0.001).is_high_risk()

    def test_exact_threshold_is_not_high(self):
        # Strictly greater-than, so the boundary passes.
        assert not RiskReport(sell_tax=HIGH_SELL_TAX).is_high_risk()

    def test_low_sell_tax_alone_is_not_high(self):
        assert not RiskReport(sell_tax=0.05).is_high_risk()


# --- RiskCache ------------------------------------------------------------

class TestRiskCache:
    def test_round_trip(self, tmp_qeth):
        c = RiskCache()
        c.put_many(1, {
            "0xAAAA": RiskReport(is_honeypot=True, sell_tax=0.6,
                                 fetched_at=int(time.time())),
        })
        r = c.get(1, "0xaaaa")
        assert r is not None
        assert r.is_honeypot is True
        assert r.sell_tax == pytest.approx(0.6)

    def test_address_lookup_case_insensitive(self, tmp_qeth):
        c = RiskCache()
        c.put_many(1, {"0xAaBb": RiskReport(fetched_at=int(time.time()))})
        assert c.get(1, "0xAABB") is not None
        assert c.get(1, "0xaabb") is not None

    def test_stale_entry_is_treated_as_missing(self, tmp_qeth):
        c = RiskCache(ttl_seconds=60)
        c.put_many(1, {"0xAA": RiskReport(fetched_at=int(time.time()) - 9999)})
        assert c.get(1, "0xAA") is None

    def test_missing_returns_uncached(self, tmp_qeth):
        c = RiskCache()
        c.put_many(1, {"0xAA": RiskReport(fetched_at=int(time.time()))})
        assert c.missing(1, ["0xAA", "0xBB"]) == ["0xBB"]

    def test_missing_also_returns_stale(self, tmp_qeth):
        c = RiskCache(ttl_seconds=60)
        c.put_many(1, {"0xAA": RiskReport(fetched_at=int(time.time()) - 9999)})
        assert "0xAA" in c.missing(1, ["0xAA"])

    def test_persisted_across_instances(self, tmp_qeth):
        c1 = RiskCache()
        c1.put_many(1, {"0xAA": RiskReport(is_honeypot=True,
                                           fetched_at=int(time.time()))})
        c2 = RiskCache()
        assert c2.get(1, "0xAA").is_honeypot is True

    def test_corrupt_cache_file_is_recovered(self, tmp_qeth):
        c = RiskCache()
        p = c._path(1)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ not valid json")
        assert c.get(1, "0xAA") is None


# --- GoPlusRisk.fetch (mocked HTTP) ---------------------------------------

class TestGoPlusFetch:
    def test_empty_input(self):
        assert GoPlusRisk().fetch(1, []) == {}

    def test_parses_response(self, monkeypatch):
        from urllib.request import Request
        import json

        addr = "0x" + "a" * 40   # GoPlus.fetch filters bad-length addrs

        def fake_urlopen(req, timeout=None):
            assert isinstance(req, Request)
            body = json.dumps({
                "message": "OK",
                "result": {
                    addr: {
                        "is_honeypot": "1",
                        "is_blacklisted": "0",
                        "hidden_owner": "0",
                        "sell_tax": "0.6",
                        "buy_tax": "0.0",
                    },
                },
            }).encode()
            class R:
                def read(self): return body
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()

        monkeypatch.setattr("qeth.risk.urllib.request.urlopen", fake_urlopen)
        out = GoPlusRisk().fetch(1, [addr])
        assert addr in out
        assert out[addr].is_honeypot is True
        assert out[addr].is_high_risk()

    def test_network_failure_returns_empty(self, monkeypatch):
        def boom(req, timeout=None):
            raise OSError("no network")
        monkeypatch.setattr("qeth.risk.urllib.request.urlopen", boom)
        # Should not raise; just return empty dict.
        assert GoPlusRisk().fetch(1, ["0x" + "a" * 40]) == {}

    def test_short_addresses_are_skipped(self, monkeypatch):
        """Passing a malformed (non-42-char) address should silently drop
        it before issuing a request — no exceptions."""
        called = []
        def watch(req, timeout=None):
            called.append(req)
            raise RuntimeError("shouldn't reach here")
        monkeypatch.setattr("qeth.risk.urllib.request.urlopen", watch)
        assert GoPlusRisk().fetch(1, ["0xAA", "not-an-address"]) == {}
        assert not called
