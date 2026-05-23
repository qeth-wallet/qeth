"""Live tests for DefiLlama price source."""

from decimal import Decimal

import pytest

from qeth.chains import DEFAULT_CHAINS
from qeth.prices import DefiLlamaPrices, Price

pytestmark = pytest.mark.network

ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)

USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"


def test_native_eth_price():
    out = DefiLlamaPrices().fetch(ETH, contracts=[], include_native=True)
    assert "" in out
    p = out[""]
    assert isinstance(p, Price)
    # ETH has been comfortably above $100 for years; this is a wide
    # sanity floor, not a market call.
    assert Decimal(100) < p.price_usd < Decimal(1_000_000)


def test_stablecoin_prices():
    out = DefiLlamaPrices().fetch(ETH, contracts=[USDC, USDT])
    for addr in (USDC, USDT):
        assert addr.lower() in out, f"{addr} missing from response"
        p = out[addr.lower()]
        # Within ±5% of $1 — well within de-peg tolerance.
        assert Decimal("0.95") < p.price_usd < Decimal("1.05")


def test_unknown_token_is_silently_dropped():
    """A contract DefiLlama doesn't index just doesn't appear in the
    result dict (no exception, no entry)."""
    unknown = "0x" + "d" * 40
    out = DefiLlamaPrices().fetch(ETH, contracts=[USDC, unknown])
    assert USDC.lower() in out
    assert unknown not in out
