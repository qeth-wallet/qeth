"""Approvals tree panel — grouping, rendering, scan lifecycle (commit 1)."""

from PySide6.QtCore import Qt

from qeth.plugins.approvals import ApprovalsPanel
from qeth.plugins.approvals.discovery import ApprovalRow

_MAX = (1 << 256) - 1
TOKEN = "0x" + "11" * 20
TOKEN2 = "0x" + "22" * 20
SP1 = "0x" + "ee" * 20
SP2 = "0x" + "ff" * 20


def _panel(qtbot):
    p = ApprovalsPanel()
    qtbot.addWidget(p)
    return p


def _row(token=TOKEN, spender=SP1, allowance=1, symbol="USDC", decimals=6):
    return ApprovalRow(token=token, spender=spender, allowance=allowance,
                       symbol=symbol, decimals=decimals)


def test_spenders_group_under_one_token_node(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    assert p.tree.topLevelItemCount() == 1
    tok = p.tree.topLevelItem(0)
    assert tok.text(0) == "USDC"
    assert tok.childCount() == 2


def test_two_tokens_two_nodes(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(token=TOKEN, symbol="A"), _row(token=TOKEN2, symbol="B")])
    assert p.tree.topLevelItemCount() == 2


def test_append_merges_into_existing_token(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1)])
    p.append_rows([_row(spender=SP2)])            # second batch, same token
    assert p.tree.topLevelItemCount() == 1
    assert p.tree.topLevelItem(0).childCount() == 2


def test_unlimited_rendering(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(allowance=_MAX)])
    leaf = p.tree.topLevelItem(0).child(0)
    assert "unlimited" in leaf.text(1).lower()


def test_leaves_and_token_are_checkable(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row()])
    tok = p.tree.topLevelItem(0)
    leaf = tok.child(0)
    assert tok.flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert tok.flags() & Qt.ItemFlag.ItemIsAutoTristate
    assert leaf.flags() & Qt.ItemFlag.ItemIsUserCheckable


def test_scan_progress_lifecycle(qtbot):
    p = _panel(qtbot)
    p.begin_scan()
    assert not p.btn_stop.isHidden()
    p.set_progress(3, 10)
    assert p.progress.maximum() == 10 and p.progress.value() == 3
    p.finish_scan(True)
    assert p.progress.isHidden() and p.btn_stop.isHidden()


def test_empty_after_complete_scan(qtbot):
    p = _panel(qtbot)
    p.begin_scan()
    p.finish_scan(True)
    assert "No active approvals" in p.status_lbl.text()


def test_stopped_scan_message(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row()])
    p.begin_scan()          # clears
    p.append_rows([_row()])
    p.finish_scan(False)    # stopped with rows
    assert "stopped" in p.status_lbl.text().lower()


def test_copy_enabled_only_with_leaf_selected(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row()])
    assert not p.btn_copy.isEnabled()
    p.tree.setCurrentItem(p.tree.topLevelItem(0).child(0))
    assert p.btn_copy.isEnabled()
    # selecting the token node (not a leaf) → no copy target
    p.tree.setCurrentItem(p.tree.topLevelItem(0))
    assert not p.btn_copy.isEnabled()
