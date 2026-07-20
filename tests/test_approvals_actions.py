"""Approvals modify/revoke → request_transaction + reconcile (commit 2).

A fake host records the request_transaction call (no dialog, no chain); the
plugin's optimistic marks + the pair-scoped reconcile are driven directly.
"""

from types import SimpleNamespace

import pytest
from eth_utils import to_checksum_address
from PySide6.QtCore import QObject, Signal

from qeth import QULONGLONG
from qeth.plugins.approvals import ApprovalsPlugin, _format_allowance
from qeth.plugins.approvals.discovery import ApprovalRow

CHAIN = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
OWNER = "0x" + "a1" * 20
TOKEN = "0x" + "cc" * 20
SPENDER = "0x" + "ee" * 20
_MAX = (1 << 256) - 1
PAIR = (TOKEN.lower(), SPENDER.lower())


class _FakeIcons(QObject):
    icon_ready = Signal(QULONGLONG, str)

    def get(self, cid, contract):
        return None

    def request(self, cid, contract, url):
        pass


class _FakeHost:
    def __init__(self):
        self.selected_address = OWNER
        self.requests: list = []
        self.started: list = []
        self._icons = _FakeIcons()

    def current_chain(self):
        return CHAIN

    def request_transaction(self, req, chain, label, on_broadcast=None,
                            on_confirmed=None, on_cancel=None):
        self.requests.append(SimpleNamespace(
            req=req, chain=chain, label=label, on_broadcast=on_broadcast,
            on_confirmed=on_confirmed, on_cancel=on_cancel))

    def start_worker(self, worker):
        # Mirror MainWindow.start_worker: a finished worker is deleteLater()'d,
        # so a stale self._scan wrapper can outlive its C++ object.
        worker.finished.connect(worker.deleteLater)
        self.started.append(worker)

    def icon_cache(self):
        return self._icons

    def token_info(self, cid, address):
        return None

    def status_message(self, *a, **k):
        pass


@pytest.fixture
def plugin(qtbot, tmp_qeth):
    p = ApprovalsPlugin(SimpleNamespace(etherscan_api_key=None))
    p.host = _FakeHost()
    qtbot.addWidget(p.widget())
    p._loaded_for = (CHAIN.chain_id, OWNER.lower())    # match _current_view → _fresh
    return p


def _row(spender=SPENDER, allowance=_MAX, symbol="USDC", decimals=6):
    return ApprovalRow(token=TOKEN, spender=spender, allowance=allowance,
                       symbol=symbol, decimals=decimals)


def _approve_amount(data):
    return int(data[10:][64:128], 16)


def _approve_spender(data):
    return data[10:][24:64].lower()


def test_modify_builds_editable_approve_zero(plugin):
    row = _row()
    plugin._panel.append_rows([row])
    plugin._on_modify(row)
    assert len(plugin.host.requests) == 1
    rec = plugin.host.requests[0]
    req = rec.req
    assert req.to_addr == to_checksum_address(TOKEN)
    assert req.from_addr == to_checksum_address(OWNER)
    assert req.value_wei == 0
    assert req.data.lower().startswith("0x095ea7b3")
    assert _approve_spender(req.data) == SPENDER[2:].lower()
    assert _approve_amount(req.data) == 0
    assert "Modify" in rec.label


def test_revoke_builds_approve_zero(plugin):
    row = _row()
    plugin._panel.append_rows([row])
    plugin._on_revoke([row])
    rec = plugin.host.requests[0]
    assert _approve_amount(rec.req.data) == 0
    assert "Revoke" in rec.label


def test_broadcast_marks_leaf_pending(plugin):
    row = _row()
    plugin._panel.append_rows([row])
    plugin._on_revoke([row])
    plugin.host.requests[0].on_broadcast("0xhash")
    leaf = plugin._panel.tree.topLevelItem(0).child(0)
    assert leaf.text(1) == "pending…"
    assert leaf.isDisabled()


def test_confirm_schedules_and_runs_reconcile(plugin):
    row = _row()
    plugin._panel.append_rows([row])
    plugin._on_revoke([row])
    plugin.host.requests[0].on_confirmed({"status": 1})
    assert PAIR in plugin._reconcile_pending
    plugin._run_reconcile()
    assert plugin.host.started                          # a ReconcileWorker queued
    assert plugin._reconcile_pending == set()           # drained


def test_reconcile_zero_removes_leaf(plugin):
    plugin._panel.append_rows([_row()])
    plugin._on_reconciled(CHAIN.chain_id, OWNER.lower(), {PAIR: 0}, plugin._epoch)
    assert plugin._panel.tree.topLevelItemCount() == 0


