"""ENS data layer (qeth.plugins.ens.ens_app) — parsing, tree, expiry, contenthash, cache.
No network: the BENS HTTP call is injected."""

from qeth.plugins.ens import ens_app as ea
from qeth.plugins.ens.ens_app import EnsName


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
    # BENS expiry_date is grace-inclusive (nameExpires + 90d); the real .eth
    # registration expiry is 90 days earlier (what expiry_status expects).
    assert n.owner == "0xOWN"
    assert n.expiry_ts == _utc(2048, 3, 27, 13, 25, 30) - ea.GRACE_PERIOD_S
    # a non-.eth name has no grace period → date passes through unchanged
    dns = ea._parse_name_item(
        {"name": "foo.box", "expiry_date": "2048-03-27T13:25:30.000Z"})
    assert dns.expiry_ts == _utc(2048, 3, 27, 13, 25, 30)
    assert ea._parse_name_item({"name": ""}) is None
    # a cleared record reads back as the zero address → shown as "no address"
    cleared = ea._parse_name_item(
        {"name": "x.eth", "resolved_address": {"hash": ea.ZERO_ADDRESS}})
    assert cleared.resolved_address is None


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


def test_registrar_token_ids_filters_and_paginates():
    reg = ea.ENS_ETH_REGISTRAR
    other = "0x" + "ee" * 20
    pages = {
        1: {"items": [{"id": "11", "token": {"address": reg}},
                      {"id": "22", "token": {"address": other}}],   # not the registrar
            "next_page_params": {"page": "2"}},
        2: {"items": [{"id": "33", "token": {"address_hash": reg.lower()}}],
            "next_page_params": None},
    }

    def fake_get(url):
        return pages[2] if "page=2" in url else pages[1]

    ids = ea._registrar_token_ids("0xabc", get_json=fake_get)
    assert ids == [11, 33]              # the non-registrar token dropped


def test_lookup_registrant_names_skips_known_and_resolves():
    reg = ea.ENS_ETH_REGISTRAR
    # crv (the gap) + vitalik (already found via BENS → skipped).
    crv_id = int.from_bytes(ea._labelhash("crv"), "big")
    vit_id = int.from_bytes(ea._labelhash("vitalik"), "big")
    nft = {"items": [{"id": str(crv_id), "token": {"address": reg}},
                     {"id": str(vit_id), "token": {"address": reg}}],
           "next_page_params": None}

    calls = []

    def fake_get(url):
        if "/nft" in url:
            return nft
        calls.append(url)                # only the unknown tokenId resolves
        return {"name": "crv.eth", "attributes": [
            {"trait_type": "Expiration Date", "display_type": "date",
             "value": 1801533201000}]}

    names = ea.lookup_registrant_names(
        1, "0xabc", skip_labelhashes={vit_id}, get_json=fake_get)
    assert [n.name for n in names] == ["crv.eth"]
    assert names[0].source == "registrant"   # tagged so verify won't drop it
    assert names[0].expiry_ts == 1801533201  # carried so it shows + can renew
    assert len(calls) == 1 and str(crv_id) in calls[0]   # vitalik never fetched


def test_ens_metadata_parses_name_and_expiry():
    d = {"name": "crv.eth", "attributes": [
        {"trait_type": "Length", "value": 3},
        {"trait_type": "Expiration Date", "display_type": "date",
         "value": 1801533201000}]}
    assert ea._ens_metadata(1, get_json=lambda u: d) == ("crv.eth", 1801533201)
    # no expiry attribute → name only
    assert ea._ens_metadata(1, get_json=lambda u: {"name": "x.eth"}) \
        == ("x.eth", None)


def test_lookup_registrant_names_mainnet_only():
    def boom(url):
        raise AssertionError("should not hit the network off mainnet")
    assert ea.lookup_registrant_names(10, "0xabc", get_json=boom) == []


