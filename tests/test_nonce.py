"""Nonce selection: the suggested nonce comes from OUR own broadcast tracking
(pending_nonce_floor) maxed with the MINED ("latest") count — never the node's
flaky "pending" count. So back-to-back sends get fresh, increasing nonces even
while the previous tx is still pending (qeth is the sole signer for the account,
so its broadcast record is authoritative).

The floor scans the tx CACHE directly (not the ws live snapshot), so it holds
with QETH_LIVE_WS=0, and it counts our confirmed-in-cache txs as well as pending
ones — a sibling composer's tx that mined and flipped to confirmed before this
one is signed still bumps the nonce. Only dropped entries (nonce freed) are
excluded."""
from qeth.chains import Chain
from qeth.plugins.transactions import GasSuggestionWorker, TransactionsPlugin
from qeth.signing import SigningRequest
from qeth.transactions import Transaction

ADDR = "0x" + "11" * 20
OTHER = "0x" + "22" * 20


def _tx(block, frm=ADDR, *, nonce=0, pending=False, dropped=False):
    return Transaction(
        chain_id=1, hash="0x" + "ab" * 32, block_number=block,
        timestamp=0, nonce=nonce, from_addr=frm, to_addr=OTHER, value_wei=0,
        gas_used=0, gas_price_wei=0, method_id="0x", input_data="0x",
        success=True, pending=pending, dropped=dropped,
    )


def _plugin_with_cache(cache):
    """``cache`` is either a list of txs (keyed under (1, ADDR)) or a full
    ``{(chain_id, addr): [tx, ...]}`` dict."""
    plug = TransactionsPlugin.__new__(TransactionsPlugin)
    plug._cache = cache if isinstance(cache, dict) else {(1, ADDR): cache}
    return plug


# --- pending_nonce_floor: our own broadcast/mined tracking ------------------


def test_floor_is_none_with_nothing_tracked():
    assert _plugin_with_cache([]).pending_nonce_floor(1, ADDR) is None


def test_floor_is_highest_pending_plus_one():
    txs = [
        _tx(0, nonce=5, pending=True),
        _tx(0, nonce=7, pending=True),   # highest
        _tx(0, nonce=6, pending=True),
    ]
    assert _plugin_with_cache(txs).pending_nonce_floor(1, ADDR) == 8


def test_floor_counts_confirmed_not_just_pending():
    # The sibling-composer case: a tx we broadcast already mined and flipped to
    # confirmed (pending=False) before the second composer is signed. Its nonce
    # is still ours-and-spent, so the floor must bump past it.
    txs = [_tx(100, nonce=9)]   # confirmed, not pending
    assert _plugin_with_cache(txs).pending_nonce_floor(1, ADDR) == 10


def test_floor_ignores_other_senders():
    # Received txs live in our cache under our account key but carry the
    # sender's nonce — never ours.
    txs = [_tx(100, frm=OTHER, nonce=9)]
    assert _plugin_with_cache(txs).pending_nonce_floor(1, ADDR) is None


def test_floor_ignores_dropped():
    # A dropped tx freed its nonce for reuse — it must not raise the floor
    # (which would skip a nonce and stick the next tx behind an empty slot).
    txs = [_tx(0, nonce=4, pending=True, dropped=True)]
    assert _plugin_with_cache(txs).pending_nonce_floor(1, ADDR) is None


def test_floor_scans_across_account_keyed_entries():
    # A pending tx from ADDR may sit under a different account's cache view
    # (whoever was on screen when it was recorded); the floor scans every
    # entry for the chain and filters by from_addr.
    cache = {
        (1, OTHER): [_tx(0, nonce=11, pending=True)],   # ours, other view
        (1, ADDR): [_tx(100, nonce=3)],
    }
    assert _plugin_with_cache(cache).pending_nonce_floor(1, ADDR) == 12


