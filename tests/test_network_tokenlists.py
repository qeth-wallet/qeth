"""Live tests for each TokenListSource implementation.

Each source has an exhaustively-different response shape (Uniswap +
CoinGecko: tokenlists.org schema; Curve: ``{data: {tokens: [...]}}``;
1inch: dict keyed by address). Schema drift in any of them would
silently empty out the merged index — these tests guard against that.
"""

import pytest

from qeth.tokenlists import CoinGeckoPerChain, Curve, OneInch, UniswapDefault

pytestmark = pytest.mark.network


@pytest.mark.parametrize("source_cls", [
    UniswapDefault, CoinGeckoPerChain, Curve, OneInch,
])
def test_source_yields_well_formed_entries(source_cls, tmp_path):
    """Pull at least one batch and verify each entry has the canonical
    fields populated correctly."""
    src = source_cls()
    entries = list(src.fetch_entries(tmp_path, ttl=300.0, timeout=15.0))
    assert len(entries) > 0, f"{source_cls.__name__} returned nothing"
    for e in entries[:20]:
        assert e.chain_id > 0
        assert e.address.startswith("0x") and len(e.address) == 42
        assert e.address == e.address.lower()
        assert e.symbol  # non-empty
        assert isinstance(e.decimals, int)


def test_uniswap_default_covers_multiple_chains(tmp_path):
    """The Uniswap default list is multichain in a single file — that's
    its main value-add over the per-chain feeds."""
    src = UniswapDefault()
    entries = list(src.fetch_entries(tmp_path, ttl=0.0, timeout=15.0))
    chains_seen = {e.chain_id for e in entries}
    # At minimum Ethereum and one L2 should appear.
    assert 1 in chains_seen
    assert any(cid in chains_seen for cid in (10, 137, 42161, 8453))


def test_coingecko_includes_well_known_token_on_eth(tmp_path):
    """USDC's contract address should appear in the CoinGecko list for
    Ethereum; otherwise the URL/schema drifted."""
    src = CoinGeckoPerChain()
    USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    found = any(
        e.address == USDC for e in src.fetch_entries(tmp_path, 0.0, 15.0)
        if e.chain_id == 1
    )
    assert found


def test_curve_returns_pool_assets(tmp_path):
    """The Curve source pulls /v1/getTokens/all per chain — verify
    canonical pool assets like USDC come back for Ethereum."""
    src = Curve()
    USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
    found = any(
        e.address == USDC for e in src.fetch_entries(tmp_path, 0.0, 15.0)
        if e.chain_id == 1
    )
    assert found, "USDC missing from Curve's getTokens — endpoint may have drifted"
