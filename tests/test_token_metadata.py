"""Tests for qeth.token_metadata."""

from qeth.token_metadata import TokenMetadataCache


def test_put_then_get(tmp_qeth):
    c = TokenMetadataCache()
    c.put_many(1, {
        "0xAAAA": {"symbol": "AAA", "name": "Aaa", "decimals": 18},
    })
    assert c.get(1, "0xaaaa") == {"symbol": "AAA", "name": "Aaa", "decimals": 18}


def test_get_is_case_insensitive(tmp_qeth):
    c = TokenMetadataCache()
    c.put_many(1, {"0xABCD": {"symbol": "X", "name": "X", "decimals": 18}})
    assert c.get(1, "0xabcd") is not None
    assert c.get(1, "0xABCD") is not None


def test_missing_returns_uncached_subset(tmp_qeth):
    c = TokenMetadataCache()
    c.put_many(1, {
        "0xAAAA": {"symbol": "A", "name": "A", "decimals": 18},
        "0xBBBB": {"symbol": "B", "name": "B", "decimals": 18},
    })
    assert c.missing(1, ["0xAAAA", "0xCCCC", "0xBBBB"]) == ["0xCCCC"]


def test_missing_preserves_input_casing(tmp_qeth):
    """missing() should return input addresses with whatever case the
    caller used — they get passed straight to the multicall."""
    c = TokenMetadataCache()
    out = c.missing(1, ["0xAaBb"])
    assert out == ["0xAaBb"]


def test_persisted_across_instances(tmp_qeth):
    c1 = TokenMetadataCache()
    c1.put_many(1, {"0xAA": {"symbol": "A", "name": "A", "decimals": 6}})

    c2 = TokenMetadataCache()
    assert c2.get(1, "0xaa") == {"symbol": "A", "name": "A", "decimals": 6}


def test_default_decimals_when_missing(tmp_qeth):
    c = TokenMetadataCache()
    # Caller can omit decimals; cache backfills the canonical 18.
    c.put_many(1, {"0xAA": {"symbol": "A", "name": "A"}})
    assert c.get(1, "0xAA")["decimals"] == 18


def test_unknown_chain_returns_none(tmp_qeth):
    c = TokenMetadataCache()
    assert c.get(99999, "0xAA") is None
