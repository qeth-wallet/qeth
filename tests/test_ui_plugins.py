"""Tests for TokensPlugin + TransactionsPlugin in isolation.

These instantiate each plugin against a stub host so the lifecycle
hooks can be driven without a full MainWindow. They lock in the
plugin contract: who owns what, who fires when, and what the
plugins do in response to lifecycle calls.
"""

from typing import Optional

import pytest

from qeth.chains import DEFAULT_CHAINS
from qeth.tokens_plugin import TokensPlugin
from qeth.transactions import Transaction
from qeth.transactions_plugin import TransactionsPlugin


ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
ADDR = "0x7a16ff8270133f063aab6c9977183d9e72835428"


class _StubHost:
    """Minimal Host-shaped stand-in. Tracks workers it would have
    started so tests can inspect them, but never actually starts
    them (each test's hermetic monkeypatching neutralizes ``run``).
    """
    def __init__(self, chain=ETH, address: Optional[str] = None):
        self._chain = chain
        self.selected_address = address
        self.started_workers: list = []
        self.status_calls: list[tuple[str, int]] = []

    def current_chain(self):
        return self._chain

    def start_worker(self, worker):
        self.started_workers.append(worker)

    def status_message(self, text: str, timeout_ms: int = 3000) -> None:
        self.status_calls.append((text, timeout_ms))


# --- TransactionsPlugin ----------------------------------------------------

class TestTransactionsPlugin:
    def test_widget_returns_transaction_panel(self, qtbot, tmp_qeth):
        from qeth.ui import TransactionListPanel
        plugin = TransactionsPlugin()
        w = plugin.widget()
        qtbot.addWidget(w)
        assert isinstance(w, TransactionListPanel)

    def test_account_change_to_none_clears_panel(self, qtbot, tmp_qeth):
        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        plugin._cache[(ETH.chain_id, ADDR.lower())] = [
            Transaction(
                chain_id=1, hash="0x" + "ab" * 32, block_number=1, timestamp=1,
                nonce=0, from_addr=ADDR, to_addr="0xbeef",
                value_wei=0, gas_used=0, gas_price_wei=0,
                method_id="", input_data="0x", success=True,
            )
        ]
        plugin.on_account_changed(ADDR)
        assert plugin.widget().table.rowCount() == 1
        plugin.on_account_changed(None)
        assert plugin.widget().table.rowCount() == 0

    def test_cached_account_renders_immediately(self, qtbot, tmp_qeth):
        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())

        plugin._cache[(ETH.chain_id, ADDR.lower())] = [
            Transaction(
                chain_id=1, hash="0x" + "ab" * 32, block_number=1, timestamp=1,
                nonce=0, from_addr=ADDR,
                to_addr="0xbeefbeefbeefbeefbeefbeefbeefbeefbeefbeef",
                value_wei=10**18, gas_used=0, gas_price_wei=0,
                method_id="", input_data="0x", success=True,
            )
        ]
        plugin.on_account_changed(ADDR)
        # Cached → rendered immediately without a worker fetch (plugin
        # not active in this test, so no fetch should fire anyway).
        assert plugin.widget().table.rowCount() == 1

    def test_unsupported_chain_shows_error_when_activated(self, qtbot, tmp_qeth):
        from qeth.chains import Chain
        fake_chain = Chain(name="Fake", chain_id=999_999, rpc_url="https://x")
        plugin = TransactionsPlugin()
        host = _StubHost(chain=fake_chain, address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        # Pretend the panel is visible so on_activated takes the fetch path.
        plugin.widget().show()
        plugin.on_activated()
        # No host.start_worker should have been called — supports() returned
        # False, so we short-circuited with an error message.
        assert host.started_workers == []
        assert not plugin.widget().status_lbl.isHidden()
        assert "aren't available" in plugin.widget().status_lbl.text()


# --- TokensPlugin -----------------------------------------------------------

@pytest.fixture
def tokens_plugin(qtbot, tmp_qeth, monkeypatch):
    """A TokensPlugin with all background workers neutralized."""
    from qeth import tokens_plugin as tp

    def _noop_run(self):
        return

    for cls_name in (
        "TokenListsLoader", "TokenListWorker", "BalanceWorker",
        "PricesWorker", "RiskWorker", "MetadataWorker",
    ):
        cls = getattr(tp, cls_name)
        monkeypatch.setattr(cls, "run", _noop_run)

    from qeth.store import Store
    store = Store.load()
    plugin = TokensPlugin(store)
    qtbot.addWidget(plugin.widget())
    return plugin


class TestTokensPlugin:
    def test_widget_returns_token_panel(self, tokens_plugin):
        from qeth.ui import TokenListPanel
        assert isinstance(tokens_plugin.widget(), TokenListPanel)

    def test_action_widgets_are_panel_buttons(self, tokens_plugin):
        actions = tokens_plugin.action_widgets()
        panel = tokens_plugin.widget()
        # The plugin exposes exactly the panel's four +/-/star/eye buttons.
        assert panel.btn_add in actions
        assert panel.btn_hide in actions
        assert panel.btn_pin in actions
        assert panel.btn_show_all in actions

    def test_attach_starts_refresh_timer_and_lists_loader(self, tokens_plugin):
        host = _StubHost()
        tokens_plugin.attach(host)
        # Refresh timer running.
        assert tokens_plugin._refresh_timer is not None
        assert tokens_plugin._refresh_timer.isActive()
        # Lists loader handed to the host.
        assert tokens_plugin._lists_loader in host.started_workers

    def test_account_change_to_none_clears_panel(self, tokens_plugin):
        host = _StubHost(address=ADDR)
        tokens_plugin.attach(host)
        # Populate the panel so we can see clear() do something.
        from qeth.tokens import TokenBalance
        tokens_plugin.widget().show_balances(ETH, 10**18, [], {})
        assert tokens_plugin.widget().table.rowCount() == 1

        tokens_plugin.on_account_changed(None)
        assert tokens_plugin.widget().table.rowCount() == 0
        assert tokens_plugin._displayed_view is None

    def test_show_all_toggle_sets_state(self, tokens_plugin):
        host = _StubHost()
        tokens_plugin.attach(host)
        assert tokens_plugin._show_all is False
        tokens_plugin._on_show_all_toggled(True)
        assert tokens_plugin._show_all is True

    def test_hide_token_persists_to_store(self, tokens_plugin):
        host = _StubHost()
        tokens_plugin.attach(host)
        scam = "0xdeadbeef00000000000000000000000000000001"
        tokens_plugin._on_hide_token(1, scam)
        assert tokens_plugin._store.is_hidden(1, scam)

    def test_pin_token_persists_to_store(self, tokens_plugin):
        host = _StubHost()
        tokens_plugin.attach(host)
        good = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        tokens_plugin._on_pin_token(1, good)
        assert tokens_plugin._store.is_force_shown(1, good)
