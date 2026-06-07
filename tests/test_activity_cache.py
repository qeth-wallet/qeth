"""ActivityCache — persist resolved Activity objects so a chain's second
visit paints from disk instead of refetching."""

import json

from qeth.activity_cache import ActivityCache
from qeth.tx_activity import Activity, AssetLeg


def test_roundtrip_preserves_verb_coins_and_flags(tmp_path):
    c = ActivityCache(root=tmp_path)
    acts = {
        "0xaa": Activity(
            "deposit",
            (AssetLeg("USDC", "0x1"), AssetLeg("ETH", None)),
            (AssetLeg("WBTC", "0x2"),),
            show_arrow=True, muted=False),
        "0xbb": Activity("send", (), (), show_arrow=False, muted=True),
    }
    c.update(1, "0xABC", acts)
    # a fresh instance (cold memory) must read it back from disk
    got = ActivityCache(root=tmp_path).load(1, "0xabc")
    assert got["0xaa"].verb == "deposit"
    assert [(l.symbol, l.contract) for l in got["0xaa"].out] \
        == [("USDC", "0x1"), ("ETH", None)]
    assert got["0xaa"].inn[0].contract == "0x2"
    assert got["0xbb"].muted is True and got["0xbb"].show_arrow is False


def test_update_merges_across_calls(tmp_path):
    c = ActivityCache(root=tmp_path)
    c.update(1, "0xabc", {"0xaa": Activity("deposit")})
    c.update(1, "0xabc", {"0xbb": Activity("send")})
    assert set(ActivityCache(root=tmp_path).load(1, "0xabc")) == {"0xaa", "0xbb"}


def test_update_overwrites_same_hash(tmp_path):
    c = ActivityCache(root=tmp_path)
    c.update(1, "0xabc", {"0xaa": Activity("transfer")})
    c.update(1, "0xabc", {"0xaa": Activity("exchange",
                                           (AssetLeg("USDC", "0x1"),), ())})
    got = ActivityCache(root=tmp_path).load(1, "0xabc")
    assert got["0xaa"].verb == "exchange" and got["0xaa"].out[0].symbol == "USDC"


def test_address_is_case_insensitive(tmp_path):
    c = ActivityCache(root=tmp_path)
    c.update(1, "0xABCDEF", {"0xaa": Activity("deposit")})
    assert "0xaa" in c.load(1, "0xabcdef")


def test_missing_and_empty_are_safe(tmp_path):
    c = ActivityCache(root=tmp_path)
    assert c.load(1, "0xabc") == {}
    c.update(1, "0xabc", {})          # no-op, no file written
    assert c.load(1, "0xabc") == {}


def test_legacy_unversioned_cache_is_ignored(tmp_path):
    c = ActivityCache(root=tmp_path)
    f = c._file(1, "0xabc")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"0xaa": {"v": "deposit", "o": [], "i": []}}))
    assert c.load(1, "0xabc") == {}   # no _v → rebuilt, not pinned stale


def test_other_build_version_is_ignored(tmp_path):
    c = ActivityCache(root=tmp_path)
    f = c._file(1, "0xabc")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(
        {"_v": -999, "acts": {"0xaa": {"v": "deposit", "o": [], "i": []}}}))
    assert c.load(1, "0xabc") == {}
