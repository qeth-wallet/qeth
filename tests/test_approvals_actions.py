"""Approvals modify/revoke → request_transaction + reconcile (commit 2).

A fake host records the request_transaction call (no dialog, no chain); the
plugin's optimistic marks + the pair-scoped reconcile are driven directly.
"""

from types import SimpleNamespace

import pytest
from eth_utils import to_checksum_address

from qeth.plugins.approvals import ApprovalsPlugin
from qeth.plugins.approvals.discovery import ApprovalRow
from qeth.plugins.transactions import _format_token_amount

CHAIN = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
OWNER = "0x" + "a1" * 20
TOKEN = "0x" + "cc" * 20
SPENDER = "0x" + "ee" * 20
_MAX = (1 << 256) - 1
PAIR = (TOKEN.lower(), SPENDER.lower())


class _FakeHost:
    def __init__(self):
        self.selected_address = OWNER
        self.requests: list = []
        self.started: list = []

    def current_chain(self):
        return CHAIN

    def request_transaction(self, req, chain, label, on_broadcast=None,
                            on_confirmed=None, on_cancel=None):
        self.requests.append(SimpleNamespace(
            req=req, chain=chain, label=label, on_broadcast=on_broadcast,
            on_confirmed=on_confirmed, on_cancel=on_cancel))

    def start_worker(self, worker):
        self.started.append(worker)

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
    assert leaf.text(1) == _format_token_amount(5_000_000, 6, "USDC")
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
