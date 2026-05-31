"""Hermetic checks for the price-source chain coverage.

A chain that's usable in the app but missing from DEFILLAMA_CHAIN_SLUGS
gets *no* token prices, so the tokens panel drops every priced row at its
`price is None` filter and the wallet looks empty even though discovery
worked. These guard against that gap. (Live fetches are in
test_network_prices.py.)
"""

import json

import pytest

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


class TestNativeCoingeckoId:
    def _chain(self, symbol, coingecko_id):
        from types import SimpleNamespace
        return SimpleNamespace(chain_id=1, symbol=symbol,
                               coingecko_id=coingecko_id)

    def test_symbol_overrides_wrong_config_default(self):
        from qeth.prices import native_coingecko_id
        # Picker-added Avalanche: coingecko_id left at the "ethereum"
        # default, but AVAX must resolve to avalanche-2 (not ETH's price).
        assert native_coingecko_id(self._chain("AVAX", "ethereum")) == "avalanche-2"

    def test_eth_chain_resolves_to_ethereum(self):
        from qeth.prices import native_coingecko_id
        assert native_coingecko_id(self._chain("ETH", "ethereum")) == "ethereum"

    def test_unknown_symbol_with_ethereum_default_is_none(self):
        from qeth.prices import native_coingecko_id
        # Footgun guard: unknown native + the suspicious "ethereum"
        # default → no native price rather than a wrong one.
        assert native_coingecko_id(self._chain("FOO", "ethereum")) is None

    def test_explicit_id_for_unknown_symbol_is_used(self):
        from qeth.prices import native_coingecko_id
        assert native_coingecko_id(self._chain("FOO", "foo-token")) == "foo-token"

    def test_default_chains_all_resolve(self):
        from qeth.prices import native_coingecko_id
        from qeth.chains import DEFAULT_CHAINS
        for c in DEFAULT_CHAINS:
            assert native_coingecko_id(c), f"{c.name} has no native id"


class TestNativeIdDiscovery:
    @pytest.fixture(autouse=True)
    def _reset(self):
        import qeth.prices as prices
        orig = prices._DISCOVERED_NATIVE_IDS
        yield
        prices._DISCOVERED_NATIVE_IDS = orig   # don't leak into other tests

    def test_discovered_map_takes_priority(self, monkeypatch):
        import qeth.prices as prices
        from types import SimpleNamespace
        monkeypatch.setattr(prices, "_DISCOVERED_NATIVE_IDS", {239: "tac"})
        # TAC: not in the symbol map, config left at the "ethereum" default;
        # discovery supplies the right id where a hardcoded map can't.
        ch = SimpleNamespace(chain_id=239, symbol="TAC", coingecko_id="ethereum")
        assert prices.native_coingecko_id(ch) == "tac"

    def test_load_parses_asset_platforms(self, tmp_path, monkeypatch):
        import qeth.prices as prices
        payload = json.dumps([
            {"id": "avalanche", "chain_identifier": 43114,
             "native_coin_id": "avalanche-2"},
            {"id": "tac", "chain_identifier": 239, "native_coin_id": "tac"},
            {"id": "no-chain", "chain_identifier": None, "native_coin_id": "x"},
        ]).encode()

        class R:
            def read(self): return payload
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(prices.urllib.request, "urlopen",
                            lambda req, timeout=None: R())
        m = prices.load_native_coin_ids(cache_dir=tmp_path, force=True)
        assert m == {43114: "avalanche-2", 239: "tac"}   # null chain_id dropped

    def test_load_falls_back_on_error(self, tmp_path, monkeypatch):
        import qeth.prices as prices
        def boom(req, timeout=None):
            raise OSError("network down")
        monkeypatch.setattr(prices.urllib.request, "urlopen", boom)
        assert prices.load_native_coin_ids(cache_dir=tmp_path, force=True) == {}
