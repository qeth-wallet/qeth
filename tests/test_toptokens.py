"""Top-tokens-by-market-cap provider (qeth.toptokens)."""

from __future__ import annotations

import json

import qeth.toptokens as tt_mod
from qeth.toptokens import TopToken, TopTokens, fetch_top_tokens

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"


def _seed(tmp_path, chains, fetched_at=0.0):
    p = tmp_path / "seed.json"
    p.write_text(json.dumps({"fetched_at": fetched_at, "chains": chains}))
    return p


def test_contracts_from_seed(tmp_path):
    seed = _seed(tmp_path, {"1": [{"address": USDC, "symbol": "USDC"}]})
    tt = TopTokens(cache_dir=tmp_path / "cache", seed_path=seed)
    assert tt.contracts(1) == [USDC]
    assert tt.contracts(999) == []          # unknown chain → empty, not error


def test_addresses_are_lowercased(tmp_path):
    checksummed = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"   # EIP-55 USDC
    seed = _seed(tmp_path, {"1": [{"address": checksummed, "symbol": "USDC"}]})
    tt = TopTokens(cache_dir=tmp_path / "cache", seed_path=seed)
    assert tt.contracts(1) == [USDC]        # canonical lower for set-dedupe


def test_cache_overrides_seed(tmp_path):
    seed = _seed(tmp_path, {"1": [{"address": USDC, "symbol": "USDC"}]})
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    other = "0x" + "11" * 20
    (cache_dir / "top_tokens.json").write_text(json.dumps(
        {"fetched_at": 123.0, "chains": {"1": [{"address": other, "symbol": "X"}]}}))
    tt = TopTokens(cache_dir=cache_dir, seed_path=seed)
    assert tt.contracts(1) == [other]       # fresh cache wins over the seed


def test_corrupt_cache_falls_back_to_seed(tmp_path):
    seed = _seed(tmp_path, {"1": [{"address": USDC, "symbol": "USDC"}]})
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "top_tokens.json").write_text("{ this is not json")
    tt = TopTokens(cache_dir=cache_dir, seed_path=seed)
    assert tt.contracts(1) == [USDC]        # unreadable cache → seed


def test_is_stale_uses_ttl_and_clock(tmp_path):
    seed = _seed(tmp_path, {"1": []}, fetched_at=1000.0)
    fresh = TopTokens(cache_dir=tmp_path / "a", seed_path=seed,
                      ttl_seconds=100, clock=lambda: 1050.0)
    stale = TopTokens(cache_dir=tmp_path / "b", seed_path=seed,
                      ttl_seconds=100, clock=lambda: 2000.0)
    assert not fresh.is_stale()
    assert stale.is_stale()


def test_refresh_writes_cache_and_updates(tmp_path):
    seed = _seed(tmp_path, {"1": []})
    cache_dir = tmp_path / "cache"
    new = "0x" + "22" * 20

    def fake_fetch(chain_ids, top_n, timeout):
        assert chain_ids == [1]
        return {1: [TopToken(address=new, symbol="NEW")]}

    tt = TopTokens(cache_dir=cache_dir, seed_path=seed,
                   clock=lambda: 5.0, fetch=fake_fetch)
    assert tt.refresh([1]) is True
    assert tt.contracts(1) == [new]
    # cache file was written…
    written = json.loads((cache_dir / "top_tokens.json").read_text())
    assert written["chains"]["1"][0]["address"] == new
    assert written["fetched_at"] == 5.0
    # …and a fresh provider now reads it.
    assert TopTokens(cache_dir=cache_dir, seed_path=seed).contracts(1) == [new]


def test_refresh_tolerates_fetch_failure(tmp_path):
    seed = _seed(tmp_path, {"1": [{"address": USDC, "symbol": "USDC"}]})

    def boom(chain_ids, top_n, timeout):
        raise OSError("network down")

    tt = TopTokens(cache_dir=tmp_path / "c", seed_path=seed, fetch=boom)
    assert tt.refresh([1]) is False
    assert tt.contracts(1) == [USDC]        # seed preserved, no crash


def test_fetch_top_tokens_ranks_and_maps(monkeypatch):
    # markets gives the mcap order; coins/list gives per-chain contracts.
    markets = [{"id": "tether"}, {"id": "usd-coin"}, {"id": "bitcoin"}]
    listing = [
        {"id": "tether", "symbol": "usdt", "platforms": {"ethereum": USDT}},
        {"id": "usd-coin", "symbol": "usdc",
         "platforms": {"ethereum": USDC, "polygon-pos": "0x" + "33" * 20}},
        {"id": "bitcoin", "symbol": "btc", "platforms": {}},   # L1, no contract
    ]

    def fake_get(url, timeout):
        if "coins/markets" in url:
            return markets
        if "coins/list" in url:
            return listing
        raise AssertionError(url)

    monkeypatch.setattr(tt_mod, "_get_json", fake_get)
    out = fetch_top_tokens([1, 137], top_n=10)
    # rank order preserved; BTC dropped (no ERC-20 contract on chain 1)
    assert [t.address for t in out[1]] == [USDT, USDC]
    assert out[1][0].symbol == "USDT"
    assert [t.address for t in out[137]] == ["0x" + "33" * 20]


def test_shipped_seed_is_valid_and_has_usdc():
    # The bundled qeth/data/top_tokens.json must load and carry the majors.
    tt = TopTokens()
    assert USDC in tt.contracts(1)
    assert len(tt.contracts(1)) > 100        # mainnet head is substantial
    assert all(a == a.lower() and a.startswith("0x")
               for a in tt.contracts(1))
