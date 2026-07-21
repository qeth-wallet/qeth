"""Approvals tree panel — grouping, rendering, scan lifecycle, address reveal."""

from types import SimpleNamespace

from PySide6.QtCore import QObject, Qt, Signal

from qeth import QULONGLONG
from qeth.plugins.approvals import ApprovalsPanel, _format_allowance
from qeth.plugins.approvals.discovery import ApprovalRow

_MAX = (1 << 256) - 1
TOKEN = "0x" + "11" * 20
TOKEN2 = "0x" + "22" * 20
SP1 = "0x" + "ee" * 20
SP2 = "0x" + "ff" * 20
LABEL = "Uniswap: Router"


class _FakeIcons(QObject):
    icon_ready = Signal(QULONGLONG, str)

    def get(self, cid, contract):
        return None

    def request(self, cid, contract, url):
        pass


class _HostWithExplorer:
    def __init__(self, explorer):
        self._chain = SimpleNamespace(chain_id=1, explorer=explorer)
        self._icons = _FakeIcons()

    def current_chain(self):
        return self._chain

    def icon_cache(self):
        return self._icons

    def token_info(self, cid, addr):
        return None


def _panel(qtbot, host=None):
    p = ApprovalsPanel(host=host)
    qtbot.addWidget(p)
    return p


def _row(token=TOKEN, spender=SP1, allowance=1, symbol="USDC", decimals=6,
         price_usd=None, token_balance=0):
    return ApprovalRow(token=token, spender=spender, allowance=allowance,
                       symbol=symbol, decimals=decimals, price_usd=price_usd,
                       token_balance=token_balance)


def test_spenders_group_under_one_token_node(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    assert p.tree.topLevelItemCount() == 1
    tok = p.tree.topLevelItem(0)
    assert tok.text(0).startswith("USDC (0x") and tok.text(0).endswith(")")
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
    assert leaf.text(1) == "∞"                     # infinity sign, not a number


def test_spender_label_shown_in_col0_with_tooltip(qtbot):
    p = _panel(qtbot)
    p.append_rows([ApprovalRow(token=TOKEN, spender=SP1, allowance=1,
                               symbol="USDC", decimals=6,
                               spender_label="Uniswap: Router")])
    leaf = p.tree.topLevelItem(0).child(0)
    assert leaf.text(0) == "Uniswap: Router"       # WHO, not a bare address
    assert "Uniswap: Router" in leaf.toolTip(0) and SP1 in leaf.toolTip(0)


def test_spender_without_label_shows_full_address(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1)])             # no label → address (elides in view)
    leaf = p.tree.topLevelItem(0).child(0)
    assert leaf.text(0) == SP1


# --- allowance formatting -------------------------------------------------

def test_format_allowance_unlimited_threshold():
    assert _format_allowance(_MAX, 18) == "∞"
    assert _format_allowance(2 ** 255, 18) == "∞"                # near-max sentinel
    assert _format_allowance(2 ** 255 - 1, 0) != "∞"             # just below → a number


def test_format_allowance_scientific_for_large_finite():
    # 91.2 billion tokens (below 2**255) → typographic ×10ⁿ, not 11 plain digits
    assert _format_allowance(91_200_000_000 * 10 ** 18, 18) == "9.12 × 10¹⁰"


def test_format_allowance_applies_decimals_without_symbol():
    assert _format_allowance(5_000_000, 6) == "5"                # 5-unit cap, bare
    assert _format_allowance(1500, 3) == "1.5"


def test_allowance_column_has_no_symbol(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(allowance=5_000_000, symbol="USDC", decimals=6)])
    leaf = p.tree.topLevelItem(0).child(0)
    assert leaf.text(1) == "5"                                   # symbol lives in the branch


# --- hover / selection address reveal -------------------------------------

def _named_row(spender=SP1):
    return ApprovalRow(token=TOKEN, spender=spender, allowance=1,
                       symbol="USDC", decimals=6, spender_label=LABEL)


