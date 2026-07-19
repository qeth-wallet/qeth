"""Layout + interaction tests for MainWindow.

These intentionally pin the user-visible structure (where the buttons
live, what the tabs are, what enabling rules apply) rather than the
exact widget tree. They're the safety net for the upcoming
plugin-style refactor: as long as the same observable behaviour
survives, the refactor is fine.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QPushButton, QTabBar,
)

from qeth.chains import DEFAULT_CHAINS
from qeth.token_discovery import TokenBalance


# --- Account actions row ----------------------------------------------------

def test_account_actions_rendered_as_buttons(mainwindow):
    """Add / Copy / Remove are wired as buttons on the slot's bottom row.
    Add is the labelled primary (like the Tokens 'Send'); Copy / Remove are
    icon-only flat buttons (like the Tokens utility icons), with their label
    moved to the tooltip."""
    # Texts carry GNOME-HIG access-key mnemonics ("&A" → Alt+A).
    assert mainwindow.act_add.text() == "&Add Account"
    assert mainwindow.act_copy.text() == "&Copy Address"
    assert mainwindow.act_remove.text() == "&Remove Account"
    buttons = mainwindow.findChildren(QPushButton)
    # Add is the labelled primary button.
    assert any(b.text().replace("&", "") == "Add Account" for b in buttons)
    # Copy / Remove are icon-only (no text) — identified by their tooltip,
    # carrying an icon, flat, so they read as utility buttons.
    for label in ("Copy Address", "Remove Account"):
        match = [b for b in buttons if b.toolTip() == label]
        assert match, f"no button tooltipped {label!r}"
        btn = match[0]
        assert not btn.text() and not btn.icon().isNull() and btn.isFlat()


def test_accounts_tree_has_copy_and_delete_shortcuts(mainwindow):
    """Ctrl+C copies / Del removes the selected address, scoped to the
    accounts tree so they don't shadow copy/delete in other panels."""
    from PySide6.QtGui import QKeySequence
    wp = mainwindow.wallets_plugin
    assert wp.act_copy.shortcut() == QKeySequence(QKeySequence.Copy)
    assert wp.act_remove.shortcut() == QKeySequence(QKeySequence.Delete)
    # Scoped to the tree (not application-global).
    assert wp.act_copy.shortcutContext() == Qt.WidgetWithChildrenShortcut
    assert wp.act_remove.shortcutContext() == Qt.WidgetWithChildrenShortcut
    tree_actions = set(wp._tree.actions())
    assert wp.act_copy in tree_actions
    assert wp.act_remove in tree_actions


def test_wallet_tree_group_roots_have_icons(mainwindow):
    """A populated Ledger / Hot wallet / Watch-only root carries the same icon
    as its add-account menu action. (Empty roots are now hidden — see
    TestWalletsTreeHidesEmptyRoots — so populate one of each first.)"""
    wp = mainwindow.wallets_plugin
    wp._store.accounts = [
        {"address": "0x" + "11" * 20, "source": "ledger",
         "scheme": "Ledger Live", "label": ""},
        {"address": "0x" + "22" * 20, "source": "hot", "label": ""},
        {"address": "0x" + "33" * 20, "source": "watch_only", "label": ""},
    ]
    wp._rebuild_tree()
    tree = wp._tree
    roots = {tree.topLevelItem(i).text(0).split(" (")[0]: tree.topLevelItem(i)
             for i in range(tree.topLevelItemCount())}
    assert {"Ledger", "Hot wallet", "Watch only"} <= set(roots)
    for name in ("Ledger", "Hot wallet", "Watch only"):
        assert not roots[name].icon(0).isNull(), f"{name} root has no icon"


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
    assert labels == ["Tokens", "Transactions", "ENS", "Approvals"]


def test_tab_bar_starts_on_tokens(mainwindow):
    # After the plugin refactor the active widget lives behind the
    # slot's active plugin — but the observable behaviour is the same.
    assert mainwindow.right_slot.active() is mainwindow.tokens_plugin


def test_join_workers_shuts_down_every_plugin(mainwindow):
    # Regression: ens_plugin used to be omitted from the shutdown loop, so its
    # timers/workers leaked past quit. Every mounted plugin must be shut down.
    shut: list = []
    for pid in ("wallets", "tokens", "transactions", "ens", "approvals"):
        p = mainwindow.plugin(pid)
        p.shutdown = lambda pid=pid: shut.append(pid)
    mainwindow._join_workers()
    assert set(shut) == {"wallets", "tokens", "transactions", "ens", "approvals"}


def test_host_plugin_accessor_resolves_all_and_unknown(mainwindow):
    assert mainwindow.plugin("tokens") is mainwindow.tokens_plugin
    assert mainwindow.plugin("ens") is mainwindow.ens_plugin
    assert mainwindow.plugin("nope") is None


def test_right_slot_has_a_config_gear_corner(mainwindow):
    # The plugin on/off gear is mounted right-aligned on the right slot's tab
    # row, and carries the plugin-toggle menu.
    corner = mainwindow.right_slot._corner
    assert corner.count() == 1
    gear = corner.itemAt(0).widget()
    assert gear is not None and gear.menu() is not None
    assert [a.text() for a in gear.menu().actions()] == ["Tokens", "ENS", "Approvals"]


def test_switching_tab_swaps_visible_panel(qtbot, mainwindow):
    tab_bar = mainwindow.findChild(QTabBar)
    tab_bar.setCurrentIndex(1)
    assert mainwindow.right_slot.active() is mainwindow.transactions_plugin
    tab_bar.setCurrentIndex(0)
    assert mainwindow.right_slot.active() is mainwindow.tokens_plugin


