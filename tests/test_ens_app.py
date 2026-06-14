"""ENS data layer (qeth.ens_app) — parsing, tree, expiry, contenthash, cache.
No network: the BENS HTTP call is injected."""

from qeth import ens_app as ea
from qeth.ens_app import EnsName


# --- EnsName shape ---------------------------------------------------------

def test_name_label_parent_subdomain():
    twold = EnsName("vitalik.eth")
    assert twold.label == "vitalik" and twold.parent is None
    assert not twold.is_subdomain
    sub = EnsName("alice.vitalik.eth")
    assert sub.label == "alice" and sub.parent == "vitalik.eth"
    assert sub.is_subdomain


# --- expiry ----------------------------------------------------------------

def test_expiry_status():
    DAY = 24 * 3600
    now = 1_000_000_000
    assert ea.expiry_status(None, now) == "none"                 # subdomain
    assert ea.expiry_status(now + 365 * DAY, now) == "active"
    assert ea.expiry_status(now + 5 * DAY, now) == "expiring"    # within warn
    assert ea.expiry_status(now - 5 * DAY, now) == "grace"       # expired, renewable
    assert ea.expiry_status(now - 200 * DAY, now) == "expired"   # past 90d grace


def _utc(y, mo, d, h, mi, s):
    from datetime import datetime, timezone
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


def test_iso_to_unix():
    assert ea._iso_to_unix("2048-03-27T13:25:30.000Z") == _utc(2048, 3, 27, 13, 25, 30)
    assert ea._iso_to_unix(None) is None
    assert ea._iso_to_unix("garbage") is None


# --- BENS parse + pagination ----------------------------------------------

def test_parse_name_item():
    n = ea._parse_name_item({
        "name": "vitalik.eth",
        "resolved_address": {"hash": "0xRES"},
        "owner": {"hash": "0xOWN"},
        "expiry_date": "2048-03-27T13:25:30.000Z",
    })
    assert n.name == "vitalik.eth" and n.resolved_address == "0xRES"
    assert n.owner == "0xOWN" and n.expiry_ts == _utc(2048, 3, 27, 13, 25, 30)
    assert ea._parse_name_item({"name": ""}) is None


def test_lookup_owned_names_paginates_and_dedupes():
    pages = {
        1: {"items": [{"name": "a.eth"}, {"name": "b.eth"}],
            "next_page_params": {"page_token": "tok2"}},
        2: {"items": [{"name": "b.eth"}, {"name": "c.eth"}],   # b dup
            "next_page_params": None},
    }

    def fake_get(url):
        return pages[2] if "page_token=tok2" in url else pages[1]

    names = ea.lookup_owned_names(1, "0xabc", get_json=fake_get)
    assert [n.name for n in names] == ["a.eth", "b.eth", "c.eth"]   # deduped


def test_lookup_tolerates_errors():
    def boom(url):
        raise RuntimeError("BENS down")
    assert ea.lookup_owned_names(1, "0xabc", get_json=boom) == []


# --- tree ------------------------------------------------------------------

def test_build_tree_nests_owned_subdomains():
    names = [EnsName("vitalik.eth"), EnsName("alice.vitalik.eth"),
             EnsName("bob.vitalik.eth"), EnsName("standalone.eth")]
    roots = ea.build_tree(names)
    assert [r.name.name for r in roots] == ["standalone.eth", "vitalik.eth"]
    vit = roots[1]
    assert [c.name.name for c in vit.children] == \
        ["alice.vitalik.eth", "bob.vitalik.eth"]
    # an orphan subdomain (parent not owned) becomes a root itself
    orphan = ea.build_tree([EnsName("x.notowned.eth")])
    assert [r.name.name for r in orphan] == ["x.notowned.eth"]


# --- contenthash -----------------------------------------------------------

def test_decode_contenthash():
    assert ea.decode_contenthash(None) is None
    assert ea.decode_contenthash("0x") is None
    # ipfs-ns (0xe301) + CIDv1 bytes -> ipfs://b<base32>
    ipfs = ea.decode_contenthash("0xe30101701220" + "ab" * 32)
    assert ipfs is not None and ipfs.startswith("ipfs://b")
    ipns = ea.decode_contenthash("0xe50101" + "cd" * 20)
    assert ipns is not None and ipns.startswith("ipns://b")
    # unknown codec -> raw marker (still shows something)
    assert ea.decode_contenthash("0xdeadbeef").startswith("contenthash:")


# --- cache -----------------------------------------------------------------

def test_ens_cache_round_trip(tmp_path):
    cache = ea.EnsCache(cache_dir=tmp_path)
    assert cache.load(1, "0xABC") is None
    names = [EnsName("vitalik.eth", resolved_address="0xRES", owner="0xOWN",
                     expiry_ts=123, source="owned"),
             EnsName("alice.vitalik.eth", source="custom")]
    cache.save(1, "0xABC", names)
    back = cache.load(1, "0xabc")
    assert back is not None and len(back) == 2
    assert back[0].name == "vitalik.eth" and back[0].expiry_ts == 123
    assert back[0].resolved_address == "0xRES"
    assert back[1].source == "custom"
