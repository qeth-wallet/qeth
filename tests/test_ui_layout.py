"""Layout + interaction tests for MainWindow.

These intentionally pin the user-visible structure (where the buttons
live, what the tabs are, what enabling rules apply) rather than the
exact widget tree. They're the safety net for the upcoming
plugin-style refactor: as long as the same observable behaviour
survives, the refactor is fine.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QTabBar, QToolButton,
)

from qeth.chains import DEFAULT_CHAINS
from qeth.tokens import TokenBalance


# --- Top-of-left: account actions row ---------------------------------------

def test_account_actions_present_at_top_of_left_pane(mainwindow):
    """Three account actions (Add / Copy / Remove) are wired up and
    rendered as buttons. They live on the MainWindow so the test
    doesn't care which container holds them."""
    assert mainwindow.act_add.text() == "Add account"
    assert mainwindow.act_copy.text() == "Copy address"
    assert mainwindow.act_remove.text() == "Remove account"
    # All three actions should be rendered as buttons somewhere visible.
    rendered_texts = {
        b.defaultAction().text()
        for b in mainwindow.findChildren(QToolButton)
        if b.defaultAction() is not None
    }
    assert {"Add account", "Copy address", "Remove account"} <= rendered_texts


def test_copy_and_remove_disabled_until_account_selected(mainwindow):
    """No tree selection (default for an empty store) → Copy and Remove
    actions are disabled; Add is always available."""
    assert mainwindow.act_add.isEnabled()
    assert not mainwindow.act_copy.isEnabled()
    assert not mainwindow.act_remove.isEnabled()


# --- Top-of-right: tab bar --------------------------------------------------

def test_tab_bar_has_tokens_and_transactions(mainwindow):
    tab_bar = mainwindow.findChild(QTabBar)
    assert tab_bar is not None
    labels = [tab_bar.tabText(i) for i in range(tab_bar.count())]
    assert labels == ["Tokens", "Transactions"]


def test_tab_bar_starts_on_tokens(mainwindow):
    # After the plugin refactor the active widget lives behind the
    # slot's active plugin — but the observable behaviour is the same.
    assert mainwindow.right_slot.active() is mainwindow.tokens_plugin


def test_switching_tab_swaps_visible_panel(qtbot, mainwindow):
    tab_bar = mainwindow.findChild(QTabBar)
    tab_bar.setCurrentIndex(1)
    assert mainwindow.right_slot.active() is mainwindow.transactions_plugin
    tab_bar.setCurrentIndex(0)
    assert mainwindow.right_slot.active() is mainwindow.tokens_plugin


# --- Bottom-of-right: single shared row -------------------------------------

def test_chain_combo_and_token_actions_share_one_row(mainwindow):
    """The +/-/★/👁 token-action buttons and the chain selector live
    in the same bottom row of the right slot — regression guard for
    the "two stacked rows" bug that prompted the merge."""
    bottom = mainwindow.right_slot._bottom

    def widgets_in(layout):
        out = []
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.widget() is not None:
                out.append(item.widget())
            elif item.layout() is not None:
                out.extend(widgets_in(item.layout()))
        return out

    widgets = widgets_in(bottom)
    assert mainwindow.chain_combo in widgets
    assert mainwindow.token_panel.btn_add in widgets
    assert mainwindow.token_panel.btn_show_all in widgets


def test_chain_combo_lists_all_default_chains(mainwindow):
    combo = mainwindow.chain_combo
    assert combo.count() == len(DEFAULT_CHAINS)
    chain_ids = [combo.itemData(i) for i in range(combo.count())]
    assert chain_ids == [c.chain_id for c in DEFAULT_CHAINS]


def test_chain_combo_change_updates_store(mainwindow):
    combo = mainwindow.chain_combo
    # Switch to Optimism (chain_id=10). Persistence is on by default
    # for user-driven changes via the toolbar combo.
    target = combo.findData(10)
    assert target >= 0
    combo.setCurrentIndex(target)
    assert mainwindow.store.current_chain_id == 10


# --- Account selection drives Copy / Remove --------------------------------

def _add_account_and_rebuild(mainwindow, address: str):
    """Inject a Ledger account into the store and refresh the tree
    so we can drive selection from a UI test."""
    mainwindow.store.add_account({
        "address": address,
        "path": "44'/60'/0'/0/0",
        "source": "ledger",
        "scheme": "BIP-44",
        "label": "",
    })
    mainwindow._rebuild_tree()


def test_selecting_account_enables_copy_and_remove(mainwindow):
    addr = "0x7a16ff8270133f063aab6c9977183d9e72835428"
    _add_account_and_rebuild(mainwindow, addr)

    # Find the QTreeWidgetItem we just added and select it.
    matches = mainwindow.tree.findItems(
        addr, Qt.MatchContains | Qt.MatchRecursive, 0
    )
    assert matches, "newly added account should appear in the tree"
    mainwindow.tree.setCurrentItem(matches[0])

    assert mainwindow.act_copy.isEnabled()
    assert mainwindow.act_remove.isEnabled()


def test_copy_action_puts_address_on_clipboard(mainwindow):
    addr = "0x7a16ff8270133f063aab6c9977183d9e72835428"
    _add_account_and_rebuild(mainwindow, addr)
    matches = mainwindow.tree.findItems(
        addr, Qt.MatchContains | Qt.MatchRecursive, 0
    )
    mainwindow.tree.setCurrentItem(matches[0])

    mainwindow.act_copy.trigger()
    assert QApplication.clipboard().text() == addr


# --- Token-action buttons follow the table selection -----------------------

def _fake_chain():
    return next(c for c in DEFAULT_CHAINS if c.chain_id == 1)


def test_hide_pin_disabled_by_default(mainwindow):
    # Empty token panel — nothing selected, nothing to hide/pin.
    assert not mainwindow.token_panel.btn_hide.isEnabled()
    assert not mainwindow.token_panel.btn_pin.isEnabled()


def test_hide_pin_enabled_only_for_erc20_rows(mainwindow):
    chain = _fake_chain()
    tokens = [
        TokenBalance(
            contract="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
            symbol="USDC", name="USD Coin", decimals=6,
            balance_raw=1_000_000,
        ),
    ]
    mainwindow.token_panel.show_balances(chain, 0, tokens, list_entries={})

    table = mainwindow.token_panel.table
    # Native row (row 0) — hide/pin make no sense for the native asset.
    table.selectRow(0)
    assert not mainwindow.token_panel.btn_hide.isEnabled()
    assert not mainwindow.token_panel.btn_pin.isEnabled()

    # ERC-20 row (row 1) — both should be available.
    table.selectRow(1)
    assert mainwindow.token_panel.btn_hide.isEnabled()
    assert mainwindow.token_panel.btn_pin.isEnabled()
