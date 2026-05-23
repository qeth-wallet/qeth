"""Tests for the scam heuristic and source-failure resilience in
qeth.tokenlists.
"""

import pytest

from qeth.risk import RiskReport
from qeth.tokenlists import TokenLists


REAL_USDC_ETH = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
SCAM_ETH      = "0xdeadbeef00000000000000000000000000000001"


@pytest.fixture
def lists_with_usdc():
    """A TokenLists with USDC whitelisted on Ethereum and nothing else."""
    lists = TokenLists(sources=[])
    lists._index[(1, REAL_USDC_ETH)] = type("E", (), {
        "chain_id": 1, "address": REAL_USDC_ETH,
        "symbol": "USDC", "name": "USD Coin", "decimals": 6,
        "source": "test", "logo_uri": None,
    })()
    lists._loaded = True
    return lists


class TestIsLikelyScam:
    def test_whitelist_short_circuits(self, lists_with_usdc):
        # Even with an obviously scammy name/symbol, presence in a
        # whitelist trumps everything.
        assert not lists_with_usdc.is_likely_scam(
            1, REAL_USDC_ETH,
            symbol="VISIT https://x.com",
            name="airdrop reward",
        )

    def test_url_in_name(self, lists_with_usdc):
        assert lists_with_usdc.is_likely_scam(
            1, SCAM_ETH, "MEME", "Visit https://drop.io for free $100",
        )

    def test_url_in_symbol(self, lists_with_usdc):
        assert lists_with_usdc.is_likely_scam(
            1, SCAM_ETH, "$ - Visit DropUSDC.com to claim", "Spam",
        )

    def test_keyword_in_name(self, lists_with_usdc):
        assert lists_with_usdc.is_likely_scam(
            1, SCAM_ETH, "X", "claim your reward",
        )

    def test_plain_ascii_impersonation(self, lists_with_usdc):
        # Symbol literally "USDC" on a non-canonical contract.
        assert lists_with_usdc.is_likely_scam(
            1, SCAM_ETH, "USDC", "USD Coin",
        )

    def test_canonical_symbol_check_is_case_insensitive(self, lists_with_usdc):
        # Real example from one user's wallet
        assert lists_with_usdc.is_likely_scam(1, SCAM_ETH, "usdc", "USD Coin")
        assert lists_with_usdc.is_likely_scam(1, SCAM_ETH, "Usdt", "Tether")

    def test_legit_meme_token_is_not_flagged(self, lists_with_usdc):
        # Random meme coin with a distinct name; no URL, no impersonation.
        assert not lists_with_usdc.is_likely_scam(
            1, "0xdeadbeef02", "PEPE2", "Pepe coin 2",
        )

    def test_high_risk_overrides(self, lists_with_usdc):
        """A clean-looking name doesn't save a contract with GoPlus
        red flags."""
        risk = RiskReport(is_honeypot=True, fetched_at=1)
        assert lists_with_usdc.is_likely_scam(
            1, SCAM_ETH, "PEPE", "Pepe coin", risk=risk,
        )

    def test_clean_risk_doesnt_force_flag(self, lists_with_usdc):
        risk = RiskReport(is_honeypot=False, fetched_at=1)
        assert not lists_with_usdc.is_likely_scam(
            1, "0xdeadbeef02", "PEPE2", "Pepe coin 2", risk=risk,
        )

    def test_none_risk_is_safe(self, lists_with_usdc):
        # Passing risk=None should behave as "no info"; the text
        # heuristic still runs.
        assert lists_with_usdc.is_likely_scam(
            1, SCAM_ETH, "USDC", "fake usdc", risk=None,
        )

    def test_whitelist_beats_risk(self, lists_with_usdc):
        risk = RiskReport(is_honeypot=True, fetched_at=1)
        # USDC is on the whitelist; even a "honeypot" verdict from
        # GoPlus shouldn't flag the canonical USDC contract.
        assert not lists_with_usdc.is_likely_scam(
            1, REAL_USDC_ETH, "USDC", "USD Coin", risk=risk,
        )


class TestLoadFailureTolerant:
    """One bad source must never sink the whole index."""

    def test_one_failing_source_doesnt_block_others(self, tmp_qeth):
        from qeth.tokenlists import TokenListSource, TokenListEntry

        class Good(TokenListSource):
            name = "good"
            def fetch_entries(self, cache_dir, ttl, timeout):
                yield TokenListEntry(
                    chain_id=1, address="0xaaaa", symbol="A", name="Aye",
                    decimals=18, source="good",
                )

        class Broken(TokenListSource):
            name = "broken"
            def fetch_entries(self, cache_dir, ttl, timeout):
                raise RuntimeError("simulated outage")

        # Order matters: broken first to prove it doesn't abort the loop.
        lists = TokenLists(sources=[Broken(), Good()])
        lists.load()

        assert lists.count() == 1
        assert lists.is_known(1, "0xaaaa")

    def test_first_source_to_provide_a_pair_wins(self, tmp_qeth):
        from qeth.tokenlists import TokenListSource, TokenListEntry

        class A(TokenListSource):
            name = "a"
            def fetch_entries(self, cache_dir, ttl, timeout):
                yield TokenListEntry(
                    chain_id=1, address="0xaaaa",
                    symbol="FIRST", name="From A",
                    decimals=18, source="a",
                )

        class B(TokenListSource):
            name = "b"
            def fetch_entries(self, cache_dir, ttl, timeout):
                yield TokenListEntry(
                    chain_id=1, address="0xaaaa",
                    symbol="SECOND", name="From B",
                    decimals=6, source="b",
                )

        lists = TokenLists(sources=[A(), B()])
        lists.load()
        e = lists.get(1, "0xaaaa")
        assert e.symbol == "FIRST"
        assert e.source == "a"