def test_hover_reveals_address_then_restores(qtbot):
    p = _panel(qtbot)
    p.append_rows([_named_row()])
    leaf = p.tree.topLevelItem(0).child(0)
    assert leaf.text(0) == LABEL                    # name by default
    p._on_item_entered(leaf, 0)                     # hover
    assert leaf.text(0) == SP1                       # reveals address
    p._hovered = None
    p._refresh_reveal()                              # mouse left the viewport
    assert leaf.text(0) == LABEL                     # restored


def test_selection_reveals_address(qtbot):
    p = _panel(qtbot)
    p.append_rows([_named_row()])
    leaf = p.tree.topLevelItem(0).child(0)
    p.tree.setCurrentItem(leaf)                      # selection → reveal
    assert leaf.text(0) == SP1


def test_unnamed_leaf_unchanged_on_hover(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1)])              # no label
    leaf = p.tree.topLevelItem(0).child(0)
    p._on_item_entered(leaf, 0)
    assert leaf.text(0) == SP1                       # already the address


# --- explorer -------------------------------------------------------------

def test_double_click_opens_spender_in_explorer(qtbot, monkeypatch):
    opened = []
    monkeypatch.setattr("qeth.plugins.approvals.QDesktopServices.openUrl",
                        lambda url: opened.append(url.toString()))
    p = _panel(qtbot, host=_HostWithExplorer("https://etherscan.io"))
    p.append_rows([_row(spender=SP1)])
    p._on_double_clicked(p.tree.topLevelItem(0).child(0), 0)
    assert opened == [f"https://etherscan.io/address/{SP1}"]


def test_explorer_button_enabled_with_leaf_and_explorer(qtbot):
    p = _panel(qtbot, host=_HostWithExplorer("https://etherscan.io"))
    p.append_rows([_row(spender=SP1)])
    assert not p.btn_explorer.isEnabled()            # nothing selected
    p.tree.setCurrentItem(p.tree.topLevelItem(0).child(0))
    assert p.btn_explorer.isEnabled()


def test_explorer_button_disabled_when_chain_has_no_explorer(qtbot):
    p = _panel(qtbot, host=_HostWithExplorer(""))
    p.append_rows([_row(spender=SP1)])
    p.tree.setCurrentItem(p.tree.topLevelItem(0).child(0))
    assert not p.btn_explorer.isEnabled()


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
    assert not p._scan_bar.isHidden()               # bar + stop shown as one unit
    p.set_progress(3, 10)
    assert p.progress.maximum() == 10 and p.progress.value() == 3
    p.finish_scan(True)
    assert p._scan_bar.isHidden()


def test_stop_button_is_a_small_toolbutton_next_to_bar(qtbot):
    from PySide6.QtWidgets import QToolButton
    p = _panel(qtbot)
    assert isinstance(p.btn_stop, QToolButton)      # small stop sign, not a big button
    assert p.btn_stop.parent() is p._scan_bar       # sits in the progress row
    assert p.btn_stop not in p.action_widgets()     # not in the bottom action row


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

def test_action_button_disabled_without_leaf(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row()])
    assert not p.btn_action.isEnabled()                # nothing selected/checked
    p.tree.setCurrentItem(p.tree.topLevelItem(0))      # token node, not a leaf
    assert not p.btn_action.isEnabled()


def test_action_button_is_modify_for_selected_leaf(qtbot):
    p = _panel(qtbot)
    row = _row()
    p.append_rows([row])
    p.tree.setCurrentItem(p.tree.topLevelItem(0).child(0))
    assert p.btn_action.isEnabled() and "Modify" in p.btn_action.text()
    got = []
    p.modify_requested.connect(got.append)
    p.btn_action.click()
    assert got == [row]


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
    assert leaf.text(1) == "5"                     # was ∞, now the finite value
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


def test_action_button_morphs_to_revoke_on_check(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    assert "Modify" in p.btn_action.text()                     # nothing checked
    p.tree.topLevelItem(0).child(0).setCheckState(0, Qt.CheckState.Checked)
    assert p.btn_action.text() == "&Revoke (1)"
    p.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)   # both
    assert p.btn_action.text() == "&Revoke (2)"