def test_floor_is_per_chain():
    cache = {
        (1, ADDR): [_tx(0, nonce=5, pending=True)],
        (137, ADDR): [_tx(0, nonce=99, pending=True)],
    }
    assert _plugin_with_cache(cache).pending_nonce_floor(1, ADDR) == 6


def test_floor_needs_no_live_snapshot():
    # Regression for QETH_LIVE_WS=0: the floor never touches _live_snapshot, so
    # it works with the watcher disabled (the plugin here has no such attr).
    plug = _plugin_with_cache([_tx(0, nonce=2, pending=True)])
    assert not hasattr(plug, "_live_snapshot")
    assert plug.pending_nonce_floor(1, ADDR) == 3


# --- fork_floor_block: the verified-preview fork floor ----------------------
# A verified simulation forks a few blocks behind the head (proof convergence)
# but must NOT fork before this wallet's own latest tx, or it would hide a
# just-sent approval from the follow-up swap. This is where that floor comes
# from — and an in-flight (pending) sent tx demands the freshest state (head),
# since it may have just mined before our receipt watcher confirmed it.

from qeth.plugins.transactions import _FORK_FLOOR_HEAD


def test_fork_floor_is_none_with_no_history():
    assert _plugin_with_cache([]).fork_floor_block(1, ADDR) is None


def test_floor_is_highest_confirmed_we_sent():
    txs = [_tx(100), _tx(140), _tx(120)]
    assert _plugin_with_cache(txs).fork_floor_block(1, ADDR) == 140


def test_pending_sent_tx_forks_at_head():
    # The approve-then-swap case: the approval is still pending in our cache
    # (mined or not), so the floor demands head — never head-lag, which would
    # hide it. Regression for "approval invisible for ~30s".
    txs = [_tx(100), _tx(0, pending=True)]
    assert _plugin_with_cache(txs).fork_floor_block(1, ADDR) == _FORK_FLOOR_HEAD


def test_floor_ignores_pending_from_others_and_dropped():
    txs = [
        _tx(100),                          # ours, confirmed
        _tx(0, frm=OTHER, pending=True),   # someone else's pending → not ours
        _tx(0, pending=True, dropped=True),  # ours but dropped → not in-flight
    ]
    assert _plugin_with_cache(txs).fork_floor_block(1, ADDR) == 100


# --- GasSuggestionWorker: applies the floor over the MINED count -----------

class _StubClient:
    def __init__(self, chain):
        pass

    def estimate_gas(self, tx):
        return 21_000

    def gas_price(self):
        return 1_000_000_000

    def max_priority_fee(self):
        return 0

    def rpc(self, *a, **k):
        return {}

    def get_transaction_count(self, addr, block):
        # The whole point: we ask for MINED state, never "pending".
        assert block == "latest", f"queried {block!r}, expected 'latest'"
        return 3


def _run_worker(monkeypatch, floor):
    import qeth.plugins.transactions as tx
    monkeypatch.setattr(tx, "EthClient", _StubClient)
    chain = Chain("Polygon", 137, "https://x", eip1559=False)
    req = SigningRequest(
        chain_id=137, from_addr=ADDR, to_addr=ADDR, value_wei=0, data="0x",
    )
    worker = GasSuggestionWorker(chain, req, nonce_floor=floor)
    out: dict = {}
    worker.suggested.connect(lambda d: out.update(d))
    worker.run()
    return out


def test_worker_uses_mined_count_when_no_floor(qtbot, monkeypatch):
    assert _run_worker(monkeypatch, None)["nonce"] == 3


def test_worker_floors_above_mined(qtbot, monkeypatch):
    # We have nonce 7 in flight -> next must be 8, even though mined count is 3.
    assert _run_worker(monkeypatch, 8)["nonce"] == 8


def test_worker_floor_ignored_when_below_mined(qtbot, monkeypatch):
    assert _run_worker(monkeypatch, 2)["nonce"] == 3