def test_lookup_registrant_names_tolerates_errors():
    def boom(url):
        raise RuntimeError("blockscout down")
    assert ea.lookup_registrant_names(1, "0xabc", get_json=boom) == []


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


def test_ens_cache_records_round_trip(tmp_path):
    cache = ea.EnsCache(cache_dir=tmp_path)
    assert cache.load_records(1, "vitalik.eth") is None
    rec = ea.EnsRecords(texts={"url": "https://vitalik.ca"},
                        contenthash="ipfs://bafy")
    cache.save_records(1, "Vitalik.eth", rec, 1234, verified=True)
    back = cache.load_records(1, "vitalik.eth")        # case-insensitive
    assert back is not None
    got, block, verified = back
    assert got.texts == {"url": "https://vitalik.ca"}
    assert got.contenthash == "ipfs://bafy" and block == 1234 and verified is True


# --- on-chain verification (namehash / decoders / multicall orchestration) --

def test_namehash_known_vectors():
    assert ea.namehash("") == b"\x00" * 32
    # EIP-137 reference value for "eth"
    assert ea.namehash("eth").hex() == (
        "93cdeb708b7545dc668eb9280176169d1c33cfd8ed6f04690a0bcc88a93fc4ae")


def test_name_warning_flags_confusables():
    zwj = "‍"
    # clean names (incl. valid emoji) → no warning
    assert ea.name_warning("vitalik.eth") is None
    assert ea.name_warning("\U0001F680.eth") is None          # 🚀.eth
    # invisible zero-width joiners between letters → flagged
    zname = "v" + zwj + "i" + zwj + "t" + zwj + "a" + zwj + "l" + zwj + "ik.eth"
    assert ea.name_warning(zname) is not None
    # mixed-script homoglyph (Cyrillic 'а' U+0430) → flagged
    assert ea.name_warning("vitаlik.eth") is not None


def test_is_eth_2ld():
    assert ea._is_eth_2ld("vitalik.eth")
    assert not ea._is_eth_2ld("blog.vitalik.eth")   # subdomain
    assert not ea._is_eth_2ld("foo.xyz")            # other TLD


def test_rent_price_sums_base_and_premium():
    from eth_abi import encode as abi_encode

    class _PriceClient:
        def __init__(self):
            self.to = None
            self.data = None

        def call(self, tx, block="latest"):
            self.to = tx["to"]
            self.data = tx["data"]
            return "0x" + abi_encode(["uint256", "uint256"], [1000, 7]).hex()

    cl = _PriceClient()
    price = ea.rent_price(None, "vitalik", ea_year := 31_536_000, client=cl)
    assert price == 1007                              # base + premium
    assert cl.to == ea.ENS_ETH_CONTROLLER             # the controller, not registry
    assert cl.data[2:10] == "83e7f6ff"                # rentPrice(string,uint256)
    # The label (not the full name) is encoded, with the duration.
    assert cl.data[10:] == abi_encode(
        ["string", "uint256"], ["vitalik", ea_year]).hex()


def test_rent_price_none_on_read_failure():
    class _BoomClient:
        def call(self, tx, block="latest"):
            raise RuntimeError("rpc down")

    assert ea.rent_price(None, "vitalik", 31_536_000, client=_BoomClient()) is None


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