def test_reconcile_nonzero_updates_leaf(plugin):
    plugin._panel.append_rows([_row(allowance=_MAX)])
    plugin._on_reconciled(CHAIN.chain_id, OWNER.lower(), {PAIR: 5_000_000},
                          plugin._epoch)
    leaf = plugin._panel.tree.topLevelItem(0).child(0)
    assert leaf.text(1) == _format_allowance(5_000_000, 6) == "5"    # no symbol
    assert not leaf.isDisabled()


def test_stale_epoch_reconcile_ignored(plugin):
    plugin._panel.append_rows([_row()])
    plugin._on_reconciled(CHAIN.chain_id, OWNER.lower(), {PAIR: 0},
                          plugin._epoch - 1)
    assert plugin._panel.tree.topLevelItemCount() == 1


def test_invalidate_clears_reconcile_queue(plugin):
    plugin._reconcile_pending.add(PAIR)
    plugin._reconcile_timer.start()
    plugin._invalidate()
    assert plugin._reconcile_pending == set()
    assert not plugin._reconcile_timer.isActive()


# --- commit 3: batch revoke via RevokeQueue -------------------------------

SP2 = "0x" + "dd" * 20


def _two_rows(plugin):
    r1, r2 = _row(spender=SPENDER), _row(spender=SP2)
    plugin._panel.append_rows([r1, r2])
    return r1, r2


def test_single_revoke_stays_direct(plugin):
    plugin._panel.append_rows([_row()])
    plugin._on_revoke([_row()])
    assert plugin._queue is None                      # no queue for one row
    assert "/" not in plugin.host.requests[0].label   # no "(k/N)" counter


def test_batch_revoke_opens_first_of_n(plugin):
    r1, r2 = _two_rows(plugin)
    plugin._on_revoke([r1, r2])
    assert plugin._queue is not None
    assert len(plugin.host.requests) == 1
    assert "(1/2)" in plugin.host.requests[0].label
    assert _approve_amount(plugin.host.requests[0].req.data) == 0


def test_batch_revoke_auto_advances_on_broadcast(plugin):
    r1, r2 = _two_rows(plugin)
    plugin._on_revoke([r1, r2])
    plugin.host.requests[0].on_broadcast("0xh0")      # advance to row 2
    assert len(plugin.host.requests) == 2
    assert "(2/2)" in plugin.host.requests[1].label
    plugin.host.requests[1].on_broadcast("0xh1")      # last one
    assert plugin._queue is None                      # finished + cleared


def test_batch_broadcast_marks_each_leaf_pending(plugin):
    r1, r2 = _two_rows(plugin)
    plugin._on_revoke([r1, r2])
    plugin.host.requests[0].on_broadcast("0xh0")
    assert plugin._panel._leaf_for(TOKEN, SPENDER).text(1) == "pending…"


def test_batch_revoke_cancel_aborts_chain(plugin):
    r1, r2 = _two_rows(plugin)
    plugin._on_revoke([r1, r2])
    plugin.host.requests[0].on_cancel()               # user dismissed dialog 1
    assert len(plugin.host.requests) == 1             # no second dialog opened
    assert plugin._queue is None


def test_invalidate_aborts_active_batch(plugin):
    r1, r2 = _two_rows(plugin)
    plugin._on_revoke([r1, r2])
    plugin._invalidate()                              # account/chain change mid-batch
    assert plugin._queue is None
    assert len(plugin.host.requests) == 1             # never advanced


# --- crash: _stop_scan on a host-deleted worker (account switch mid-scan) ---

def test_stop_scan_survives_a_deleted_worker(plugin):
    """Reproduce 'Internal C++ object (ScanWorker) already deleted': the host
    deleteLater()s a finished scan worker, then an account switch runs
    _invalidate → _stop_scan on the stale wrapper. Must not raise."""
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    from qeth.plugins.approvals import ScanWorker
    w = ScanWorker(CHAIN, OWNER, object(), [], object())
    plugin._scan = w                                   # pretend a scan is live
    w.deleteLater()
    QApplication.instance().sendPostedEvents(None, QEvent.Type.DeferredDelete)
    plugin._invalidate()                               # was: RuntimeError, aborting the switch
    assert plugin._scan is None


def test_finished_scan_forgets_worker_before_deletelater(plugin):
    plugin._loaded_for = None
    plugin._kick(force=True)                           # spawns + starts a worker
    w = plugin._scan
    assert w is not None and w in plugin.host.started
    w.finished.emit()                                  # host deleteLater()s it here
    assert plugin._scan is None                        # forgotten synchronously


# --- caching: instant re-open + incremental persist/prune -----------------

def test_cached_rows_render_without_cold_scan(plugin):
    plugin._cache.save(CHAIN.chain_id, OWNER, [_row()], 500)
    plugin._loaded_for = None
    plugin._kick(force=True)
    assert plugin._panel.tree.topLevelItemCount() == 1     # painted from cache
    assert plugin._panel._scan_bar.isHidden()              # no cold-scan progress bar