def test_action_button_revoke_emits_checked_batch(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    p.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)   # both
    got = []
    p.revoke_requested.connect(got.append)
    p.btn_action.click()
    assert len(got) == 1 and len(got[0]) == 2


def test_checked_boxes_win_over_selection(qtbot):
    # a checked box means "batch revoke" even if a different row is selected
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    p.tree.topLevelItem(0).child(1).setCheckState(0, Qt.CheckState.Checked)  # check SP2
    p.tree.setCurrentItem(p.tree.topLevelItem(0).child(0))                   # select SP1
    assert p.btn_action.text() == "&Revoke (1)"
    got = []
    p.revoke_requested.connect(got.append)
    p.btn_action.click()
    assert [x.spender for x in got[0]] == [SP2]


def test_append_does_not_leave_boxes_checked(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    assert p.checked_leaves() == []                   # populate starts unchecked
    assert "Modify" in p.btn_action.text()


# --- USD valuation + sorting + column sizing ------------------------------

from decimal import Decimal  # noqa: E402

from qeth.formatting import short_addr  # noqa: E402
from qeth.plugins.approvals import (  # noqa: E402
    _allowance_cell, _row_sort_value, _row_usd,
)


def test_row_usd_finite_priced():
    assert _row_usd(_row(allowance=5_000_000, decimals=6,
                         price_usd=Decimal("1"))) == Decimal("5")


def test_row_usd_none_when_unpriced():
    assert _row_usd(_row(allowance=5_000_000, decimals=6, price_usd=None)) is None


def test_row_usd_none_when_unlimited():
    assert _row_usd(_row(allowance=_MAX, price_usd=Decimal("1"))) is None


def test_row_sort_value_unlimited_is_inf():
    assert _row_sort_value(_row(allowance=_MAX)) == float("inf")


def test_row_sort_value_priced_is_usd():
    assert _row_sort_value(_row(allowance=5_000_000, decimals=6,
                                price_usd=Decimal("2"))) == 10.0


def test_row_sort_value_unpriced_finite_is_zero():
    assert _row_sort_value(_row(allowance=5_000_000, decimals=6,
                                price_usd=None)) == 0.0


def test_allowance_cell_is_amount_only_no_usd():
    # USD is not shown (it read as clutter), even when priced.
    assert _allowance_cell(_row(allowance=5_000_000, decimals=6,
                                price_usd=Decimal("1"))) == "5"
    assert "$" not in _allowance_cell(_row(allowance=_MAX, price_usd=Decimal("1")))


def test_token_node_shows_symbol_and_short_address(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(symbol="USDC")])
    assert p.tree.topLevelItem(0).text(0) == f"USDC ({short_addr(TOKEN)})"


def test_allowance_column_has_no_usd_text(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(allowance=5_000_000, decimals=6, price_usd=Decimal("1"))])
    assert p.tree.topLevelItem(0).child(0).text(1) == "5"


def test_stretch_last_section_is_off(qtbot):
    p = _panel(qtbot)
    assert p.tree.header().stretchLastSection() is False


def test_default_sort_ranks_by_value_at_risk(qtbot):
    # No header click: the most-exposed token bubbles to the top out of the box.
    # Both unlimited (so the OLD summed-cap sort tied them at ∞) — the sort ranks
    # by the value actually HELD, so BIG ($5000) beats SMALL ($5).
    p = _panel(qtbot)
    p.append_rows([
        _row(token=TOKEN, symbol="SMALL", spender=SP1, allowance=_MAX,
             decimals=6, price_usd=Decimal("1"), token_balance=5_000_000),        # $5
        _row(token=TOKEN2, symbol="BIG", spender=SP1, allowance=_MAX,
             decimals=6, price_usd=Decimal("1"), token_balance=5_000_000_000),    # $5000
    ])
    assert p.tree.topLevelItem(0).text(0).startswith("BIG")
    assert p.tree.topLevelItem(1).text(0).startswith("SMALL")


