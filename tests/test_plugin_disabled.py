"""MainWindow with optional plugins disabled — the on/off proof.

Builds the real window with tokens/ens switched off (and each alone) and
asserts it comes up mounting only the enabled plugins, the disabled ones are
absent (never constructed), the host methods that used to reach into Tokens
degrade to None, the shared IconCache still works, the sign path is intact, and
teardown is clean.
"""

import pytest


def _window(qtbot, disabled, tmp_qeth, fake_rpc):
    from qeth.store import Store
    from qeth.ui import MainWindow
    store = Store.load()
    store.disabled_plugins = set(disabled)
    win = MainWindow(store, fake_rpc)
    qtbot.addWidget(win)
    return win


def _right_tabs(win):
    tb = win.right_slot._tab_bar
    return [tb.tabText(i) for i in range(tb.count())]


@pytest.mark.usefixtures("hermetic_mainwindow")
def test_tokens_and_ens_off(qtbot, tmp_qeth, fake_rpc):
    win = _window(qtbot, {"tokens", "ens"}, tmp_qeth, fake_rpc)

    # Only the required plugins are mounted; the disabled ones are absent.
    assert set(win.plugins) == {"wallets", "transactions", "approvals"}
    assert win.tokens_plugin is None
    assert win.ens_plugin is None
    assert win.plugin("tokens") is None and win.plugin("ens") is None
    assert _right_tabs(win) == ["Transactions", "Approvals"]

    # Host methods that used to reach into Tokens degrade to None cleanly.
    addr = "0x" + "ab" * 20
    assert win.token_info(1, addr) is None
    assert win.native_price_usd(1, addr) is None
    # The IconCache is host-owned, so it survives Tokens being gone.
    assert win.icon_cache() is not None

    # The sign path (composer kwargs) depends only on Transactions + host
    # methods — it must still assemble with Tokens off.
    kwargs = win._composer_shared_kwargs(win.current_chain(), addr)
    assert kwargs["abi_source"] is not None
    assert kwargs["icon_cache"] is win.icon_cache()

    # Chain selector stays visible (Transactions doesn't hide it), and teardown
    # shuts down exactly the mounted plugins without error.
    m = win._manifest_for(win.right_slot.active())
    assert m is not None and not m.hides_chain_selector
    win._join_workers()


@pytest.mark.usefixtures("hermetic_mainwindow")
def test_only_tokens_off_keeps_ens(qtbot, tmp_qeth, fake_rpc):
    win = _window(qtbot, {"tokens"}, tmp_qeth, fake_rpc)
    assert set(win.plugins) == {"wallets", "transactions", "ens", "approvals"}
    assert win.tokens_plugin is None and win.ens_plugin is not None
    assert _right_tabs(win) == ["Transactions", "ENS", "Approvals"]
    win._join_workers()


@pytest.mark.usefixtures("hermetic_mainwindow")
def test_only_ens_off_keeps_tokens(qtbot, tmp_qeth, fake_rpc):
    win = _window(qtbot, {"ens"}, tmp_qeth, fake_rpc)
    assert set(win.plugins) == {"wallets", "tokens", "transactions", "approvals"}
    assert win.ens_plugin is None and win.tokens_plugin is not None
    assert _right_tabs(win) == ["Tokens", "Transactions", "Approvals"]
    # Tokens present → its icon cache is the host's shared instance.
    assert win.tokens_plugin.icon_cache is win.icon_cache()
    win._join_workers()


@pytest.mark.usefixtures("hermetic_mainwindow")
def test_approvals_off(qtbot, tmp_qeth, fake_rpc):
    win = _window(qtbot, {"approvals"}, tmp_qeth, fake_rpc)
    assert "approvals" not in win.plugins
    assert win.plugin("approvals") is None
    assert _right_tabs(win) == ["Tokens", "Transactions", "ENS"]
    win._join_workers()
