"""Tests for qeth.store — config persistence + hide/show semantics."""

import os
import stat

from qeth.chains import DEFAULT_CHAINS
from qeth.store import Store, _merge_chain, ensure_private_root


class TestPrivateRoot:
    """~/.qeth must be owner-only (0700) so another local user can't traverse
    into the cache subtree and read wallet addresses / chain ids / metadata
    (issue #3). Mirrors the keystore dir's hardening."""

    def test_ensure_private_root_creates_owner_only(self, tmp_qeth):
        import qeth.store as store_mod
        # tmp_qeth points CONFIG_DIR at a fresh dir; make it a child that
        # doesn't exist yet so we test creation, not just chmod.
        root = tmp_qeth / "root"
        store_mod.CONFIG_DIR = root
        ensure_private_root()
        assert root.is_dir()
        assert stat.S_IMODE(root.stat().st_mode) == 0o700

    def test_ensure_private_root_tightens_a_loose_existing_dir(self, tmp_qeth):
        import qeth.store as store_mod
        root = tmp_qeth / "loose"
        root.mkdir()
        os.chmod(root, 0o755)            # an older build's default-umask dir
        store_mod.CONFIG_DIR = root
        ensure_private_root()
        assert stat.S_IMODE(root.stat().st_mode) == 0o700

    def test_save_hardens_the_root(self, tmp_qeth):
        # Store.save() goes through ensure_private_root, so a round-trip
        # leaves the config dir owner-only.
        Store().save()
        assert stat.S_IMODE(tmp_qeth.stat().st_mode) == 0o700


class TestRoundTrip:
    def test_notifications_enabled_defaults_on(self, tmp_qeth):
        assert Store().notifications_enabled is True
        assert Store.load().notifications_enabled is True

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
             "scheme": "Ledger Live", "tree": "0xaaa", "label": ""},
        ]
        s1.tree_labels = {"ledger/0xaaa": "0xAAA…"}
        s1.default_account = "0xAAA"
        s1.current_chain_id = 10
        s1.hide_token(1, "0xDeadBeef00000000000000000000000000000001")
        s1.force_show_token(10, "0xFEED00000000000000000000000000000000FEED")
        s1.window_geometry = "deadbeef"
        s1.splitter_state_main = "01020304"
        s1.notifications_enabled = False
        s1.add_custom_token(1, "0xCAFE00000000000000000000000000000000CAFE")
        s1.save()

        s2 = Store.load()
        assert s2.notifications_enabled is False
        assert s2.custom_tokens == {
            (1, "0xcafe00000000000000000000000000000000cafe")}
        assert s2.accounts == s1.accounts
        assert s2.tree_labels == s1.tree_labels
        assert s2.default_account == "0xAAA"
        assert s2.current_chain_id == 10
        assert s2.hidden_tokens == {(1, "0xdeadbeef00000000000000000000000000000001")}
        assert s2.shown_tokens == {(10, "0xfeed00000000000000000000000000000000feed")}
        assert s2.window_geometry == "deadbeef"
        assert s2.splitter_state_main == "01020304"


class TestSaveConcurrency:
    """save() is called from more than one thread (the GUI and the aiohttp
    RPC thread's add_chain). The snapshot must be decoupled from live state,
    and an out-of-order write must not regress the file."""

    def test_accounts_snapshot_isolated_from_later_mutation(self, tmp_qeth,
                                                             monkeypatch):
        """save() copies each account dict, so a set_label mutating a live dict
        after the snapshot (json.dumps runs outside the lock) can't leak into
        this write — nor raise 'dict changed size during iteration'."""
        import qeth.store as store_mod
        s = Store()
        s.accounts = [{"address": "0xAAA", "path": "", "source": "hot"}]
        real_dumps = store_mod.json.dumps

        def mutating_dumps(obj, **kw):
            s.accounts[0]["label"] = "renamed"   # concurrent set_label
            return real_dumps(obj, **kw)
        monkeypatch.setattr(store_mod.json, "dumps", mutating_dumps)

        s.save()   # must not raise
        assert "renamed" not in (tmp_qeth / "config.json").read_text()

    def test_stale_snapshot_write_is_dropped(self, tmp_qeth):
        """A lower-seq write reaching disk after a higher one is out of order
        and must be dropped, not allowed to regress the file."""
        s = Store()
        s.default_account = "0xAAA"
        s.save()                       # seq 1 → written
        # Pretend a newer snapshot (higher seq) already landed on disk.
        s._last_written_seq = 999
        s.default_account = "0xBBB"
        s.save()                       # seq 2 < 999 → dropped
        assert Store.load().default_account == "0xAAA"   # not regressed


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