def test_header_click_switches_to_token_alphabetical(qtbot):
    # Clicking the identity header switches away from the value-at-risk default.
    p = _panel(qtbot)
    p.append_rows([
        _row(token=TOKEN2, symbol="ZRX", spender=SP1),
        _row(token=TOKEN, symbol="AAVE", spender=SP1),
    ])
    p._on_header_clicked(0)
    assert p.tree.topLevelItem(0).text(0).startswith("AAVE")
    assert p.tree.topLevelItem(1).text(0).startswith("ZRX")


def test_held_token_outranks_a_bigger_unheld_allowance(qtbot):
    # A huge unlimited cap on a token you DON'T hold is $0 at risk → sorts below
    # a modest holding of a token you do hold (the default value-at-risk order).
    p = _panel(qtbot)
    p.append_rows([
        _row(token=TOKEN2, symbol="UNHELD", spender=SP1, allowance=_MAX,
             price_usd=Decimal("1"), token_balance=0),                # nothing held
        _row(token=TOKEN, symbol="HELD", spender=SP1, allowance=1_000_000,
             decimals=6, price_usd=Decimal("1"), token_balance=10_000_000),   # $10
    ])
    assert p.tree.topLevelItem(0).text(0).startswith("HELD")     # real risk first
    assert p.tree.topLevelItem(1).text(0).startswith("UNHELD")   # $0 at risk → bottom


def test_token_sort_key_is_value_at_risk(qtbot):
    from qeth.plugins.approvals import _USD_SORT_ROLE
    p = _panel(qtbot)
    p.append_rows([
        _row(token=TOKEN, symbol="T", spender=SP1, allowance=_MAX,
             decimals=6, price_usd=Decimal("2"), token_balance=3_000_000),   # 3 × $2
    ])
    assert p.tree.topLevelItem(0).data(1, _USD_SORT_ROLE) == 6.0         # value at risk


def test_leaves_still_sort_by_their_own_cap(qtbot):
    # Within a token, spender leaves keep ranking by allowance exposure — an
    # unlimited cap leaf above a finite one.
    p = _panel(qtbot)
    p.append_rows([
        _row(token=TOKEN, symbol="T", spender=SP1, allowance=1_000_000,
             decimals=6, price_usd=Decimal("1")),                    # $1 finite
        _row(token=TOKEN, symbol="T", spender=SP2, allowance=_MAX),  # unlimited
    ])
    node = p.tree.topLevelItem(0)
    assert node.child(0).text(0) == SP2       # unlimited spender first
    assert node.child(1).text(0) == SP1


# --- resizable two-column split -------------------------------------------

from PySide6.QtWidgets import QHeaderView  # noqa: E402


def test_allowance_column_is_right_aligned(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(allowance=5)])
    leaf = p.tree.topLevelItem(0).child(0)
    assert leaf.textAlignment(1) & int(Qt.AlignmentFlag.AlignRight)


def test_icon_buttons_are_flat(qtbot):
    p = _panel(qtbot)
    assert p.btn_copy.isFlat() and p.btn_explorer.isFlat()
    assert not p.btn_action.isFlat()               # the text button keeps its frame


def test_no_rescan_button_in_action_row(qtbot):
    p = _panel(qtbot)
    assert not hasattr(p, "btn_refresh")
    # action, select-all, copy, explorer, search (no rescan)
    assert p.action_widgets() == [p.btn_action, p.btn_select_all, p.btn_copy,
                                   p.btn_explorer, p.btn_search]


# --- find / filter (Ctrl+F) ------------------------------------------------

def _tok_visibility(p):
    from qeth.plugins.approvals import _TOKEN_ROLE
    return {p.tree.topLevelItem(i).data(0, _TOKEN_ROLE):
            not p.tree.topLevelItem(i).isHidden()
            for i in range(p.tree.topLevelItemCount())}


def _visible_spenders(p):
    from qeth.plugins.approvals import _ROW_ROLE
    out = []
    for i in range(p.tree.topLevelItemCount()):
        node = p.tree.topLevelItem(i)
        if node.isHidden():
            continue
        for j in range(node.childCount()):
            leaf = node.child(j)
            r = leaf.data(0, _ROW_ROLE)
            if not leaf.isHidden() and r is not None:
                out.append(r.spender.lower())
    return sorted(set(out))