def test_ownership_check_disowned_by():
    # different owner, read landed → disowned
    assert ea.OwnershipCheck(controller="0xOther", owner_known=True).disowned_by("0xme")
    # no owner (doesn't exist), read landed → disowned
    assert ea.OwnershipCheck(controller=None, owner_known=True).disowned_by("0xme")
    # read failed → NOT disowned (unknown, keep)
    assert not ea.OwnershipCheck(controller=None, owner_known=False).disowned_by("0xme")
    # you own it → never disowned
    assert not ea.OwnershipCheck(controller="0xme", owner_known=True).disowned_by("0xME")


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

    def block_number(self):
        # The records batch co-reads the height (mc.block_number()); a stub
        # value is enough for the callers that don't assert on it.
        return _FakePending(True, 1000)


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
        if target == ea.ENS_ETH_REGISTRAR and sel == ea._SEL_NAME_EXPIRES:
            return True, 1801533201             # nameExpires (a 2LD only)
        if target == RESOLVER and sel == ea._SEL_ADDR_COIN:
            return True, "0xReso1ved"
        return False, None        # legacy addr + everything else

    states, _block = ea._read_name_states(
        _FakeClient(resp), ["vitalik.eth", "blog.vitalik.eth"])
    vit = states["vitalik.eth"]
    assert vit.controller == "0xC0ntr0ller"
    assert vit.registrant == "0xReg1strant"     # 2LD → registrar queried
    assert vit.expiry == 1801533201             # ...and its nameExpires
    assert vit.resolved_address == "0xReso1ved"
    sub = states["blog.vitalik.eth"]
    assert sub.controller == "0xC0ntr0ller"
    assert sub.registrant is None               # subdomain → not an NFT
    assert sub.expiry is None                   # subdomain → no registration
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

    states, _block = ea._read_name_states(_FakeClient(resp), ["curvefi.eth"])
    st = states["curvefi.eth"]
    assert st.wrapped is True
    assert st.controller == REAL and st.registrant == REAL
    assert st.owned_by(REAL)


def test_read_name_states_record_exists_flags_legacy_name():
    # recordExists False → the name lives only in the old registry (owner()
    # falls back), so registry writes would revert: in_registry must read False.
    def resp(target, sel):
        if target == ea.ENS_REGISTRY and sel == ea._SEL_OWNER:
            return True, "0xLegacyOwner"
        if target == ea.ENS_REGISTRY and sel == ea._SEL_RECORD_EXISTS:
            return True, False
        return False, None

    states, _ = ea._read_name_states(_FakeClient(resp), ["legacy.eth"])
    assert states["legacy.eth"].controller == "0xLegacyOwner"
    assert states["legacy.eth"].in_registry is False


def test_read_name_states_record_exists_true_is_current():
    def resp(target, sel):
        if target == ea.ENS_REGISTRY and sel == ea._SEL_OWNER:
            return True, "0xOwner"
        if target == ea.ENS_REGISTRY and sel == ea._SEL_RECORD_EXISTS:
            return True, True
        return False, None

    states, _ = ea._read_name_states(_FakeClient(resp), ["new.eth"])
    assert states["new.eth"].in_registry is True


def test_read_name_states_record_exists_read_fail_defaults_true():
    # A transient recordExists read failure must not block removal of a normal
    # name — in_registry stays at its safe default (True).
    def resp(target, sel):
        if target == ea.ENS_REGISTRY and sel == ea._SEL_OWNER:
            return True, "0xOwner"
        return False, None                   # recordExists read didn't land

    states, _ = ea._read_name_states(_FakeClient(resp), ["glitch.eth"])
    assert states["glitch.eth"].in_registry is True


def test_read_records_via_client_batches():
    RESOLVER = "0x" + "ab" * 20

    def resp(target, sel):
        if target == ea.ENS_REGISTRY and sel == ea._SEL_RESOLVER:
            return True, RESOLVER
        if target == RESOLVER and sel == ea._SEL_TEXT:
            return True, "hello"
        if target == RESOLVER and sel == ea._SEL_CONTENTHASH:
            return True, "ipfs://bafy"
        return False, None

    rec, ok, ext, res, _blk = ea._read_records_via_client(
        _FakeClient(resp), "vitalik.eth")
    assert ok is True and ext is False
    assert rec.contenthash == "ipfs://bafy"
    assert rec.texts and all(v == "hello" for v in rec.texts.values())