class TestSetLabel:
    def test_updates_label_and_persists(self, tmp_qeth):
        s = Store()
        s.add_account({"address": "0xAA", "path": "x", "label": "old"})
        assert s.set_label("0xAA", "new") is True
        assert s.accounts[0]["label"] == "new"
        # Round-trips through disk.
        s2 = Store.load()
        assert s2.accounts[0]["label"] == "new"

    def test_case_insensitive_address_match(self, tmp_qeth):
        s = Store()
        s.add_account({"address": "0xAa", "path": "x"})
        assert s.set_label("0xaa", "hi") is True
        assert s.accounts[0]["label"] == "hi"

    def test_unknown_address_returns_false(self, tmp_qeth):
        s = Store()
        assert s.set_label("0xCC", "nope") is False

    def test_labels_every_record_holding_the_address(self, tmp_qeth):
        # Same address in two records (Ledger + watch-only) — distinct paths, so
        # add_account keeps both. The label must land on BOTH, not just the first.
        s = Store()
        s.add_account({"address": "0xAA", "path": "m/44'/60'/0'/0/0",
                       "source": "ledger", "label": ""})
        s.add_account({"address": "0xAA", "path": "",
                       "source": "watch_only", "label": ""})
        assert s.set_label("0xAA", "Main") is True
        assert [a["label"] for a in s.accounts] == ["Main", "Main"]


class TestReorderAccounts:
    def test_reorders_by_address_and_path_not_address_alone(self, tmp_qeth):
        # Two records share an address (distinct paths). Reordering must move the
        # exact record, not drag its same-address twin along.
        s = Store()
        s.add_account({"address": "0xAA", "path": "p1", "source": "ledger"})
        s.add_account({"address": "0xBB", "path": "p2", "source": "hot"})
        s.add_account({"address": "0xAA", "path": "p3", "source": "watch_only"})
        s.reorder_accounts([("0xAA", "p3"), ("0xAA", "p1"), ("0xBB", "p2")])
        assert [(a["address"], a["path"]) for a in s.accounts] == [
            ("0xAA", "p3"), ("0xAA", "p1"), ("0xBB", "p2")]

    def test_source_order_persists(self, tmp_qeth):
        s = Store()
        s.set_source_order(["qr", "ledger", "hot"])
        assert Store.load().account_source_order == ["qr", "ledger", "hot"]


def _led(addr, path, scheme="Ledger Live"):
    return {"address": addr, "path": path, "source": "ledger",
            "scheme": scheme, "label": ""}


def _qr(addr, path, xfp, scheme="BIP44 (…/0/i)"):
    return {"address": addr, "path": path, "source": "qr",
            "scheme": scheme, "xfp": xfp, "label": ""}


class TestTreeMigration:
    def test_ledger_one_scheme_is_one_tree(self, tmp_qeth):
        accts = [_led("0xAAA", "44'/60'/0'/0/0"), _led("0xBBB", "44'/60'/1'/0/0")]
        labels: dict = {}
        from qeth.store import _assign_tree_ids
        _assign_tree_ids(accts, labels)
        # Both accounts land in one tree, anchored on the first address.
        assert accts[0]["tree"] == "0xaaa"
        assert accts[1]["tree"] == "0xaaa"
        assert labels == {"ledger/0xaaa": "0xAAA…"}

    def test_qr_distinct_xfp_split_into_two_trees(self, tmp_qeth):
        accts = [_qr("0xAAA", "m/44'/60'/0'/0/0", "0x11111111"),
                 _qr("0xBBB", "m/44'/60'/0'/0/0", "0x22222222")]
        from qeth.store import _assign_tree_ids
        _assign_tree_ids(accts, {})
        assert accts[0]["tree"] == "0xaaa"
        assert accts[1]["tree"] == "0xbbb"      # different device → own tree

    def test_existing_label_not_overwritten(self, tmp_qeth):
        accts = [_led("0xAAA", "44'/60'/0'/0/0")]
        labels = {"ledger/0xaaa": "My Nano"}
        from qeth.store import _assign_tree_ids
        _assign_tree_ids(accts, labels)
        assert labels["ledger/0xaaa"] == "My Nano"

    def test_already_stamped_records_untouched(self, tmp_qeth):
        accts = [{"address": "0xAAA", "path": "p", "source": "ledger",
                  "scheme": "Ledger Live", "tree": "0xkept", "label": ""}]
        from qeth.store import _assign_tree_ids
        _assign_tree_ids(accts, {})
        assert accts[0]["tree"] == "0xkept"

    def test_migration_persists_and_is_idempotent(self, tmp_qeth):
        s = Store()
        s.accounts = [_led("0xAAA", "44'/60'/0'/0/0")]
        s.save()
        s2 = Store.load()                       # migration runs on load
        assert s2.accounts[0]["tree"] == "0xaaa"
        assert s2.tree_labels == {"ledger/0xaaa": "0xAAA…"}
        s2.save()
        s3 = Store.load()                       # second load must not change it
        assert s3.accounts == s2.accounts
        assert s3.tree_labels == s2.tree_labels


