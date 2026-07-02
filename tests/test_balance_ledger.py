"""Unit tests for BalanceLedger — the block-ordered balance writer that owns
the per-(chain, account, asset) freshness stamps and the ordered cache write.

These pin the invariant directly (the plugin exercises the same code through
_persist_targeted_balances / _record_nonzero_block delegates)."""
from types import SimpleNamespace

from qeth.balance_ledger import BalanceLedger
from qeth.wallet_cache import WalletCache

CHAIN = SimpleNamespace(chain_id=1)
ACC = "0x" + "ab" * 20
TOK = "0x" + "cd" * 20


class _Lists:
    """Minimal token_lists / token_metadata stub: knows one token's metadata."""
    def __init__(self, known: dict | None = None):
        self._known = known or {}

    def get(self, chain_id, contract):
        return self._known.get(contract.lower())


def _ledger(tmp_path, *, meta=None):
    cache = WalletCache(cache_dir=tmp_path)
    unpriced: dict = {}
    # token_lists.get → a TokenListEntry-like (has .symbol/.name/.decimals/
    # .logo_uri); token_metadata.get → {symbol,name,decimals}. Only the
    # metadata path is used when both are present.
    md = {} if meta is None else {TOK.lower(): meta}
    ledger = BalanceLedger(lambda: cache, _Lists(), _Lists(md), unpriced)
    return ledger, cache, unpriced


def _meta():
    return {"symbol": "TOK", "name": "Token", "decimals": 18}


def test_apply_read_writes_absolute_native_and_token(tmp_path):
    ledger, cache, _ = _ledger(tmp_path, meta=_meta())
    ledger.apply_read(CHAIN, ACC, 5 * 10**18, {TOK: 7 * 10**18}, block=10)
    w = cache.load(1, ACC)
    assert w.native_balance_wei == 5 * 10**18
    assert {t.contract: t.balance_raw for t in w.tokens} == {TOK.lower(): 7 * 10**18}


def test_stale_token_read_is_ignored(tmp_path):
    ledger, cache, _ = _ledger(tmp_path, meta=_meta())
    ledger.apply_read(CHAIN, ACC, 0, {TOK: 100}, block=10)
    ledger.apply_read(CHAIN, ACC, 0, {TOK: 999}, block=5)   # older → ignored
    assert cache.load(1, ACC).tokens[0].balance_raw == 100
    ledger.apply_read(CHAIN, ACC, 0, {TOK: 999}, block=11)  # newer → applies
    assert cache.load(1, ACC).tokens[0].balance_raw == 999


def test_authoritative_zero_drops_the_row(tmp_path):
    ledger, cache, _ = _ledger(tmp_path, meta=_meta())
    ledger.apply_read(CHAIN, ACC, 0, {TOK: 100}, block=10)
    ledger.apply_read(CHAIN, ACC, 0, {TOK: 0}, block=11)    # fresh zero → drop
    assert cache.load(1, ACC).tokens == []


def test_native_is_per_account_block_ordered(tmp_path):
    ledger, cache, _ = _ledger(tmp_path)
    ledger.apply_read(CHAIN, ACC, 5 * 10**18, {}, block=10)
    ledger.apply_read(CHAIN, ACC, 1 * 10**18, {}, block=5)  # stale → no regress
    assert cache.load(1, ACC).native_balance_wei == 5 * 10**18


def test_note_nonzero_blocks_a_stale_zero_drop(tmp_path):
    ledger, cache, _ = _ledger(tmp_path, meta=_meta())
    ledger.apply_read(CHAIN, ACC, 0, {TOK: 100}, block=10)
    ledger.note_nonzero(1, ACC, TOK, 20)          # known non-zero as of 20
    ledger.apply_read(CHAIN, ACC, 0, {TOK: 0}, block=15)   # older zero → ignored
    assert cache.load(1, ACC).tokens[0].balance_raw == 100
    ledger.apply_read(CHAIN, ACC, 0, {TOK: 0}, block=21)   # newer zero → drops
    assert cache.load(1, ACC).tokens == []


def test_uncached_token_needs_metadata_and_nonzero(tmp_path):
    ledger, cache, _ = _ledger(tmp_path, meta=None)  # no metadata for TOK
    ledger.apply_read(CHAIN, ACC, 0, {TOK: 100}, block=10)
    assert cache.load(1, ACC).tokens == []           # unknown token skipped
    ledger2, cache2, _ = _ledger(tmp_path / "b", meta=_meta())
    ledger2.apply_read(CHAIN, ACC, 0, {TOK: 0}, block=10)
    assert cache2.load(1, ACC) is None or cache2.load(1, ACC).tokens == []


def test_block_none_read_never_stale_and_carries_no_order(tmp_path):
    ledger, cache, _ = _ledger(tmp_path, meta=_meta())
    ledger.apply_read(CHAIN, ACC, 0, {TOK: 50}, block=None)
    assert cache.load(1, ACC).tokens[0].balance_raw == 50
    assert not ledger.is_token_stale(1, ACC, TOK, None)


def test_reused_token_resets_unpriced_grace(tmp_path):
    ledger, cache, unpriced = _ledger(tmp_path, meta=_meta())
    unpriced[(1, TOK.lower())] = 123.0        # a past grace timestamp
    ledger.apply_read(CHAIN, ACC, 0, {TOK: 5}, block=10)  # (re-)received
    assert (1, TOK.lower()) not in unpriced   # grace window restarted


def test_getter_tracks_a_swapped_cache(tmp_path):
    """The ledger reads the cache through the getter, so a caller that swaps
    its WalletCache instance is seen (the plugin/tests do this)."""
    box = {"cache": WalletCache(cache_dir=tmp_path)}
    ledger = BalanceLedger(lambda: box["cache"], _Lists(), _Lists({TOK.lower(): _meta()}), {})
    box["cache"] = WalletCache(cache_dir=tmp_path / "swapped")
    ledger.apply_read(CHAIN, ACC, 9, {TOK: 3}, block=1)
    assert box["cache"].load(1, ACC).native_balance_wei == 9