def test_read_records_includes_eth_address():
    # The ETH address record is read at the chain head alongside text/content,
    # so a setAddr shows the moment it confirms (not only via the finalized
    # ownership pass). Keyed by ENSIP-9 coin type 60.
    RESOLVER = "0x" + "ab" * 20
    ADDR = "0x" + "cd" * 20

    def resp(target, sel):
        if target == ea.ENS_REGISTRY and sel == ea._SEL_RESOLVER:
            return True, RESOLVER
        if target == RESOLVER and sel == ea._SEL_ADDR:
            return True, ADDR
        return False, None

    rec, ok, ext, res, _blk = ea._read_records_via_client(
        _FakeClient(resp), "vitalik.eth")
    assert ok is True
    assert rec.addresses == {"60": ADDR}


def test_read_records_zero_address_is_absent():
    # addr(node) returning zero (decoder → None) leaves no address record, so
    # the UI can clear a name's resolution when it's set to 0x0.
    RESOLVER = "0x" + "ab" * 20

    def resp(target, sel):
        if target == ea.ENS_REGISTRY and sel == ea._SEL_RESOLVER:
            return True, RESOLVER
        if target == RESOLVER and sel == ea._SEL_TEXT:
            return True, "hi"
        return False, None              # addr (+ everything else) absent

    rec, ok, ext, res, _blk = ea._read_records_via_client(_FakeClient(resp), "x.eth")
    assert ok is True and rec.addresses == {}


def test_read_records_no_resolver_is_landed_empty():
    # registry.resolver read LANDS but returns zero → no records, ok=True.
    rec, ok, ext, res, _blk = ea._read_records_via_client(
        _FakeClient(lambda t, s: (True, None)), "x.eth")
    assert ok is True and rec.texts == {} and rec.contenthash is None


def test_read_records_resolver_glitch_is_not_ok():
    # resolver lookup read FAILED (multicall slot) → ok=False (don't trust it).
    rec, ok, ext, res, _blk = ea._read_records_via_client(
        _FakeClient(lambda t, s: (False, None)), "x.eth")
    assert ok is False and rec.texts == {}


def test_read_records_round2_glitch_is_not_ok():
    # resolver found, not extended, but the whole round-2 batch failed → ok=False.
    RESOLVER = "0x" + "ab" * 20

    def resp(target, sel):
        if sel == ea._SEL_RESOLVER:
            return True, RESOLVER
        return False, None                            # all text/content/supports fail

    rec, ok, ext, res, _blk = ea._read_records_via_client(_FakeClient(resp), "x.eth")
    assert ok is False and ext is False


def test_read_records_extended_resolver_is_ccip():
    # ExtendedResolver: supportsInterface True, on-chain text/content revert →
    # ok=True (expected), extended=True (caller follows CCIP).
    RESOLVER = "0x" + "ab" * 20

    def resp(target, sel):
        if sel == ea._SEL_RESOLVER:
            return True, RESOLVER
        if sel == ea._SEL_SUPPORTS:
            return True, 1                            # IExtendedResolver
        return False, None                            # text/content revert (CCIP)

    rec, ok, ext, res, _blk = ea._read_records_via_client(_FakeClient(resp), "uni.eth")
    assert ok is True and ext is True and res == RESOLVER
    assert rec.texts == {} and rec.contenthash is None


def test_read_records_ccip_follows_gateway():
    from eth_abi import encode as abi_encode

    class _FakeEth:
        def call(self, tx, block=None, ccip_read_enabled=None):
            from eth_abi import decode as abi_decode
            data = bytes.fromhex(tx["data"][2:])
            _dnsname, inner = abi_decode(["bytes", "bytes"], data[4:])
            if inner[:4] == ea._SEL_TEXT:
                return abi_encode(["bytes"], [abi_encode(["string"], ["gw-value"])])
            return abi_encode(["bytes"], [abi_encode(["bytes"], [b""])])

    class _FakeW3:
        eth = _FakeEth()

    rec = ea._read_records_ccip(_FakeW3(), "0x" + "ab" * 20, "uni.eth")
    assert rec.texts and all(v == "gw-value" for v in rec.texts.values())


