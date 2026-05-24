"""Tests for qeth.store — config persistence + hide/show semantics."""

import json

import pytest

from qeth.chains import Chain
from qeth.store import Store, _merge_chain


class TestRoundTrip:
    def test_load_with_no_config_yields_defaults(self, tmp_qeth):
        s = Store.load()
        assert s.accounts == []
        assert s.default_account is None
        assert s.current_chain_id == 1
        assert s.hidden_tokens == set()
        assert s.shown_tokens == set()
        # Should have all the default chains
        assert {c.chain_id for c in s.chains} >= {1, 10, 137, 42161, 8453}

    def test_save_then_load_round_trips_everything(self, tmp_qeth):
        s1 = Store()
        s1.accounts = [
            {"address": "0xAAA", "path": "44'/60'/0'/0/0", "source": "ledger",
             "scheme": "Ledger Live", "label": ""},
        ]
        s1.default_account = "0xAAA"
        s1.current_chain_id = 10
        s1.hide_token(1, "0xDeadBeef00000000000000000000000000000001")
        s1.force_show_token(10, "0xFEED00000000000000000000000000000000FEED")
        s1.window_geometry = "deadbeef"
        s1.splitter_state_main = "01020304"
        s1.save()

        s2 = Store.load()
        assert s2.accounts == s1.accounts
        assert s2.default_account == "0xAAA"
        assert s2.current_chain_id == 10
        assert s2.hidden_tokens == {(1, "0xdeadbeef00000000000000000000000000000001")}
        assert s2.shown_tokens == {(10, "0xfeed00000000000000000000000000000000feed")}
        assert s2.window_geometry == "deadbeef"
        assert s2.splitter_state_main == "01020304"


class TestAddAccount:
    def test_first_account_becomes_default(self, tmp_qeth):
        s = Store()
        s.add_account({"address": "0xABCD", "path": "x"})
        assert s.default_account == "0xABCD"

    def test_duplicate_address_with_same_path_is_rejected(self, tmp_qeth):
        s = Store()
        s.add_account({"address": "0xABCD", "path": "x"})
        added = s.add_account({"address": "0xABCD", "path": "x"})
        assert added is False
        assert len(s.accounts) == 1

    def test_same_address_different_path_is_kept(self, tmp_qeth):
        s = Store()
        s.add_account({"address": "0xABCD", "path": "x"})
        s.add_account({"address": "0xABCD", "path": "y"})
        assert len(s.accounts) == 2

    def test_dedup_is_case_insensitive_on_address(self, tmp_qeth):
        s = Store()
        s.add_account({"address": "0xABCD", "path": "x"})
        added = s.add_account({"address": "0xabcd", "path": "x"})
        assert added is False


class TestRemoveAccount:
    def test_removes_match(self, tmp_qeth):
        s = Store()
        s.add_account({"address": "0xAA", "path": "x"})
        s.add_account({"address": "0xBB", "path": "y"})
        removed = s.remove_account("0xAA")
        assert removed is True
        assert [a["address"] for a in s.accounts] == ["0xBB"]

    def test_removing_default_promotes_next(self, tmp_qeth):
        s = Store()
        s.add_account({"address": "0xAA", "path": "x"})
        s.add_account({"address": "0xBB", "path": "y"})
        assert s.default_account == "0xAA"
        s.remove_account("0xAA")
        assert s.default_account == "0xBB"

    def test_removing_last_account_clears_default(self, tmp_qeth):
        s = Store()
        s.add_account({"address": "0xAA", "path": "x"})
        s.remove_account("0xAA")
        assert s.default_account is None

    def test_missing_address_returns_false(self, tmp_qeth):
        s = Store()
        assert s.remove_account("0xCC") is False