def test_ctrl_f_shows_bar_escape_hides(qtbot):
    p = _panel(qtbot)                                 # not shown → check isHidden flag
    assert p._search_edit.isHidden()
    p._show_search()
    assert not p._search_edit.isHidden() and p.btn_search.isChecked()
    p._hide_search()
    assert p._search_edit.isHidden() and not p.btn_search.isChecked()


def test_filter_by_token_address_shows_only_that_subtree(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(token=TOKEN, symbol="USDC", spender=SP1),
                   _row(token=TOKEN2, symbol="DAI", spender=SP2)])
    p._search_by(TOKEN)
    vis = _tok_visibility(p)
    assert vis[TOKEN.lower()] and not vis[TOKEN2.lower()]


def test_filter_by_token_symbol(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(token=TOKEN, symbol="USDC", spender=SP1),
                   _row(token=TOKEN2, symbol="DAI", spender=SP2)])
    p._search_by("dai")
    vis = _tok_visibility(p)
    assert vis[TOKEN2.lower()] and not vis[TOKEN.lower()]


def test_filter_by_spender_address_keeps_only_matching_leaves(qtbot):
    p = _panel(qtbot)
    # SP1 approved on both tokens; SP2 only on TOKEN2.
    p.append_rows([_row(token=TOKEN, symbol="A", spender=SP1),
                   _row(token=TOKEN2, symbol="B", spender=SP1),
                   _row(token=TOKEN2, symbol="B", spender=SP2)])
    p._search_by(SP1)
    # both token nodes stay (each has an SP1 leaf), but SP2's leaf is hidden
    assert _visible_spenders(p) == [SP1.lower()]
    assert all(_tok_visibility(p).values())          # both tokens shown


def test_filter_by_spender_soft_name(qtbot):
    p = _panel(qtbot)
    p.append_rows([
        ApprovalRow(token=TOKEN, spender=SP1, allowance=1, symbol="A",
                    spender_soft_label="Venus crvUSD"),
        _row(token=TOKEN2, symbol="B", spender=SP2)])
    p._search_by("venus")
    vis = _tok_visibility(p)
    assert vis[TOKEN.lower()] and not vis[TOKEN2.lower()]


def test_filter_by_spender_name_tag(qtbot):
    p = _panel(qtbot)
    p.append_rows([
        ApprovalRow(token=TOKEN, spender=SP1, allowance=1, symbol="A",
                    spender_label="Uniswap: Universal Router"),
        _row(token=TOKEN2, symbol="B", spender=SP2)])
    p._search_by("uniswap")
    assert _visible_spenders(p) == [SP1.lower()]


def test_clearing_filter_restores_everything(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(token=TOKEN, symbol="A", spender=SP1),
                   _row(token=TOKEN2, symbol="B", spender=SP2)])
    p._search_by(TOKEN)
    assert not _tok_visibility(p)[TOKEN2.lower()]     # filtered out
    p._hide_search()                                  # clears the field → reset
    assert all(_tok_visibility(p).values())


def test_active_filter_reapplies_to_streamed_rows(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(token=TOKEN, symbol="A", spender=SP1)])
    p._search_by(TOKEN2)                              # filter a token not present yet
    assert not _tok_visibility(p)[TOKEN.lower()]
    p.append_rows([_row(token=TOKEN2, symbol="B", spender=SP2)])   # scan streams it in
    vis = _tok_visibility(p)
    assert vis[TOKEN2.lower()] and not vis[TOKEN.lower()]


def test_clear_resets_the_filter(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(token=TOKEN, spender=SP1)])
    p._search_by(TOKEN)
    p.clear()
    assert p._filter_text == "" and not p._search_edit.isVisible()


