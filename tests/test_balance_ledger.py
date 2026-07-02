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
    lists, metadata = _Lists(), _Lists(md)
    ledger = BalanceLedger(lambda: cache, lambda: lists, lambda: metadata,
                           unpriced)
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


def test_apply_floor_credits_a_received_token(tmp_path):
    ledger, cache, _ = _ledger(tmp_path, meta=_meta())
    ledger.apply_floor(CHAIN, ACC, TOK, 10, 5 * 10**18)
    assert cache.load(1, ACC).tokens[0].balance_raw == 5 * 10**18


def test_apply_floor_skips_when_a_read_at_or_after_block_applied(tmp_path):
    """finding 5: the ws absolute read at the receipt block already reflects
    the receive, so the credit must not double-count — in EITHER order."""
    ledger, cache, _ = _ledger(tmp_path, meta=_meta())
    # read-first: absolute 150 at block 10, then the credit at block 10.
    ledger.apply_read(CHAIN, ACC, 0, {TOK: 150}, block=10)
    ledger.apply_floor(CHAIN, ACC, TOK, 10, 50)      # same block → skipped
    assert cache.load(1, ACC).tokens[0].balance_raw == 150
    ledger.apply_floor(CHAIN, ACC, TOK, 9, 50)       # older block → skipped
    assert cache.load(1, ACC).tokens[0].balance_raw == 150
    ledger.apply_floor(CHAIN, ACC, TOK, 11, 50)      # newer block → credits
    assert cache.load(1, ACC).tokens[0].balance_raw == 200


def test_apply_floor_is_idempotent_across_duplicate_confirms(tmp_path):
    """Two apply_floor calls for the same token at the same block credit ONCE
    (the first stamps the block; the second is skipped) — which is why the
    caller sums a receipt's per-token logs before calling."""
    ledger, cache, _ = _ledger(tmp_path, meta=_meta())
    ledger.apply_floor(CHAIN, ACC, TOK, 10, 5)
    ledger.apply_floor(CHAIN, ACC, TOK, 10, 5)       # duplicate → no double
    assert cache.load(1, ACC).tokens[0].balance_raw == 5


def test_apply_floor_blocks_a_later_stale_zero_drop(tmp_path):
    ledger, cache, _ = _ledger(tmp_path, meta=_meta())
    ledger.apply_floor(CHAIN, ACC, TOK, 20, 7)        # received at block 20
    ledger.apply_read(CHAIN, ACC, 0, {TOK: 0}, block=15)   # older zero → ignored
    assert cache.load(1, ACC).tokens[0].balance_raw == 7


def test_getters_track_swapped_token_sources(tmp_path):
    """Metadata lookups go through getters, so a caller that swaps its
    token_lists / token_metadata after construction is seen — the plugin's
    tests reassign _token_lists, and a stale ref meant apply_floor couldn't
    resolve metadata and silently dropped a just-received recognised token."""
    cache = WalletCache(cache_dir=tmp_path)
    src = {"lists": _Lists(), "meta": _Lists()}     # initially know nothing
    ledger = BalanceLedger(lambda: cache, lambda: src["lists"],
                           lambda: src["meta"], {})
    ledger.apply_floor(CHAIN, ACC, TOK, 10, 5)      # unknown token → skipped
    w = cache.load(1, ACC)
    assert w is None or w.tokens == []
    src["meta"] = _Lists({TOK.lower(): _meta()})    # swap in a knowing source
    ledger.apply_floor(CHAIN, ACC, TOK, 11, 5)      # now resolvable → added
    assert cache.load(1, ACC).tokens[0].balance_raw == 5


def test_getter_tracks_a_swapped_cache(tmp_path):
    """The ledger reads the cache through the getter, so a caller that swaps
    its WalletCache instance is seen (the plugin/tests do this)."""
    box = {"cache": WalletCache(cache_dir=tmp_path)}
    lists, metadata = _Lists(), _Lists({TOK.lower(): _meta()})
    ledger = BalanceLedger(
        lambda: box["cache"], lambda: lists, lambda: metadata, {})
    box["cache"] = WalletCache(cache_dir=tmp_path / "swapped")
    ledger.apply_read(CHAIN, ACC, 9, {TOK: 3}, block=1)
    assert box["cache"].load(1, ACC).native_balance_wei == 9