class TestTreeLabels:
    def test_set_tree_label_round_trips_and_noop(self, tmp_qeth):
        s = Store()
        assert s.set_tree_label("ledger/0xaaa", "Cold") is True
        assert s.set_tree_label("ledger/0xaaa", "Cold") is False   # no change
        assert Store.load().tree_labels == {"ledger/0xaaa": "Cold"}

    def test_remove_last_account_prunes_label_sibling_survives(self, tmp_qeth):
        s = Store()
        s.accounts = [
            {"address": "0xAAA", "path": "p0", "source": "ledger",
             "scheme": "Ledger Live", "tree": "0xaaa", "label": ""},
            {"address": "0xBBB", "path": "p1", "source": "ledger",
             "scheme": "Ledger Live", "tree": "0xbbb", "label": ""},
        ]
        s.tree_labels = {"ledger/0xaaa": "A", "ledger/0xbbb": "B"}
        s.remove_account("0xAAA", "p0")
        assert s.tree_labels == {"ledger/0xbbb": "B"}   # A pruned, B survives

    def test_default_tree_label_from_current_first_account(self, tmp_qeth):
        s = Store()
        s.accounts = [
            {"address": "0x425dEEEE", "path": "p", "source": "ledger",
             "scheme": "Ledger Live", "tree": "0xanchor", "label": ""},
        ]
        # Derived from the tree's current first account, not the id.
        assert s.default_tree_label("ledger/0xanchor") == "0x425d…"
        # Empty tree → falls back to the id (itself an address).
        assert s.default_tree_label("ledger/0xdead00") == "0xdead…"

    def test_ensure_tree_label_seeds_once(self, tmp_qeth):
        s = Store()
        s.accounts = [
            {"address": "0x425dEEEE", "path": "p", "source": "ledger",
             "scheme": "Ledger Live", "tree": "0x425deeee", "label": ""},
        ]
        s.ensure_tree_label("ledger", "0x425deeee")
        assert s.tree_labels["ledger/0x425deeee"] == "0x425d…"
        s.set_tree_label("ledger/0x425deeee", "Renamed")
        s.ensure_tree_label("ledger", "0x425deeee")     # must not clobber
        assert s.tree_labels["ledger/0x425deeee"] == "Renamed"


class TestResolveTree:
    def _store(self):
        s = Store()
        s.accounts = [
            _led("0xA1", "44'/60'/0'/0/0") | {"tree": "0xa1"},
            _led("0xA2", "44'/60'/1'/0/0") | {"tree": "0xa1"},
            _qr("0xQ1", "m/44'/60'/0'/0/0", "0x11111111") | {"tree": "0xq1"},
        ]
        return s

    def test_ledger_overlap_join(self, tmp_qeth):
        s = self._store()
        # Re-scan hits an existing (address, path) — even one not being re-added.
        tid = s.resolve_tree("ledger", "Ledger Live",
                             [("0xA2", "44'/60'/1'/0/0")])
        assert tid == "0xa1"

    def test_ledger_disjoint_is_new_tree(self, tmp_qeth):
        s = self._store()
        assert s.resolve_tree("ledger", "Ledger Live", [("0xZZ", "x")]) is None

    def test_qr_fingerprint_join(self, tmp_qeth):
        s = self._store()
        assert s.resolve_tree("qr", "BIP44 (…/0/i)", [],
                              xfp="0x11111111") == "0xq1"

    def test_qr_zero_xfp_falls_back_to_overlap(self, tmp_qeth):
        s = self._store()
        tid = s.resolve_tree("qr", "BIP44 (…/0/i)",
                             [("0xQ1", "m/44'/60'/0'/0/0")], xfp="0x00000000")
        assert tid == "0xq1"

    def test_scheme_mismatch_is_new_tree(self, tmp_qeth):
        s = self._store()
        # Same addresses, different scheme → a different tree.
        assert s.resolve_tree("ledger", "Legacy",
                             [("0xA1", "44'/60'/0'/0/0")]) is None