def test_allowance_column_gets_a_gap_before_amount(qtbot):
    p = _shown_panel(qtbot)
    vp = p.tree.viewport().width()
    if vp <= 2 * 48:
        import pytest
        pytest.skip("viewport too small offscreen")
    # allowance column is content + a fixed gap, so the number isn't jammed
    # against the identity column
    from qeth.plugins.approvals import _COL_GAP
    assert p.tree.columnWidth(1) >= _COL_GAP


def test_both_columns_interactive_no_last_stretch(qtbot):
    p = _panel(qtbot)
    hh = p.tree.header()
    assert hh.sectionResizeMode(0) == QHeaderView.ResizeMode.Interactive
    assert hh.sectionResizeMode(1) == QHeaderView.ResizeMode.Interactive
    assert hh.stretchLastSection() is False


def _shown_panel(qtbot):
    p = _panel(qtbot)
    p.resize(440, 300)
    p.show()
    qtbot.waitExposed(p)
    p.append_rows([_row(spender=SP1)])
    return p


def test_columns_fill_the_viewport(qtbot):
    p = _shown_panel(qtbot)
    vp = p.tree.viewport().width()
    if vp <= 2 * 48:
        import pytest
        pytest.skip("viewport too small offscreen")
    assert p.tree.columnWidth(0) + p.tree.columnWidth(1) == vp


def test_divider_drag_reflows_and_stays_filled(qtbot):
    p = _shown_panel(qtbot)
    vp = p.tree.viewport().width()
    if vp <= 2 * 48:
        import pytest
        pytest.skip("viewport too small offscreen")
    target = vp // 3
    p._on_section_resized(0, p.tree.columnWidth(0), target)   # user drags divider
    assert p.tree.columnWidth(0) == target
    assert p.tree.columnWidth(0) + p.tree.columnWidth(1) == vp   # allowance absorbed


# --- upsert / all_rows / prune (cache support) ----------------------------

def test_append_upserts_existing_pair(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1, allowance=5, decimals=0)])
    p.append_rows([_row(spender=SP1, allowance=9, decimals=0)])   # same pair, new value
    tok = p.tree.topLevelItem(0)
    assert tok.childCount() == 1                                  # not duplicated
    assert tok.child(0).text(1) == "9"                            # refreshed in place


def test_upsert_preserves_check_state(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1, allowance=5, decimals=0)])
    p.tree.topLevelItem(0).child(0).setCheckState(0, Qt.CheckState.Checked)
    p.append_rows([_row(spender=SP1, allowance=9, decimals=0)])   # re-scan
    assert p.tree.topLevelItem(0).child(0).checkState(0) == Qt.CheckState.Checked


def test_all_rows_returns_displayed(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    assert {r.spender for r in p.all_rows()} == {SP1, SP2}


def test_prune_zeroed_drops_only_the_zeroed_leaves(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    p.prune_zeroed({(TOKEN.lower(), SP2.lower())})     # only SP2 read as zero
    assert p.tree.topLevelItem(0).childCount() == 1
    assert p.all_rows()[0].spender == SP1              # SP1 (not zeroed) survives


def test_prune_zeroed_empty_keeps_everything(qtbot):
    # A scan that read nothing as zero prunes nothing — the transient-read fix.
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1)])
    p.prune_zeroed(set())
    assert p.tree.topLevelItemCount() == 1


def test_prune_zeroed_removes_emptied_token_node(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1)])
    p.prune_zeroed({(TOKEN.lower(), SP1.lower())})
    assert p.tree.topLevelItemCount() == 0


def test_progress_bar_has_no_text(qtbot):
    p = _panel(qtbot)
    assert not p.progress.isTextVisible()          # just the bar, no "%p%"


def test_stop_button_fills_to_progress_bar_height(qtbot):
    # theme-independent alignment: the bar keeps its natural height (Fixed) and
    # the button fills to it (Ignored vertical)
    from PySide6.QtWidgets import QSizePolicy
    p = _panel(qtbot)
    assert p.progress.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Fixed
    assert p.btn_stop.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Ignored