def test_read_records_skips_resolver_lookup_when_supplied():
    RESOLVER = "0x" + "ab" * 20
    seen = []

    def resp(target, sel):
        seen.append(sel)
        if sel == ea._SEL_TEXT:
            return True, "hi"
        return False, None

    rec, ok, ext, res, _blk = ea._read_records_via_client(
        _FakeClient(resp), "vitalik.eth", resolver=RESOLVER)
    assert ok is True and rec.texts                   # records came back
    assert ea._SEL_RESOLVER not in seen               # round 1 was skipped


def test_read_records_self_heals_stale_resolver():
    # A pre-supplied (stale) resolver lands empty → read_records re-reads without
    # it, picks up the real resolver, and recovers the records.
    RIGHT = "0x" + "ab" * 20

    def resp(target, sel):
        if sel == ea._SEL_RESOLVER:
            return True, RIGHT
        if sel == ea._SEL_TEXT and target == RIGHT:
            return True, "hi"
        if sel == ea._SEL_CONTENTHASH:
            return True, None             # content call lands (empty) so ok=True
        return False, None                # stale resolver's text lands empty

    rec, ok, _blk = ea.read_records(object(), "x.eth",
                              client=_FakeClient(resp), resolver="0x" + "99" * 20)
    assert ok is True and rec.texts and all(v == "hi" for v in rec.texts.values())


def test_verified_read_records_onchain_extended_is_verified(monkeypatch):
    # An extended (CCIP) resolver that ALSO serves records on-chain → those
    # records are Helios-provable → verified ✓ (the base.eth case).
    monkeypatch.setattr("qeth.verified.verified_chain", lambda c, **k: object())
    monkeypatch.setattr("qeth.chain.EthClient", lambda c: object())
    rec = ea.EnsRecords(texts={"url": "base.org"})
    monkeypatch.setattr(ea, "_read_records_via_client",
                        lambda *a, **k: (rec, True, True, "0xR", 500))
    out, verified, blk = ea.verified_read_records(object(), "base.eth")
    assert verified is True and out.texts == {"url": "base.org"} and blk == 500


def test_verified_read_records_offchain_only_is_unverified(monkeypatch):
    # Extended resolver, nothing on-chain → records live offchain → not verified
    # (the gateway answer can't be proof-checked).
    monkeypatch.setattr("qeth.verified.verified_chain", lambda c, **k: object())
    monkeypatch.setattr("qeth.chain.EthClient", lambda c: object())
    monkeypatch.setattr(ea, "_read_records_via_client",
                        lambda *a, **k: (ea.EnsRecords(), True, True, "0xR", 500))
    _out, verified, _blk = ea.verified_read_records(object(), "uni.eth")
    assert verified is False


def test_verified_read_records_retries_transient(monkeypatch):
    monkeypatch.setattr("qeth.verified.verified_chain", lambda c, **k: object())
    monkeypatch.setattr("qeth.chain.EthClient", lambda c: object())
    monkeypatch.setattr(ea.time, "sleep", lambda s: None)
    rec = ea.EnsRecords(texts={"url": "x"})
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            return ea.EnsRecords(), False, False, None, None   # glitch
        return rec, True, False, "0xR", 500

    monkeypatch.setattr(ea, "_read_records_via_client", flaky)
    out, verified, _blk = ea.verified_read_records(object(), "vitalik.eth")
    assert verified is True and out.texts == {"url": "x"} and calls["n"] == 2


def test_verify_names_no_helios_returns_unverified(monkeypatch):
    # No sidecar → the verified-only path (fallback=False) yields nothing.
    monkeypatch.setattr("qeth.verified.verified_chain", lambda *a, **k: None)
    states, verified, _block = ea.verify_names(object(), ["vitalik.eth"])
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
        if calls["n"] < 2:                       # read didn't land (owner_known False)
            return {n.lower(): ea.OwnershipCheck() for n in names}, 100
        return {n.lower(): ea.OwnershipCheck(controller="0xC", owner_known=True)
                for n in names}, 100

    monkeypatch.setattr(ea, "_read_name_states", fake_read)
    states, verified, _block = ea.verify_names(object(), ["vitalik.eth"])
    assert verified is True
    assert states["vitalik.eth"].controller == "0xC"
    assert calls["n"] == 2                      # retried once, then succeeded