def test_ens_tree_is_a_keyboard_tab_stop(mainwindow):
    # The ENS tree must be a focus tab-stop like the tokens/transactions
    # tables, so Tab reaches it and arrow keys navigate it.
    stops = mainwindow._collect_tab_stops()
    assert mainwindow.ens_plugin._panel.tree in stops


def test_left_right_cycles_through_all_right_tabs_including_ens(qtbot, mainwindow):
    mw = mainwindow
    flt = mw._tab_cycle_filter
    mw.right_slot.set_active(mw.tokens_plugin)
    # Right: Tokens → Transactions → ENS → Approvals → wrap to Tokens
    assert flt._handle_left_right(mw.tokens_plugin._panel.table, True) is True
    assert mw.right_slot.active() is mw.transactions_plugin
    assert flt._handle_left_right(mw.transactions_plugin._panel.table, True) is True
    assert mw.right_slot.active() is mw.ens_plugin           # was excluded before
    # the ENS tree counts as a "right table", so ←/→ work while it's focused
    ens_tree = mw.ens_plugin._panel.tree
    assert ens_tree in flt._right_tables()
    assert flt._handle_left_right(ens_tree, True) is True
    approvals = mw.plugin("approvals")
    assert mw.right_slot.active() is approvals               # ENS → Approvals
    assert flt._handle_left_right(approvals._panel.tree, True) is True
    assert mw.right_slot.active() is mw.tokens_plugin        # Approvals → wrap


def test_tab_reaches_ens_tree_when_ens_active(qtbot, mainwindow):
    mw = mainwindow
    mw.right_slot.set_active(mw.ens_plugin)
    flt = mw._tab_cycle_filter
    # Tab from the wallet tree lands on the active right list — the ENS tree.
    assert flt._active_right_table() is mw.ens_plugin._panel.tree
    assert flt._handle_tab(mw.wallets_plugin._tree) is True


def test_chain_controls_hidden_on_ens_tab(qtbot, mainwindow):
    """ENS is mainnet-only, so the network selector is meaningless there and
    is hidden — restored on any other tab."""
    mw = mainwindow
    assert not mw.chain_combo.isHidden()          # Tokens active initially
    mw.right_slot.set_active(mw.ens_plugin)
    assert mw.chain_combo.isHidden() and mw.chain_rpc_btn.isHidden()
    mw.right_slot.set_active(mw.tokens_plugin)
    assert not mw.chain_combo.isHidden() and not mw.chain_rpc_btn.isHidden()


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
    # Tree lives inside WalletsPlugin now.
    mainwindow.wallets_plugin.rebuild_tree()


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


def test_initial_selection_reaches_right_slot_plugins(qtbot, tmp_qeth,
                                                      fake_rpc,
                                                      hermetic_mainwindow,
                                                      monkeypatch):
    """Regression: on startup MainWindow used to wire its
    selected_address_changed listener *after* the wallet tree's
    auto-select fired, so the right-slot plugins never received the
    initial on_account_changed and their panels stayed empty until
    TokenListsLoader nudged them. Fix is a manual replay; this test
    locks it in."""
    from qeth.store import Store
    from qeth.ui import MainWindow

    addr = "0x7a16ff8270133f063aab6c9977183d9e72835428"
    store = Store.load()
    store.add_account({
        "address": addr, "path": "44'/60'/0'/0/0",
        "source": "ledger", "scheme": "BIP-44", "label": "",
    })

    # Capture on_account_changed calls on the right-slot plugins for
    # the duration of MainWindow construction.
    calls: list[tuple[str, object]] = []

    from qeth.plugins.tokens import TokensPlugin
    from qeth.plugins.transactions import TransactionsPlugin
    monkeypatch.setattr(
        TokensPlugin, "on_account_changed",
        lambda self, a: calls.append(("tokens", a)),
    )
    monkeypatch.setattr(
        TransactionsPlugin, "on_account_changed",
        lambda self, a: calls.append(("transactions", a)),
    )

    win = MainWindow(store, fake_rpc)
    qtbot.addWidget(win)

    # Both plugins must have seen the default address before __init__
    # returned. (TokenListsLoader's no-op finish may later trigger a
    # second pass; "at least once" is what we care about.)
    assert ("tokens", addr) in calls
    assert ("transactions", addr) in calls


def test_status_bar_shows_rotating_hints_not_rpc_info(mainwindow):
    win = mainwindow
    # No more persistent RPC / default-wallet labels.
    assert not hasattr(win, "rpc_label")
    assert not hasattr(win, "default_label")
    # An idle hint is shown (light-bulb prefix), and rotates.
    first = win._hint_label.text()
    assert first.startswith("💡 ")
    win._show_next_hint()
    assert win._hint_label.text() != first   # random order, no immediate repeat
    # A transient status_message REPLACES the hint in the same label …
    win.status_message("Broadcast 0xabc", 3000)
    assert win._hint_label.text() == "Broadcast 0xabc"
    # … and the hint is restored when the message expires.
    win._restore_hint()
    assert win._hint_label.text() == win._current_hint
    assert win._hint_label.text().startswith("💡 ")


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


def test_account_book_dedupes_repeat_address_with_effective_label(mainwindow):
    """Send-dialog recipient book: a repeat address (labelled in one branch,
    empty in another) resolves to ONE entry keeping the label — so the "to"
    field still finds it by label."""
    mainwindow.store.accounts = [
        {"address": "0xAA", "source": "ledger", "path": "p1", "label": "my"},
        {"address": "0xAA", "source": "qr", "path": "p2", "label": ""},
    ]
    assert dict(mainwindow.account_book()) == {"0xAA": "my"}
