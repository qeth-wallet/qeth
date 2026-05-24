"""Tests for qeth.transactions_cache — disk persistence for past txs."""

from qeth.transactions import Transaction
from qeth.transactions_cache import TransactionCache


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