class TestSameAddressTwoSigners:
    """The same address held by two signers (Ledger + Air-gapped): routing and
    removal must act on the record, not just the address."""

    def _two(self):
        s = Store()
        s.add_account({"address": "0xAA", "path": "44'/60'/0'/5",
                       "source": "ledger", "label": ""})
        s.add_account({"address": "0xAA", "path": "m/44'/60'/0'/5",
                       "source": "qr", "label": ""})
        return s

    def test_account_for_signing_routes_by_record(self, tmp_qeth):
        s = self._two()
        # Exact (address, path) picks the right record.
        assert s.account_for_signing("0xAA", "m/44'/60'/0'/5")["source"] == "qr"
        assert s.account_for_signing("0xAA", "44'/60'/0'/5")["source"] == "ledger"
        # With no path, the connected default's remembered record wins.
        s.set_default_account("0xAA", "m/44'/60'/0'/5")
        assert s.account_for_signing("0xAA")["source"] == "qr"
        s.set_default_account("0xAA", "44'/60'/0'/5")
        assert s.account_for_signing("0xAA")["source"] == "ledger"

    def test_remove_one_record_keeps_the_twin(self, tmp_qeth):
        s = self._two()
        assert s.remove_account("0xAA", "44'/60'/0'/5") is True   # remove Ledger
        assert [(a["source"], a["path"]) for a in s.accounts] == [
            ("qr", "m/44'/60'/0'/5")]                             # QR survives

    def test_remove_connected_record_keeps_default_if_twin_remains(self, tmp_qeth):
        s = self._two()
        s.set_default_account("0xAA", "44'/60'/0'/5")     # connected via Ledger
        s.remove_account("0xAA", "44'/60'/0'/5")          # remove that record
        assert s.default_account == "0xAA"                # twin keeps it default
        assert s.default_account_path is None             # stale path dropped

    def test_remove_without_path_still_removes_all(self, tmp_qeth):
        s = self._two()
        assert s.remove_account("0xAA") is True
        assert all(a["address"] != "0xAA" for a in s.accounts)

    def test_noop_update_returns_false_and_does_not_resave(
        self, tmp_qeth, monkeypatch,
    ):
        """Setting the label to the same value it already has must
        not bump the config-file mtime — saves cost and the
        ``editingFinished`` signal can fire on focus-out without a
        real edit."""
        s = Store()
        s.add_account({"address": "0xAA", "path": "x", "label": "same"})
        from qeth import store as _store
        cfg = _store.CONFIG_FILE
        original_mtime = cfg.stat().st_mtime_ns
        # set_label with the same value: should report False.
        assert s.set_label("0xAA", "same") is False
        assert cfg.stat().st_mtime_ns == original_mtime


class TestHideShow:
    def test_hide_removes_from_shown(self, tmp_qeth):
        s = Store()
        s.force_show_token(1, "0xAAAA")
        s.hide_token(1, "0xAAAA")
        assert s.is_hidden(1, "0xAAAA")
        assert not s.is_force_shown(1, "0xAAAA")

    def test_custom_token_add_and_query(self, tmp_qeth):
        s = Store()
        s.add_custom_token(1, "0xAAAA")
        assert s.is_custom_token(1, "0xaaaa")
        assert not s.is_force_shown(1, "0xaaaa")   # tracked, NOT force-shown
        s.remove_custom_token(1, "0xAAAA")
        assert not s.is_custom_token(1, "0xaaaa")

    def test_add_custom_unhides(self, tmp_qeth):
        s = Store()
        s.hide_token(1, "0xBBBB")
        s.add_custom_token(1, "0xbbbb")
        assert s.is_custom_token(1, "0xbbbb")
        assert not s.is_hidden(1, "0xbbbb")

    def test_hide_stops_tracking_custom(self, tmp_qeth):
        s = Store()
        s.add_custom_token(1, "0xCCCC")
        s.hide_token(1, "0xcccc")
        assert s.is_hidden(1, "0xcccc")
        assert not s.is_custom_token(1, "0xcccc")   # hidden → no longer tracked

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


