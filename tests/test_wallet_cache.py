"""Tests for qeth.wallet_cache — per-wallet display state on disk."""

from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache


def test_save_load_round_trip(tmp_qeth):
    wc = WalletCache()
    original = CachedWallet(
        chain_id=1,
        address="0x7a16ff8270133f063aab6c9977183d9e72835428",
        native_balance_wei=81_397_347_302_538_618,
        native_price_usd="2056.30410",
        native_balance_updated=1779553808,
        native_price_updated=1779553808,
        tokens=[
            CachedToken(
                contract="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                symbol="USDC", name="USD Coin", decimals=6,
                logo_uri="https://example.com/usdc.png",
                balance_raw=1055736626,
                price_usd="0.9997833507400173",
                balance_updated=1779553808,
                price_updated=1779553808,
            ),
        ],
    )
    wc.save(original)

    loaded = wc.load(1, "0x7a16ff8270133f063aab6c9977183d9e72835428")

    assert loaded is not None
    assert loaded.native_balance_wei == original.native_balance_wei
    assert loaded.native_price_usd == original.native_price_usd
    assert len(loaded.tokens) == 1
    t = loaded.tokens[0]
    assert t.symbol == "USDC"
    assert t.balance_raw == 1055736626
    assert t.decimals == 6


def test_load_returns_none_when_no_file(tmp_qeth):
    wc = WalletCache()
    assert wc.load(1, "0x0000000000000000000000000000000000000000") is None


def test_load_returns_none_for_corrupt_json(tmp_qeth):
    wc = WalletCache()
    p = wc._path(1, "0xabcd")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not valid json")
    assert wc.load(1, "0xabcd") is None


def test_address_is_lowercased_on_disk_path(tmp_qeth):
    wc = WalletCache()
    cached = CachedWallet(chain_id=1, address="0xAbCdEf")
    wc.save(cached)
    # Should be findable via different case
    assert wc.load(1, "0xABCDEF") is not None
    assert wc.load(1, "0xabcdef") is not None


def test_huge_native_balance_survives_json_roundtrip(tmp_qeth):
    """Regression: JSON doesn't natively serialize Python's unbounded ints.
    The cache stores wei as int — a balance like 1e25 should round-trip
    exactly, not lose precision via float."""
    wc = WalletCache()
    huge = 10**25  # 10M ETH
    wc.save(CachedWallet(chain_id=1, address="0xa", native_balance_wei=huge))
    assert wc.load(1, "0xa").native_balance_wei == huge