def test_verify_names_retries_partial_landing(monkeypatch):
    # One name lands, another doesn't (a blipped batch) → retry until BOTH land.
    _stub_sidecar(monkeypatch)
    calls = {"n": 0}

    def fake_read(client, names):
        calls["n"] += 1
        landed = calls["n"] >= 2
        return {
            "a.eth": ea.OwnershipCheck(controller="0xA", owner_known=True),
            "b.eth": ea.OwnershipCheck(controller="0xB", owner_known=landed),
        }, 100

    monkeypatch.setattr(ea, "_read_name_states", fake_read)
    states, _v, _block = ea.verify_names(object(), ["a.eth", "b.eth"])
    assert calls["n"] == 2                       # retried until b.eth landed
    assert all(st.owner_known for st in states.values())


def test_verify_names_gives_up_after_retries(monkeypatch):
    # Persistently empty → best-effort empty (UI leaves rows unbadged, no ⚠).
    _stub_sidecar(monkeypatch)
    monkeypatch.setattr(
        ea, "_read_name_states",
        lambda client, names: ({n.lower(): ea.OwnershipCheck() for n in names}, 100))
    states, verified, _block = ea.verify_names(object(), ["vitalik.eth"])
    assert verified is True
    assert states["vitalik.eth"].controller is None


# --- custom text-key discovery from tx history -----------------------------

def _mk_settext_tx(key, val="x"):
    from eth_abi import encode as abi_encode
    from qeth.transactions import Transaction
    body = abi_encode(["bytes32", "string", "string"], [b"\x00" * 32, key, val])
    return Transaction(
        chain_id=1, hash="0x" + "11" * 32, block_number=1, timestamp=0, nonce=0,
        from_addr="0xabc", to_addr="0xres", value_wei=0, gas_used=0,
        gas_price_wei=0, method_id="0x10f13a8c",
        input_data="0x10f13a8c" + body.hex(), success=True)


def test_discover_custom_text_keys_from_txs():
    from qeth.chains import DEFAULT_CHAINS
    from qeth.transactions import Transaction
    other = Transaction(
        chain_id=1, hash="0x" + "22" * 32, block_number=1, timestamp=0, nonce=0,
        from_addr="0xabc", to_addr="0xtok", value_wei=0, gas_used=0,
        gas_price_wei=0, method_id="0xa9059cbb", input_data="0xa9059cbb",
        success=True)
    txs = [_mk_settext_tx("lt"), _mk_settext_tx("market_id"),
           _mk_settext_tx("url"), other]          # url = standard; other != setText

    class FakeSource:
        def supports(self, chain):
            return True

        def list_transactions(self, chain, address, page=1, limit=50,
                              before_block=None):
            return txs if page == 1 else []

    ch = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
    keys = ea.discover_custom_text_keys(ch, "0xabc", source=FakeSource())
    assert keys == {"lt", "market_id"}            # custom only, standard excluded


def test_discover_custom_text_keys_unsupported_or_failing():
    from qeth.chains import DEFAULT_CHAINS
    ch = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)

    class NoSource:
        def supports(self, chain):
            return False

        def list_transactions(self, *a, **k):
            raise AssertionError("must not be called when unsupported")

    assert ea.discover_custom_text_keys(ch, "0xabc", source=NoSource()) == set()

    class BoomSource:
        def supports(self, chain):
            return True

        def list_transactions(self, *a, **k):
            raise RuntimeError("network down")

    assert ea.discover_custom_text_keys(ch, "0xabc", source=BoomSource()) == set()
