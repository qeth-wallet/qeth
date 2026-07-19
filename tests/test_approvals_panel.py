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


def test_spender_label_shown_in_col0_with_tooltip(qtbot):
    p = _panel(qtbot)
    p.append_rows([ApprovalRow(token=TOKEN, spender=SP1, allowance=1,
                               symbol="USDC", decimals=6,
                               spender_label="Uniswap: Router")])
    leaf = p.tree.topLevelItem(0).child(0)
    assert leaf.text(0) == "Uniswap: Router"       # WHO, not a bare address
    assert "Uniswap: Router" in leaf.toolTip(0) and SP1 in leaf.toolTip(0)


def test_spender_falls_back_to_short_addr(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1)])             # no label
    leaf = p.tree.topLevelItem(0).child(0)
    assert leaf.text(0) != "" and leaf.text(0) != SP1   # shortened form


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


# --- commit 2: modify / revoke + optimistic updates -----------------------

def test_modify_revoke_enabled_only_with_leaf(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row()])
    assert not p.btn_modify.isEnabled() and not p.btn_revoke.isEnabled()
    p.tree.setCurrentItem(p.tree.topLevelItem(0).child(0))
    assert p.btn_modify.isEnabled() and p.btn_revoke.isEnabled()
    p.tree.setCurrentItem(p.tree.topLevelItem(0))      # token node
    assert not p.btn_modify.isEnabled() and not p.btn_revoke.isEnabled()


def test_modify_button_emits_selected_row(qtbot):
    p = _panel(qtbot)
    row = _row()
    p.append_rows([row])
    p.tree.setCurrentItem(p.tree.topLevelItem(0).child(0))
    got = []
    p.modify_requested.connect(got.append)
    p.btn_modify.click()
    assert got == [row]


def test_revoke_button_emits_selected_row_as_list(qtbot):
    p = _panel(qtbot)
    row = _row()
    p.append_rows([row])
    p.tree.setCurrentItem(p.tree.topLevelItem(0).child(0))
    got = []
    p.revoke_requested.connect(got.append)
    p.btn_revoke.click()
    assert got == [[row]]


def test_mark_pending_disables_and_relabels(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row()])
    p.mark_pending(TOKEN, SP1)
    leaf = p.tree.topLevelItem(0).child(0)
    assert leaf.text(1) == "pending…"
    assert leaf.isDisabled()


def test_update_allowance_rerenders_and_reenables(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(allowance=_MAX)])
    p.mark_pending(TOKEN, SP1)
    p.update_allowance(TOKEN, SP1, 5_000_000)
    leaf = p.tree.topLevelItem(0).child(0)
    assert "unlimited" not in leaf.text(1).lower()
    assert not leaf.isDisabled()


def test_remove_leaf_drops_empty_token_node(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    p.remove_leaf(TOKEN, SP1)
    assert p.tree.topLevelItemCount() == 1
    assert p.tree.topLevelItem(0).childCount() == 1
    p.remove_leaf(TOKEN, SP2)                          # last spender gone
    assert p.tree.topLevelItemCount() == 0


# --- commit 3: checkbox batch selection -----------------------------------

def test_checked_leaves_collects_ticked(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    p.tree.topLevelItem(0).child(0).setCheckState(0, Qt.CheckState.Checked)
    checked = p.checked_leaves()
    assert len(checked) == 1 and checked[0].spender == SP1


def test_check_token_selects_whole_subtree(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    p.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)   # down-propagates
    assert len(p.checked_leaves()) == 2


def test_revoke_label_adapts_to_checked_count(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    assert p.btn_revoke.text() == "&Revoke"
    p.tree.topLevelItem(0).child(0).setCheckState(0, Qt.CheckState.Checked)
    assert p.btn_revoke.text() == "&Revoke (1)"
    p.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
    assert p.btn_revoke.text() == "&Revoke (2)"


def test_revoke_button_emits_checked_batch(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    p.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)   # both
    got = []
    p.revoke_requested.connect(got.append)
    p.btn_revoke.click()
    assert len(got) == 1 and len(got[0]) == 2


def test_revoke_button_falls_back_to_selection(qtbot):
    p = _panel(qtbot)
    row = _row(spender=SP1)
    p.append_rows([row, _row(spender=SP2)])           # nothing checked
    p.tree.setCurrentItem(p.tree.topLevelItem(0).child(0))
    got = []
    p.revoke_requested.connect(got.append)
    p.btn_revoke.click()
    assert got == [[row]]


def test_append_does_not_leave_boxes_checked(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    assert p.checked_leaves() == []                   # populate starts unchecked
    assert p.btn_revoke.text() == "&Revoke"
