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


def test_fetch_name_marks_custom():
    def fake_get(url):
        assert "domains/vitalik.eth" in url
        return {"name": "vitalik.eth", "resolved_address": {"hash": "0xRES"}}
    n = ea.fetch_name(1, "vitalik.eth", get_json=fake_get)
    assert n is not None and n.resolved_address == "0xRES"
    assert n.source == "custom"


def test_fetch_name_none_on_error():
    def boom(url):
        raise RuntimeError("404")
    assert ea.fetch_name(1, "nope.eth", get_json=boom) is None


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


# --- on-chain verification (namehash / decoders / multicall orchestration) --

def test_namehash_known_vectors():
    assert ea.namehash("") == b"\x00" * 32
    # EIP-137 reference value for "eth"
    assert ea.namehash("eth").hex() == (
        "93cdeb708b7545dc668eb9280176169d1c33cfd8ed6f04690a0bcc88a93fc4ae")


def test_is_eth_2ld():
    assert ea._is_eth_2ld("vitalik.eth")
    assert not ea._is_eth_2ld("blog.vitalik.eth")   # subdomain
    assert not ea._is_eth_2ld("foo.xyz")            # other TLD


def test_decode_addr_word():
    addr = "d8da6bf26964af9d7eed9e03e53415d37aa96045"
    word = b"\x00" * 12 + bytes.fromhex(addr)
    assert ea._decode_addr_word(word).lower() == "0x" + addr
    assert ea._decode_addr_word(b"\x00" * 32) is None     # zero address
    assert ea._decode_addr_word(b"\x00" * 10) is None     # too short


def test_decode_addr_bytes():
    addr = "d8da6bf26964af9d7eed9e03e53415d37aa96045"
    # ABI `bytes` return: offset(0x20), length(0x14=20), payload padded to 32
    blob = ((32).to_bytes(32, "big") + (20).to_bytes(32, "big")
            + bytes.fromhex(addr) + b"\x00" * 12)
    assert ea._decode_addr_bytes(blob).lower() == "0x" + addr
    empty = (32).to_bytes(32, "big") + (0).to_bytes(32, "big")
    assert ea._decode_addr_bytes(empty) is None


def test_ownership_check_owned_by():
    st = ea.OwnershipCheck(controller="0xAaA", registrant="0xBbB")
    assert st.owned_by("0xaaa") and st.owned_by("0xBBB")
    assert not st.owned_by("0xccc")


class _FakePending:
    def __init__(self, success, value):
        self.success, self.value = success, value


class _FakeMC:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def add(self, target, data, *, decoder=None):
        ok, val = self._resp(target, data[:4])
        return _FakePending(ok, val)


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    def multicall(self, **kw):
        return _FakeMC(self._resp)


def test_read_name_states_orchestration():
    RESOLVER = "0x4976fb03C32e5B8cfe2b6cCB31c09Ba78EBaBa41"

    def resp(target, sel):
        if target == ea.ENS_REGISTRY and sel == ea._SEL_OWNER:
            return True, "0xC0ntr0ller"
        if target == ea.ENS_REGISTRY and sel == ea._SEL_RESOLVER:
            return True, RESOLVER
        if target == ea.ENS_ETH_REGISTRAR and sel == ea._SEL_OWNER_OF:
            return True, "0xReg1strant"
        if target == RESOLVER and sel == ea._SEL_ADDR_COIN:
            return True, "0xReso1ved"
        return False, None        # legacy addr + everything else

    states = ea._read_name_states(
        _FakeClient(resp), ["vitalik.eth", "blog.vitalik.eth"])
    vit = states["vitalik.eth"]
    assert vit.controller == "0xC0ntr0ller"
    assert vit.registrant == "0xReg1strant"     # 2LD → registrar queried
    assert vit.resolved_address == "0xReso1ved"
    sub = states["blog.vitalik.eth"]
    assert sub.controller == "0xC0ntr0ller"
    assert sub.registrant is None               # subdomain → not an NFT
    assert sub.resolved_address == "0xReso1ved"


def test_read_name_states_unwraps_namewrapper():
    # Wrapped name: registry.owner and registrar.ownerOf both point at the
    # NameWrapper; the real owner is NameWrapper.ownerOf(node).
    REAL = "0xReal0wnerReal0wnerReal0wnerReal0wner00"

    def resp(target, sel):
        if target == ea.ENS_NAME_WRAPPER and sel == ea._SEL_OWNER_OF:
            return True, REAL
        if target == ea.ENS_REGISTRY and sel == ea._SEL_OWNER:
            return True, ea.ENS_NAME_WRAPPER
        if target == ea.ENS_ETH_REGISTRAR and sel == ea._SEL_OWNER_OF:
            return True, ea.ENS_NAME_WRAPPER
        return False, None

    states = ea._read_name_states(_FakeClient(resp), ["curvefi.eth"])
    st = states["curvefi.eth"]
    assert st.wrapped is True
    assert st.controller == REAL and st.registrant == REAL
    assert st.owned_by(REAL)


def test_verify_names_no_helios_returns_unverified(monkeypatch):
    # No sidecar → the verified-only path (fallback=False) yields nothing.
    monkeypatch.setattr("qeth.verified.verified_chain", lambda *a, **k: None)
    states, verified = ea.verify_names(object(), ["vitalik.eth"])
    assert states == {} and verified is False


def _stub_sidecar(monkeypatch):
    """A ready (fake) Helios sidecar + EthClient, no sleeps."""
    monkeypatch.setattr("qeth.verified.verified_chain", lambda c, **k: object())
    monkeypatch.setattr("qeth.chain.EthClient", lambda c: object())
    monkeypatch.setattr(ea.time, "sleep", lambda s: None)


def test_verify_names_retries_transient_empty(monkeypatch):
    # First read comes back empty (post-sync transient), second has data.
    _stub_sidecar(monkeypatch)
    calls = {"n": 0}

    def fake_read(client, names):
        calls["n"] += 1
        if calls["n"] < 2:
            return {n.lower(): ea.OwnershipCheck() for n in names}
        return {n.lower(): ea.OwnershipCheck(controller="0xC") for n in names}

    monkeypatch.setattr(ea, "_read_name_states", fake_read)
    states, verified = ea.verify_names(object(), ["vitalik.eth"])
    assert verified is True
    assert states["vitalik.eth"].controller == "0xC"
    assert calls["n"] == 2                      # retried once, then succeeded


def test_verify_names_gives_up_after_retries(monkeypatch):
    # Persistently empty → best-effort empty (UI leaves rows unbadged, no ⚠).
    _stub_sidecar(monkeypatch)
    monkeypatch.setattr(
        ea, "_read_name_states",
        lambda client, names: {n.lower(): ea.OwnershipCheck() for n in names})
    states, verified = ea.verify_names(object(), ["vitalik.eth"])
    assert verified is True
    assert states["vitalik.eth"].controller is None