def test_stop_button_aligns_with_progress_bar_when_shown(qtbot):
    p = _panel(qtbot)
    p.resize(400, 300)
    p.begin_scan()
    p.show()
    qtbot.waitExposed(p)
    assert p.btn_stop.height() == p.progress.height()          # same height
    assert p.btn_stop.geometry().top() == p.progress.geometry().top()   # same y


# --- select-all toggle ----------------------------------------------------

def test_select_all_checks_every_leaf(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    assert len(p.checked_leaves()) == 0
    p.btn_select_all.click()
    assert len(p.checked_leaves()) == 2                # all checked
    assert p.btn_action.text() == "&Revoke (2)"       # morphs to batch revoke


def test_select_all_toggles_off_when_all_checked(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    p.btn_select_all.click()                          # all on
    p.btn_select_all.click()                          # toggle off
    assert len(p.checked_leaves()) == 0


def test_select_all_disabled_when_empty(qtbot):
    p = _panel(qtbot)
    assert not p.btn_select_all.isEnabled()
    p.append_rows([_row()])
    assert p.btn_select_all.isEnabled()


def test_select_all_is_flat_icon_button(qtbot):
    p = _panel(qtbot)
    assert p.btn_select_all.isFlat() and p.btn_select_all.text() == ""


# --- "at risk" token-node pill --------------------------------------------

from qeth.plugins.approvals import _RISK_ROLE, _risk_tag, _token_risk_usd  # noqa: E402


def test_risk_usd_is_balance_times_price():
    # 5.0 USDC (6 decimals) at $1.00 → $5 at risk
    r = _row(decimals=6, price_usd=Decimal("1"), token_balance=5_000_000)
    assert _token_risk_usd(r) == Decimal("5")
    assert _risk_tag(r) == "$5.00 at risk"


def test_risk_tag_empty_when_not_held_or_unpriced():
    assert _risk_tag(_row(price_usd=Decimal("1"), token_balance=0)) == ""  # not held
    assert _risk_tag(_row(price_usd=None, token_balance=5_000_000)) == ""  # unpriced
    # dust below a cent → no tag
    assert _risk_tag(_row(decimals=6, price_usd=Decimal("0.0001"),
                          token_balance=1)) == ""


def test_token_node_shows_risk_pill_when_held_and_priced(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1, decimals=6, price_usd=Decimal("2"),
                        token_balance=10_000_000)])       # 10 tokens × $2 = $20
    node = p._token_items[TOKEN]
    assert node.data(1, _RISK_ROLE) == "$20.00 at risk"


def test_token_node_no_pill_when_not_held(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1, price_usd=Decimal("2"), token_balance=0)])
    node = p._token_items[TOKEN]
    assert not node.data(1, _RISK_ROLE)


# --- action-button stable width + selection keys --------------------------

def test_action_button_width_locked_to_max(qtbot):
    p = _panel(qtbot)
    assert p.btn_action.minimumWidth() > 0
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    p.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)      # → Revoke (2)
    assert p.btn_action.text() == "&Revoke (2)"
    # neither label exceeds the locked minimum, so the row never reflows
    assert p.btn_action.sizeHint().width() <= p.btn_action.minimumWidth()


def _key(p, key):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent
    ev = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
    return p.eventFilter(p.tree, ev)


def test_plus_key_selects_all(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    assert _key(p, Qt.Key.Key_Plus) is True            # consumed
    assert len(p.checked_leaves()) == 2


def test_minus_key_deselects_all(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    p._select_all()
    assert _key(p, Qt.Key.Key_Minus) is True
    assert len(p.checked_leaves()) == 0


def test_asterisk_key_inverts_selection(qtbot):
    p = _panel(qtbot)
    p.append_rows([_row(spender=SP1), _row(spender=SP2)])
    p.tree.topLevelItem(0).child(0).setCheckState(0, Qt.CheckState.Checked)  # SP1
    assert _key(p, Qt.Key.Key_Asterisk) is True
    assert {r.spender for r in p.checked_leaves()} == {SP2}   # flipped


def test_select_keys_ignored_when_empty(qtbot):
    p = _panel(qtbot)
    assert _key(p, Qt.Key.Key_Plus) is not True         # nothing to select → not consumed
