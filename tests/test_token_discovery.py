"""discover_own_tokens joins the tx cache (origin) with the activity cache
(received-token legs) to find vault/LP tokens the user got from their own
transactions. All hermetic via tmp_qeth (which redirects both cache dirs)."""

from qeth.token_discovery import discover_own_tokens
from qeth.transactions import Transaction
from qeth.transactions_cache import TransactionCache
from qeth.activity_cache import ActivityCache
from qeth.tx_activity import Activity, AssetLeg

CID = 1
ME = "0x" + "11" * 20
ME2 = "0x" + "22" * 20
STRANGER = "0x" + "99" * 20
VAULT = "0x" + "a1" * 20
LP = "0x" + "b2" * 20


def _tx(h, frm, *, success=True, pending=False, dropped=False):
    return Transaction(
        chain_id=CID, hash=h, block_number=1, timestamp=1, nonce=1,
        from_addr=frm.lower(), to_addr=VAULT, value_wei=0, gas_used=1,
        gas_price_wei=1, method_id="0xdeadbeef", input_data="0x",
        success=success, pending=pending, dropped=dropped)


def _act(inn_contracts):
    inn = tuple(AssetLeg(symbol="x", contract=c) for c in inn_contracts)
    return Activity(verb="deposit", inn=inn)


def _seed(viewer, txs, acts):
    TransactionCache().save(CID, viewer, txs)
    if acts:
        ActivityCache().update(CID, viewer, acts)


def test_own_origin_received_token_is_found(tmp_qeth):
    _seed(ME, [_tx("0xh1", ME)], {"0xh1": _act([VAULT])})
    assert discover_own_tokens(CID, [ME]) == {VAULT.lower()}


def test_stranger_origin_is_ignored(tmp_qeth):
    # We received the token but did NOT originate the tx → spam-resistant drop.
    _seed(ME, [_tx("0xh1", STRANGER)], {"0xh1": _act([VAULT])})
    assert discover_own_tokens(CID, [ME]) == set()


def test_native_only_leg_is_skipped(tmp_qeth):
    _seed(ME, [_tx("0xh1", ME)], {"0xh1": _act([None])})   # received ETH only
    assert discover_own_tokens(CID, [ME]) == set()


def test_failed_pending_dropped_txs_excluded(tmp_qeth):
    _seed(ME, [
        _tx("0xf", ME, success=False),
        _tx("0xp", ME, pending=True),
        _tx("0xd", ME, dropped=True),
    ], {"0xf": _act([VAULT]), "0xp": _act([VAULT]), "0xd": _act([VAULT])})
    assert discover_own_tokens(CID, [ME]) == set()


def test_missing_activity_is_skipped(tmp_qeth):
    # Originated + succeeded, but the activity was never resolved (never viewed).
    _seed(ME, [_tx("0xh1", ME)], {})
    assert discover_own_tokens(CID, [ME]) == set()


def test_cross_account_send_is_found(tmp_qeth):
    # ME2 originates a tx that sends the vault token to ME. ME's tx cache holds
    # the incoming tx (from_addr = ME2, still one of ours); ME's activity has
    # the received leg. Discovered when scanning ME's caches.
    _seed(ME, [_tx("0xh1", ME2)], {"0xh1": _act([VAULT])})
    assert discover_own_tokens(CID, [ME, ME2]) == {VAULT.lower()}


def test_multiple_legs_and_case_normalized(tmp_qeth):
    _seed(ME, [_tx("0xh1", ME)],
          {"0xh1": _act([VAULT.upper(), LP])})
    assert discover_own_tokens(CID, [ME]) == {VAULT.lower(), LP.lower()}


def test_default_caches_are_used_when_omitted(tmp_qeth):
    # No injected caches → constructs TransactionCache()/ActivityCache() itself,
    # which tmp_qeth has redirected under tmp_path (the ACTIVITIES_DIR fix).
    _seed(ME, [_tx("0xh1", ME)], {"0xh1": _act([VAULT])})
    assert discover_own_tokens(CID, [ME]) == {VAULT.lower()}