def test_kick_passes_cached_pairs_to_worker(plugin):
    plugin._cache.save(CHAIN.chain_id, OWNER, [_row(spender=SPENDER)], 500)
    plugin._loaded_for = None
    plugin._kick(force=True)
    assert plugin._scan is not None
    assert PAIR in plugin._scan._known_pairs           # worker re-checks cached pairs


def test_scan_done_prunes_and_persists(plugin):
    plugin._cache.save(CHAIN.chain_id, OWNER, [_row(spender=SPENDER)], 100)
    plugin._loaded_for = None
    plugin._kick(force=True)                            # renders the cached (old) pair
    other = "0x" + "cc" * 20
    plugin._on_rows(CHAIN.chain_id, OWNER.lower(), [_row(spender=other)], plugin._epoch)
    plugin._on_done(CHAIN.chain_id, OWNER.lower(), True, plugin._epoch)
    # old pair pruned (not re-confirmed), new one kept + persisted
    assert {r.spender for r in plugin._panel.all_rows()} == {other}
    loaded = plugin._cache.load(CHAIN.chain_id, OWNER)
    assert loaded is not None and {r.spender for r in loaded[0]} == {other}


def test_incomplete_scan_does_not_prune_or_persist(plugin):
    plugin._panel.append_rows([_row()])
    plugin._on_done(CHAIN.chain_id, OWNER.lower(), False, plugin._epoch)   # stopped
    assert plugin._panel.tree.topLevelItemCount() == 1     # nothing pruned
    assert plugin._cache.load(CHAIN.chain_id, OWNER) is None   # nothing persisted


def test_reconcile_zero_persists_the_removal(plugin):
    plugin._panel.append_rows([_row()])
    plugin._scan_pairs = {PAIR}
    plugin._on_reconciled(CHAIN.chain_id, OWNER.lower(), {PAIR: 0}, plugin._epoch)
    loaded = plugin._cache.load(CHAIN.chain_id, OWNER)
    assert loaded is not None and loaded[0] == []      # cache reflects the revoke


def test_stopped_scan_clears_loaded_for_to_resume(plugin):
    plugin._loaded_for = (CHAIN.chain_id, OWNER.lower())
    plugin._on_done(CHAIN.chain_id, OWNER.lower(), False, plugin._epoch)   # stopped
    assert plugin._loaded_for is None      # next activation resumes the un-scanned tail


def test_completed_scan_keeps_loaded_for(plugin):
    plugin._loaded_for = (CHAIN.chain_id, OWNER.lower())
    plugin._on_done(CHAIN.chain_id, OWNER.lower(), True, plugin._epoch)    # completed
    assert plugin._loaded_for == (CHAIN.chain_id, OWNER.lower())           # settled


# --- refresh when an approve confirms (any path) --------------------------

from qeth.plugins.approvals.discovery import _APPROVAL_TOPIC0  # noqa: E402


def _approval_receipt(owner, spender=SPENDER, token=TOKEN):
    return {"logs": [{"address": token, "topics": [
        _APPROVAL_TOPIC0, "0x" + "00" * 12 + owner[2:],
        "0x" + "00" * 12 + spender[2:]]}]}


def test_confirmed_new_approve_forces_scan(plugin):
    before = plugin._epoch                              # pair not shown yet
    plugin._on_tx_confirmed(CHAIN, "0xapprove", _approval_receipt(OWNER))
    assert plugin._epoch > before                       # full re-scan to discover it
    assert plugin._loaded_for == (CHAIN.chain_id, OWNER.lower())


def test_confirmed_approve_of_shown_pair_reconciles(plugin):
    plugin._panel.append_rows([_row(spender=SPENDER)])  # pair already on screen
    before = plugin._epoch
    plugin._on_tx_confirmed(CHAIN, "0xapprove", _approval_receipt(OWNER))
    assert plugin._epoch == before                      # no full scan
    assert PAIR in plugin._reconcile_pending            # targeted re-read scheduled


def test_confirmed_approve_for_other_account_ignored(plugin):
    plugin._panel.append_rows([_row(spender=SPENDER)])
    before = plugin._epoch
    plugin._on_tx_confirmed(CHAIN, "0xapprove", _approval_receipt("0x" + "99" * 20))
    assert plugin._epoch == before and plugin._reconcile_pending == set()


def test_confirmed_non_approve_does_not_refresh(plugin):
    plugin._panel.append_rows([_row(spender=SPENDER)])
    before = plugin._epoch
    plugin._on_tx_confirmed(CHAIN, "0xtransfer", {"logs": []})
    assert plugin._epoch == before and plugin._reconcile_pending == set()
