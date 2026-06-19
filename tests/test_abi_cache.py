"""Tests for qeth.abi_cache — disk persistence for contract ABIs."""

import json
import time

from qeth.abi_cache import AbiCache, _NEGATIVE_TTL


ABI_SAMPLE = [
    {
        "type": "function",
        "name": "transfer",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]
ADDR = "0xdac17f958d2ee523a2206206994597c13d831ec7"


def test_load_returns_none_when_no_file(tmp_qeth):
    cache = AbiCache()
    assert cache.load(1, ADDR) is None


def test_save_load_round_trip_for_verified_abi(tmp_qeth):
    cache = AbiCache()
    cache.save(1, ADDR, ABI_SAMPLE)
    loaded = cache.load(1, ADDR)
    assert isinstance(loaded, list)
    assert loaded[0]["name"] == "transfer"


def test_save_load_round_trip_for_unverified_sentinel(tmp_qeth):
    cache = AbiCache()
    cache.save(1, ADDR, False)
    # A fresh sentinel comes back as the literal False — distinguishable
    # from None (cache miss), so callers skip refetching within the TTL.
    assert cache.load(1, ADDR) is False


def test_unverified_sentinel_expires_after_ttl(tmp_qeth):
    """A negative result older than the TTL reads back as None so the
    next access refetches — a contract verified since we cached the
    negative gets picked up instead of staying undecodable forever."""
    cache = AbiCache()
    p = cache._path(1, ADDR)
    p.parent.mkdir(parents=True, exist_ok=True)
    stale_ts = time.time() - _NEGATIVE_TTL - 1
    p.write_text(json.dumps({"unverified": True, "ts": stale_ts}))
    assert cache.load(1, ADDR) is None


def test_legacy_unverified_sentinel_without_ts_is_refetched(tmp_qeth):
    """The user's existing cache holds negatives written before the TTL
    landed — ``{"unverified": true}`` with no timestamp. Treat those as
    expired (None) so they're refetched once and migrated to the
    timestamped form on save."""
    cache = AbiCache()
    p = cache._path(1, ADDR)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"unverified": True}))   # legacy, no "ts"
    assert cache.load(1, ADDR) is None


def test_unverified_sentinel_does_not_clobber_a_real_abi(tmp_qeth):
    """Verified-ness is monotonic: a later "unverified" miss from a
    single-explorer resolver must not erase an ABI another source already
    cached. This is the write-write race that left freshly-decoded calls
    showing their raw 4-byte selector in the tx list — guard it at the
    store so no resolver ordering can trigger it."""
    cache = AbiCache()
    cache.save(1, ADDR, ABI_SAMPLE)
    cache.save(1, ADDR, False)          # a Blockscout-only miss lands later
    loaded = cache.load(1, ADDR)
    assert isinstance(loaded, list)
    assert loaded[0]["name"] == "transfer"


def test_real_abi_still_heals_an_unverified_sentinel(tmp_qeth):
    """The guard is one-directional — a real ABI *may* replace a negative
    sentinel (a contract verified since we cached the miss), it just can't
    be replaced *by* one."""
    cache = AbiCache()
    cache.save(1, ADDR, False)
    cache.save(1, ADDR, ABI_SAMPLE)
    assert cache.load(1, ADDR) == ABI_SAMPLE


def test_corrupt_file_returns_none(tmp_qeth):
    cache = AbiCache()
    p = cache._path(1, ADDR)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not valid json")
    assert cache.load(1, ADDR) is None


def test_address_lookup_is_case_insensitive(tmp_qeth):
    cache = AbiCache()
    cache.save(1, ADDR.upper(), ABI_SAMPLE)
    assert cache.load(1, ADDR.lower()) == ABI_SAMPLE


def test_chains_are_isolated(tmp_qeth):
    cache = AbiCache()
    cache.save(1, ADDR, ABI_SAMPLE)
    cache.save(10, ADDR, False)
    assert cache.load(1, ADDR) == ABI_SAMPLE
    assert cache.load(10, ADDR) is False


def test_proxy_stub_cached_entry_is_refetched(tmp_qeth):
    """The user's cache from before proxy resolution holds entries
    that are just the proxy's own admin ABI — useless for decoding
    real calls. ``load()`` returns None for those so the next access
    refetches with the proxy-aware path (which merges the impl)."""
    cache = AbiCache()
    # USDC-style proxy stub: admin / implementation / upgradeTo only.
    stub = [
        {"type": "function", "name": "admin",
         "stateMutability": "view", "inputs": [],
         "outputs": [{"name": "", "type": "address"}]},
        {"type": "function", "name": "implementation",
         "stateMutability": "view", "inputs": [],
         "outputs": [{"name": "", "type": "address"}]},
        {"type": "function", "name": "upgradeTo",
         "stateMutability": "nonpayable",
         "inputs": [{"name": "newImplementation", "type": "address"}],
         "outputs": []},
    ]
    cache.save(1, ADDR, stub)
    assert cache.load(1, ADDR) is None


def test_fallback_only_proxy_stub_is_refetched(tmp_qeth):
    """A pure-delegator proxy (TransparentUpgradeableProxy) exposes *no*
    functions — only a fallback — so it slips past a 'has functions'
    check and serves an undecodable, empty ABI. load() must return None
    so it refetches with proxy resolution."""
    cache = AbiCache()
    stub = [
        {"type": "constructor", "inputs": [
            {"name": "_logic", "type": "address"},
            {"name": "_data", "type": "bytes"}]},
        {"type": "event", "name": "Upgraded", "inputs": []},
        {"type": "fallback", "stateMutability": "payable"},
        {"type": "receive", "stateMutability": "payable"},
    ]
    cache.save(1, ADDR, stub)
    assert cache.load(1, ADDR) is None


def test_merged_proxy_abi_is_trusted(tmp_qeth):
    """After proxy resolution lands, the cached ABI has both the
    proxy's admin methods AND the implementation's surface — that
    has plenty of non-proxy functions and shouldn't be re-fetched."""
    cache = AbiCache()
    merged = [
        # proxy markers
        {"type": "function", "name": "admin", "inputs": [], "outputs": []},
        {"type": "function", "name": "upgradeTo", "inputs": [], "outputs": []},
        # implementation surface
        {"type": "function", "name": "transfer",
         "inputs": [{"name": "_to", "type": "address"},
                    {"name": "_value", "type": "uint256"}],
         "outputs": []},
        {"type": "function", "name": "balanceOf",
         "inputs": [{"name": "_owner", "type": "address"}],
         "outputs": []},
    ]
    cache.save(1, ADDR, merged)
    loaded = cache.load(1, ADDR)
    assert isinstance(loaded, list)
    names = {e.get("name") for e in loaded}
    assert "transfer" in names and "admin" in names


def test_legacy_bare_list_full_abi_kept(tmp_qeth):
    """Old-format entries that already hold a full non-proxy ABI
    (no proxy markers) stay cached — we only refetch ones the
    heuristic identifies as proxy stubs."""
    cache = AbiCache()
    p = cache._path(1, ADDR)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ABI_SAMPLE))   # legacy bare-list format
    loaded = cache.load(1, ADDR)
    assert loaded == ABI_SAMPLE
