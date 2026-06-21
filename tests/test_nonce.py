"""Nonce selection: the suggested nonce comes from OUR own in-flight tracking
(pending_nonce_floor) maxed with the MINED ("latest") count — never the node's
flaky "pending" count. So back-to-back sends get fresh, increasing nonces even
while the previous tx is still pending (qeth is the sole signer for the account,
so its broadcast record is authoritative)."""
from qeth.chains import Chain
from qeth.live_watcher import PendingTx
from qeth.plugins.transactions import GasSuggestionWorker, TransactionsPlugin
from qeth.signing import SigningRequest

ADDR = "0x" + "11" * 20
OTHER = "0x" + "22" * 20


def _plugin_with_pending(pending):
    plug = TransactionsPlugin.__new__(TransactionsPlugin)
    plug._live_snapshot = {1: (Chain("Ethereum", 1, "https://x"), pending)}
    return plug


def test_floor_is_none_with_nothing_in_flight():
    assert _plugin_with_pending([]).pending_nonce_floor(1, ADDR) is None


def test_floor_is_highest_in_flight_plus_one():
    pending = [
        PendingTx("0xa", ADDR, 5, "0x"),
        PendingTx("0xb", ADDR, 7, "0x"),   # highest
        PendingTx("0xc", ADDR, 6, "0x"),
    ]
    assert _plugin_with_pending(pending).pending_nonce_floor(1, ADDR) == 8


def test_floor_ignores_other_senders():
    pending = [PendingTx("0xa", OTHER, 9, "0x")]
    assert _plugin_with_pending(pending).pending_nonce_floor(1, ADDR) is None


# --- fork_floor_block: the verified-preview fork floor ----------------------
# A verified simulation forks a few blocks behind the head (proof convergence)
# but must NOT fork before this wallet's own latest tx, or it would hide a
# just-sent approval from the follow-up swap. This is where that floor comes
# from — and an in-flight (pending) sent tx demands the freshest state (head),
# since it may have just mined before our receipt watcher confirmed it.

from qeth.plugins.transactions import _FORK_FLOOR_HEAD
from qeth.transactions import Transaction


def _tx(block, frm=ADDR, *, pending=False, dropped=False):
    return Transaction(
        chain_id=1, hash="0x" + "ab" * 32, block_number=block,
        timestamp=0, nonce=0, from_addr=frm, to_addr=OTHER, value_wei=0,
        gas_used=0, gas_price_wei=0, method_id="0x", input_data="0x",
        success=True, pending=pending, dropped=dropped,
    )


def _plugin_with_cache(txs):
    plug = TransactionsPlugin.__new__(TransactionsPlugin)
    plug._cache = {(1, ADDR): txs}
    return plug


def test_floor_is_none_with_no_history():
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
