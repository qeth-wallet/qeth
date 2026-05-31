"""Hermetic checks for the price-source chain coverage.

A chain that's usable in the app but missing from DEFILLAMA_CHAIN_SLUGS
gets *no* token prices, so the tokens panel drops every priced row at its
`price is None` filter and the wallet looks empty even though discovery
worked. These guard against that gap. (Live fetches are in
test_network_prices.py.)
"""

from qeth.prices import DEFILLAMA_CHAIN_SLUGS
from qeth.chains import DEFAULT_CHAINS


def test_every_default_chain_has_a_price_slug():
    missing = [c.chain_id for c in DEFAULT_CHAINS
               if c.chain_id not in DEFILLAMA_CHAIN_SLUGS]
    assert not missing, f"chains with no DefiLlama price slug: {missing}"


def test_avalanche_is_covered():
    # Regression: Avalanche tokens (e.g. USDT) were hidden because 43114
    # had no price slug, so DefiLlama was never queried for them.
    assert DEFILLAMA_CHAIN_SLUGS.get(43114) == "avax"


def test_avalanche_in_curated_tokenlist_sources():
    """Discovery drops any token not in a curated list (is_known), so the
    per-chain tokenlist sources must include Avalanche or its tokens never
    surface — even priced ones."""
    from qeth.tokenlists import CoinGeckoPerChain, Curve, OneInch
    assert 43114 in CoinGeckoPerChain.SLUGS
    assert 43114 in Curve.SLUGS
    assert 43114 in OneInch.CHAINS