class TestHideShow:
    def test_hide_removes_from_shown(self, tmp_qeth):
        s = Store()
        s.force_show_token(1, "0xAAAA")
        s.hide_token(1, "0xAAAA")
        assert s.is_hidden(1, "0xAAAA")
        assert not s.is_force_shown(1, "0xAAAA")

    def test_force_show_removes_from_hidden(self, tmp_qeth):
        s = Store()
        s.hide_token(1, "0xBBBB")
        s.force_show_token(1, "0xBBBB")
        assert not s.is_hidden(1, "0xBBBB")
        assert s.is_force_shown(1, "0xBBBB")

    def test_address_lookup_is_case_insensitive(self, tmp_qeth):
        s = Store()
        s.hide_token(1, "0xAaBbCc")
        assert s.is_hidden(1, "0xAABBCC")
        assert s.is_hidden(1, "0xaabbcc")


class TestChainMigration:
    def test_old_polygon_config_picks_up_new_field(self, tmp_qeth):
        """A polygon entry stored before the coingecko_id field was added
        should get the default-chain's value (polygon-ecosystem-token)
        backfilled on load. Otherwise its native MATIC price gets queried
        as 'ethereum' and shows the wrong USD value."""
        old = {
            "name": "Polygon", "chain_id": 137,
            "rpc_url": "https://polygon.drpc.org",
            "symbol": "MATIC",
            "explorer": "https://polygonscan.com",
        }
        chain = _merge_chain(old)
        assert chain.coingecko_id == "polygon-ecosystem-token"

    def test_user_custom_rpc_url_survives_merge(self, tmp_qeth):
        old = {
            "name": "Polygon", "chain_id": 137,
            "rpc_url": "https://my-private-rpc/polygon",
            "symbol": "MATIC", "explorer": "x",
        }
        chain = _merge_chain(old)
        assert chain.rpc_url == "https://my-private-rpc/polygon"

    def test_unknown_chain_falls_back_to_dataclass_default(self, tmp_qeth):
        unknown = {
            "name": "MyChain", "chain_id": 99999,
            "rpc_url": "x", "symbol": "MYC", "explorer": "",
        }
        chain = _merge_chain(unknown)
        assert chain.coingecko_id == "ethereum"  # Chain dataclass default

    def test_unknown_extra_fields_are_dropped(self, tmp_qeth):
        """Forward-compat: a future schema field should be silently
        ignored by an older client, not crash with TypeError."""
        c = {
            "name": "X", "chain_id": 1, "rpc_url": "x",
            "symbol": "ETH", "explorer": "",
            "future_field_we_dont_know_about": "ignored",
        }
        chain = _merge_chain(c)
        assert chain.name == "X"


class TestPersistedHiddenSet:
    def test_hidden_persists_through_save_load(self, tmp_qeth):
        s1 = Store()
        s1.hide_token(1, "0xAA")
        s1.hide_token(10, "0xBB")
        s1.save()
        s2 = Store.load()
        assert s2.hidden_tokens == {(1, "0xaa"), (10, "0xbb")}


class TestCorruptConfig:
    def test_unreadable_json_still_returns_defaults(self, tmp_qeth):
        (tmp_qeth / "config.json").write_text("{ not valid json")
        s = Store.load()
        # Falls back to constructor defaults rather than crashing
        assert s.accounts == []
        assert s.default_account is None


class TestHeaderStates:
    def test_round_trip(self, tmp_qeth):
        s1 = Store()
        s1.set_header_state("tokens", "abcd1234")
        s1.set_header_state("transactions", "5678ef")
        s2 = Store.load()
        assert s2.get_header_state("tokens") == "abcd1234"
        assert s2.get_header_state("transactions") == "5678ef"

    def test_missing_key_returns_none(self, tmp_qeth):
        s = Store()
        assert s.get_header_state("tokens") is None

    def test_empty_state_forgets_the_entry(self, tmp_qeth):
        s = Store()
        s.set_header_state("tokens", "abcd")
        assert s.get_header_state("tokens") == "abcd"
        s.set_header_state("tokens", "")
        assert s.get_header_state("tokens") is None
        # And the JSON file no longer carries it either.
        s2 = Store.load()
        assert s2.get_header_state("tokens") is None
