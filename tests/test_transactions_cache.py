"""Tests for qeth.transactions_cache — disk persistence for past txs."""

from qeth.transactions import Transaction
from qeth.transactions_cache import TransactionCache, merge_txs


def _tx(hash_suffix: str = "ab", **kw) -> Transaction:
    defaults = dict(
        chain_id=1,
        hash="0x" + hash_suffix * 32,
        block_number=25_000_000,
        timestamp=1_779_618_611,
        nonce=10,
        from_addr="0x7a16ff8270133f063aab6c9977183d9e72835428",
        to_addr="0xdac17f958d2ee523a2206206994597c13d831ec7",
        value_wei=0,
        gas_used=63_197,
        gas_price_wei=103_828_909,
        method_id="0xa9059cbb",
        input_data="0xa9059cbb",
        success=True,
    )
    defaults.update(kw)
    return Transaction(**defaults)


ADDR = "0x7a16ff8270133f063aab6c9977183d9e72835428"


def test_save_load_round_trip(tmp_qeth):
    cache = TransactionCache()
    original = [
        _tx("ab", block_number=25_000_002, nonce=12),
        _tx("cd", block_number=25_000_001, nonce=11),
        _tx("ef", block_number=25_000_000, nonce=10),
    ]
    cache.save(1, ADDR, original)
    loaded = cache.load(1, ADDR)
    assert loaded is not None
    assert len(loaded) == 3
    assert [t.hash for t in loaded] == [t.hash for t in original]
    assert [t.block_number for t in loaded] == [t.block_number for t in original]


def test_load_returns_none_when_no_file(tmp_qeth):
    cache = TransactionCache()
    assert cache.load(1, "0x0000000000000000000000000000000000000000") is None


def test_load_returns_none_for_corrupt_json(tmp_qeth):
    cache = TransactionCache()
    p = cache._path(1, ADDR)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not valid json")
    assert cache.load(1, ADDR) is None


def test_load_skips_unparseable_rows_keeps_rest(tmp_qeth):
    cache = TransactionCache()
    p = cache._path(1, ADDR)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Mix one good row with one schema-broken row (unknown field).
    p.write_text(
        '[{"chain_id": 1, "hash": "0xabcd", "block_number": 1,'
        ' "timestamp": 0, "nonce": 0, "from_addr": "", "to_addr": null,'
        ' "value_wei": 0, "gas_used": 0, "gas_price_wei": 0,'
        ' "method_id": "", "input_data": "0x", "success": true},'
        ' {"only": "garbage"}]'
    )
    loaded = cache.load(1, ADDR)
    assert loaded is not None
    assert len(loaded) == 1
    assert loaded[0].hash == "0xabcd"


def test_address_lowercased_on_disk_path(tmp_qeth):
    """Mixed-case input addresses resolve to the same file as their
    lowercased form — saving with one and loading with the other works."""
    cache = TransactionCache()
    cache.save(1, "0xABCDEF", [_tx("ab")])
    assert cache.load(1, "0xabcdef") is not None
    assert cache.load(1, "0xAbCdEf") is not None


def test_huge_wei_value_survives(tmp_qeth):
    """Wei values can exceed JS Number range; JSON should round-trip
    them as ints (the dataclass field is int)."""
    cache = TransactionCache()
    huge = 10**25
    cache.save(1, ADDR, [_tx("ab", value_wei=huge)])
    loaded = cache.load(1, ADDR)
    assert loaded[0].value_wei == huge


def test_chains_are_isolated(tmp_qeth):
    cache = TransactionCache()
    cache.save(1, ADDR, [_tx("aa")])
    cache.save(10, ADDR, [_tx("bb"), _tx("cc")])
    assert len(cache.load(1, ADDR)) == 1
    assert len(cache.load(10, ADDR)) == 2


# --- merge_txs -------------------------------------------------------------

class TestMergeTxs:
    def test_empty_inputs(self):
        assert merge_txs([], []) == []

    def test_only_new(self):
        a, b = _tx("aa", block_number=10), _tx("bb", block_number=9)
        assert merge_txs([a, b], []) == [a, b]

    def test_only_old(self):
        a, b = _tx("aa", block_number=10), _tx("bb", block_number=9)
        assert merge_txs([], [a, b]) == [a, b]

    def test_dedupes_by_hash(self):
        """A tx returned by both lists appears once. The version from
        ``new`` wins (so post-reorg corrections propagate cleanly)."""
        old_tx = _tx("aa", block_number=10, success=False)
        new_tx = _tx("aa", block_number=10, success=True)
        merged = merge_txs([new_tx], [old_tx])
        assert len(merged) == 1
        assert merged[0].success is True

    def test_extends_history_with_older_cached(self):
        """The whole point of merging: new fetch + older cached →
        union, so historical entries survive even after they fall out
        of the recent-N fetch window."""
        new = [_tx("a1", block_number=20),
               _tx("a2", block_number=19)]
        old = [_tx("o1", block_number=10),
               _tx("o2", block_number=5)]
        merged = merge_txs(new, old)
        hashes = [t.hash for t in merged]
        assert hashes == ["0x" + "a1" * 32, "0x" + "a2" * 32,
                          "0x" + "o1" * 32, "0x" + "o2" * 32]

    def test_interleaves_by_block(self):
        """If the new fetch's window starts above the cached one but
        they overlap on intermediate blocks, the result must still be
        sorted by block desc."""
        new = [_tx("n1", block_number=20),
               _tx("n2", block_number=15)]
        old = [_tx("o1", block_number=18),
               _tx("o2", block_number=12)]
        merged = merge_txs(new, old)
        assert [t.block_number for t in merged] == [20, 18, 15, 12]

    def test_intra_block_new_wins_via_stable_sort(self):
        """Within the same block, new fetch entries sort ahead of old
        cached entries (Python's stable sort preserves insertion order
        for equal keys; merge_txs concatenates new then old)."""
        n = _tx("nn", block_number=10)
        o = _tx("oo", block_number=10)
        merged = merge_txs([n], [o])
        assert merged == [n, o]
