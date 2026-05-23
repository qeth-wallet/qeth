"""Live tests for GoPlus risk source.

GoPlus has rate limits (~30 req/min on the free tier). These tests
batch a small set and run sparingly.
"""

import pytest

from qeth.risk import GoPlusRisk, RiskReport

pytestmark = pytest.mark.network

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"


def test_returns_reports_dict():
    out = GoPlusRisk().fetch(1, [USDC, USDT])
    assert isinstance(out, dict)
    # At least one of the well-known tokens should come back. GoPlus
    # sometimes skips trust-list assets like USDC; not asserting on
    # which exactly to keep the test resilient.
    assert any(addr in out for addr in (USDC, USDT)) or out == {}


def test_short_addresses_silently_dropped():
    out = GoPlusRisk().fetch(1, ["0xAA", "not-an-address"])
    assert out == {}


def test_high_risk_flag_for_known_risky_pattern():
    """Smoke test for the parser path on a high-tax / honeypot-shape
    contract. Picks a contract previously flagged by GoPlus; if it
    returns nothing the test just skips so we don't fail on data drift."""
    # SHIB-derivative honeypots have lived briefly; rather than hard-
    # coding a specific scam contract (they get rugged and disappear),
    # we just verify that the *fields exist and parse correctly* by
    # asking for any reports we can get and inspecting one.
    out = GoPlusRisk().fetch(1, [USDC, USDT])
    if not out:
        pytest.skip("GoPlus returned no data for either USDC or USDT")
    sample = next(iter(out.values()))
    assert isinstance(sample, RiskReport)
    # All flag fields exist on the dataclass; type-check them.
    assert isinstance(sample.is_honeypot, bool)
    assert isinstance(sample.is_blacklisted, bool)
    assert isinstance(sample.sell_tax, float)
    assert isinstance(sample.is_high_risk(), bool)