class TestDiscoveredTokens:
    def test_add_query_and_round_trip(self, tmp_qeth):
        s = Store()
        assert s.add_discovered_tokens(1, ["0xAAAA", "0xBBBB"]) is True
        assert s.is_discovered_token(1, "0xaaaa")
        assert not s.is_custom_token(1, "0xaaaa")   # kept separate from custom
        assert Store.load().discovered_tokens == {(1, "0xaaaa"), (1, "0xbbbb")}

    def test_add_is_noop_when_nothing_new(self, tmp_qeth):
        s = Store()
        assert s.add_discovered_tokens(1, ["0xAAAA"]) is True
        assert s.add_discovered_tokens(1, ["0xaaaa"]) is False   # already known

    def test_add_skips_hidden(self, tmp_qeth):
        s = Store()
        s.hide_token(1, "0xAAAA")
        assert s.add_discovered_tokens(1, ["0xAAAA", "0xBBBB"]) is True
        assert not s.is_discovered_token(1, "0xaaaa")   # hidden not resurrected
        assert s.is_discovered_token(1, "0xbbbb")

    def test_hide_evicts_from_discovered(self, tmp_qeth):
        s = Store()
        s.add_discovered_tokens(1, ["0xCCCC"])
        s.hide_token(1, "0xcccc")
        assert s.is_hidden(1, "0xcccc")
        assert not s.is_discovered_token(1, "0xcccc")


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

    def test_custom_rpc_drops_inherited_public_fallbacks(self, tmp_qeth):
        """A user pointing a shipped chain at their own node must NOT keep
        our public DRPC/publicnode fallbacks — reads would silently leak
        their address set there on a transient hiccup (issue #24). Both the
        read-fallbacks and the ws endpoints are cleared so only the user's
        node is used."""
        old = {
            "name": "Ethereum", "chain_id": 1,
            "rpc_url": "http://localhost:8545",
        }
        chain = _merge_chain(old)
        assert chain.rpc_url == "http://localhost:8545"
        assert chain.fallback_rpcs == ()
        assert chain.ws_url == ()

    def test_default_rpc_keeps_public_fallbacks(self, tmp_qeth):
        """The user who hasn't overridden the RPC still gets failover
        resilience — fallbacks are only dropped on an explicit override."""
        default_eth = next(d for d in DEFAULT_CHAINS if d.chain_id == 1)
        old = {
            "name": "Ethereum", "chain_id": 1,
            "rpc_url": default_eth.rpc_url,
        }
        chain = _merge_chain(old)
        assert chain.fallback_rpcs == default_eth.fallback_rpcs
        assert chain.ws_url == default_eth.ws_url

    def test_unknown_chain_falls_back_to_dataclass_default(self, tmp_qeth):
        unknown = {
            "name": "MyChain", "chain_id": 99999,
            "rpc_url": "x", "symbol": "MYC", "explorer": "",
        }
        chain = _merge_chain(unknown)
        assert chain.coingecko_id == "ethereum"  # Chain dataclass default

    def test_unknown_extra_fields_are_dropped(self, tmp_qeth):
        """Forward-compat: a future schema field should be silently
        ignored by an older client, not crash with TypeError. Use a
        custom chain_id (not in DEFAULT_CHAINS) so the persisted
        name carries through — shipped defaults intentionally win
        on metadata fields now, but unknown chains keep persisted
        values as-is."""
        c = {
            "name": "X", "chain_id": 424242, "rpc_url": "x",
            "symbol": "XYZ", "explorer": "",
            "future_field_we_dont_know_about": "ignored",
        }
        chain = _merge_chain(c)
        assert chain.name == "X"

    def test_shipped_chain_metadata_heals_from_default(self, tmp_qeth):
        """A persisted BNB entry that was added manually before we
        shipped chain 56 as a default would carry the wrong
        coingecko_id ("ethereum"), causing the native price lookup
        to return ETH's value instead of BNB's. Merging against
        the shipped default heals it without the user having to
        edit ~/.qeth/config.json by hand."""
        old = {
            "name": "BNB Smart Chain", "chain_id": 56,
            "rpc_url": "https://bsc.drpc.org",
            "symbol": "BNB",
            "explorer": "https://bscscan.com",
            "coingecko_id": "ethereum",   # the stale wrong value
        }
        chain = _merge_chain(old)
        assert chain.coingecko_id == "binancecoin"
        # User-edited RPC URL still survives.
        assert chain.rpc_url == "https://bsc.drpc.org"


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
