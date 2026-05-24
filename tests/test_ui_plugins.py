"""Tests for TokensPlugin + TransactionsPlugin in isolation.

These instantiate each plugin against a stub host so the lifecycle
hooks can be driven without a full MainWindow. They lock in the
plugin contract: who owns what, who fires when, and what the
plugins do in response to lifecycle calls.
"""

from typing import Optional

import pytest
from PySide6.QtCore import Qt

from qeth.chains import DEFAULT_CHAINS
from qeth.plugins.tokens import TokensPlugin
from qeth.transactions import Transaction
from qeth.plugins.transactions import TransactionsPlugin


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
        from qeth.plugins.transactions import TransactionListPanel
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

    def test_loads_from_disk_cache_on_account_change(self, qtbot, tmp_qeth):
        """First-time selection of an address with no in-memory entry
        should hydrate from the disk cache and render immediately —
        this is the anti-flicker behaviour that motivated the cache."""
        from qeth.transactions_cache import TransactionCache
        prewritten = [
            Transaction(
                chain_id=1, hash="0x" + "ab" * 32, block_number=10,
                timestamp=1, nonce=0, from_addr=ADDR,
                to_addr="0xbeefbeefbeefbeefbeefbeefbeefbeefbeefbeef",
                value_wei=10**18, gas_used=0, gas_price_wei=0,
                method_id="", input_data="0x", success=True,
            )
        ]
        TransactionCache().save(ETH.chain_id, ADDR, prewritten)

        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())

        # No in-memory cache, no fetch yet — but the disk cache should
        # populate the panel synchronously.
        plugin.on_account_changed(ADDR)
        assert plugin.widget().table.rowCount() == 1
        # And the in-memory cache should now hold the hydrated entries.
        assert (ETH.chain_id, ADDR.lower()) in plugin._cache

    def test_fetched_results_merge_with_cached_history(self, qtbot, tmp_qeth):
        """A new fetch only returns the most-recent window. The plugin
        should merge it with anything older the cache holds, so the
        displayed list grows over time rather than being truncated to
        the latest 50."""
        from qeth.transactions_cache import TransactionCache
        old_history = [
            Transaction(
                chain_id=1, hash="0x" + "11" * 32, block_number=5,
                timestamp=1, nonce=0, from_addr=ADDR, to_addr="0xbeef",
                value_wei=0, gas_used=0, gas_price_wei=0,
                method_id="", input_data="0x", success=True,
            )
        ]
        TransactionCache().save(ETH.chain_id, ADDR, old_history)

        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        # Hydrate in-memory cache from disk.
        plugin.on_account_changed(ADDR)

        # Simulate a fresh fetch returning a newer transaction only.
        new_only = [
            Transaction(
                chain_id=1, hash="0x" + "22" * 32, block_number=10,
                timestamp=2, nonce=1, from_addr=ADDR, to_addr="0xbeef",
                value_wei=0, gas_used=0, gas_price_wei=0,
                method_id="", input_data="0x", success=True,
            )
        ]
        plugin._on_page_fetched(ETH.chain_id, ADDR.lower(), new_only)

        merged = plugin._cache[(ETH.chain_id, ADDR.lower())]
        # Both entries present, newer first.
        assert [t.hash for t in merged] == [new_only[0].hash, old_history[0].hash]
        # And the disk reflects the merged state.
        reloaded = TransactionCache().load(ETH.chain_id, ADDR)
        assert [t.hash for t in reloaded] == [new_only[0].hash, old_history[0].hash]

    def test_fetched_results_get_persisted(self, qtbot, tmp_qeth):
        from qeth.transactions_cache import TransactionCache
        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())

        fetched = [
            Transaction(
                chain_id=1, hash="0x" + "cd" * 32, block_number=11,
                timestamp=2, nonce=1, from_addr=ADDR,
                to_addr="0xbeef", value_wei=0, gas_used=0,
                gas_price_wei=0, method_id="", input_data="0x",
                success=True,
            )
        ]
        plugin._on_page_fetched(ETH.chain_id, ADDR.lower(), fetched)

        # On disk now — a fresh TransactionCache instance can read it.
        reloaded = TransactionCache().load(ETH.chain_id, ADDR)
        assert reloaded is not None
        assert len(reloaded) == 1
        assert reloaded[0].hash == fetched[0].hash

    def test_is_full_history(self):
        """Cache completeness is derived from the data itself: sent
        nonces are strictly monotonic per sender, so nonce 0 present
        + contiguous range = the entire outgoing history."""
        from qeth.plugins.transactions import _is_full_history

        def _t(n):
            return Transaction(
                chain_id=1, hash="0x" + format(n, "064x"),
                block_number=n, timestamp=n, nonce=n,
                from_addr=ADDR, to_addr="0xfeed",
                value_wei=0, gas_used=0, gas_price_wei=0,
                method_id="", input_data="0x", success=True,
            )

        assert _is_full_history([]) is False
        # Has nonce 0 and contiguous → complete.
        assert _is_full_history([_t(0), _t(1), _t(2)]) is True
        assert _is_full_history([_t(0)]) is True
        # Missing nonce 0 (older history missing) → incomplete.
        assert _is_full_history([_t(5), _t(6), _t(7)]) is False
        # Gap in the middle → incomplete.
        assert _is_full_history([_t(0), _t(1), _t(3)]) is False

    def test_worker_filters_to_sent_only_by_default(self, qtbot, tmp_qeth):
        """Sent-only is the right default — received txs carry the
        sender's nonce, which would interleave non-monotonically with
        the wallet's own nonces and break the sort-by-nonce order."""
        from qeth.plugins.transactions import TransactionsWorker

        def _mk(hash_suffix: str, sender: str, nonce: int) -> Transaction:
            return Transaction(
                chain_id=1, hash="0x" + hash_suffix * 32,
                block_number=nonce, timestamp=nonce, nonce=nonce,
                from_addr=sender.lower(),
                to_addr="0xfeedfeedfeedfeedfeedfeedfeedfeedfeedfeed",
                value_wei=0, gas_used=0, gas_price_wei=0,
                method_id="", input_data="0x", success=True,
            )

        # Mixed page: one sent + two received with their senders'
        # (unrelated) nonces.
        page1 = [
            _mk("aa", ADDR, 100),               # sent by us
            _mk("bb", "0xstrangeaddress1", 7),  # received
            _mk("cc", "0xstrangeaddress2", 3),  # received
        ]

        class _Source:
            def __init__(self):
                self.calls = 0

            def supports(self, _c):
                return True

            def list_transactions(self, chain, address, page=1, limit=50):
                self.calls += 1
                return page1 if page == 1 else []

        emitted: list[list[Transaction]] = []
        worker = TransactionsWorker(
            _Source(), ETH, ADDR, page_pause_s=0,
        )
        worker.page_fetched.connect(
            lambda _c, _a, p: emitted.append(list(p))
        )
        worker.run()

        # Only the sent row survives the filter.
        assert len(emitted) == 1
        assert [t.hash for t in emitted[0]] == ["0x" + "aa" * 32]

    def test_worker_filter_can_be_disabled(self, qtbot, tmp_qeth):
        from qeth.plugins.transactions import TransactionsWorker

        def _mk(suffix, sender):
            return Transaction(
                chain_id=1, hash="0x" + suffix * 32, block_number=1,
                timestamp=1, nonce=1, from_addr=sender.lower(),
                to_addr="0xfeed", value_wei=0, gas_used=0,
                gas_price_wei=0, method_id="", input_data="0x",
                success=True,
            )

        rows = [_mk("aa", ADDR), _mk("bb", "0xother")]

        class _Source:
            def supports(self, _c):
                return True

            def list_transactions(self, _c, _a, page=1, limit=50):
                return rows if page == 1 else []

        emitted = []
        worker = TransactionsWorker(
            _Source(), ETH, ADDR, page_pause_s=0, sent_only=False,
        )
        worker.page_fetched.connect(
            lambda _c, _a, p: emitted.append(list(p))
        )
        worker.run()

        # Filter off → both rows pass through unchanged.
        assert len(emitted[0]) == 2

    def test_paginating_worker_early_exits_on_known_hash(self, qtbot, tmp_qeth):
        """When ``walk_to_end=False`` the worker stops the moment a
        page contains a hash from ``known_hashes``. This is the
        incremental-refresh path used after the cache has been fully
        backfilled at least once: page 1 typically overlaps prior
        history and we're done in one HTTP call."""
        from qeth.plugins.transactions import TransactionsWorker

        def _mk(hash_suffix: str, nonce: int) -> Transaction:
            return Transaction(
                chain_id=1, hash="0x" + hash_suffix * 32,
                block_number=nonce, timestamp=nonce,
                nonce=nonce, from_addr=ADDR, to_addr="0xbeef",
                value_wei=0, gas_used=0, gas_price_wei=0,
                method_id="", input_data="0x", success=True,
            )

        page1 = [_mk("aa", 5), _mk("bb", 4), _mk("cc", 3)]   # all new
        page2 = [_mk("dd", 2), _mk("known", 1)]              # overlap
        page3 = [_mk("ee", 0)]                               # not reached

        class _FakeSource:
            def __init__(self):
                self.calls: list[int] = []

            def supports(self, _chain):
                return True

            def list_transactions(self, chain, address, page=1, limit=50):
                self.calls.append(page)
                if page == 1:
                    return page1
                if page == 2:
                    return page2
                return page3

        source = _FakeSource()
        worker = TransactionsWorker(
            source, ETH, ADDR,
            known_hashes={"0x" + "known" * 32}, page_pause_s=0,
            walk_to_end=False,
        )
        emitted: list[list[Transaction]] = []
        worker.page_fetched.connect(
            lambda _cid, _addr, p: emitted.append(list(p))
        )
        completed: list[tuple] = []
        worker.completed.connect(
            lambda cid, addr: completed.append((cid, addr))
        )

        # Run synchronously so the test doesn't race the QThread.
        worker.run()

        assert source.calls == [1, 2]   # page 3 never fetched
        assert len(emitted) == 2
        assert emitted[0] == page1
        assert emitted[1] == page2
        assert completed == [(1, ADDR.lower())]

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
    from qeth.plugins import tokens as tp

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
        from qeth.plugins.tokens import TokenListPanel
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


# --- WalletsPlugin ----------------------------------------------------------

@pytest.fixture
def wallets_plugin(qtbot, tmp_qeth):
    from qeth.store import Store
    from qeth.plugins.wallets import WalletsPlugin
    store = Store.load()
    plugin = WalletsPlugin(store)
    qtbot.addWidget(plugin.widget())
    return plugin


class TestWalletsPlugin:
    def test_widget_holds_tree_and_details(self, wallets_plugin):
        from qeth.plugins.wallets import DetailsPanel
        from PySide6.QtWidgets import QTreeWidget
        assert isinstance(wallets_plugin._tree, QTreeWidget)
        assert isinstance(wallets_plugin._details, DetailsPanel)

    def test_action_widgets_returns_empty(self, wallets_plugin):
        # Wallets' action row sits at the TOP of its own widget, not
        # on the slot's bottom row — so action_widgets() is intentionally
        # empty.
        assert wallets_plugin.action_widgets() == []

    def test_actions_are_wired(self, wallets_plugin):
        assert wallets_plugin.act_add is not None
        assert wallets_plugin.act_copy is not None
        assert wallets_plugin.act_remove is not None
        # Copy/Remove start disabled (no selection yet).
        assert not wallets_plugin.act_copy.isEnabled()
        assert not wallets_plugin.act_remove.isEnabled()

    def test_selection_emits_address_signal(self, qtbot, wallets_plugin):
        addr = "0x7a16ff8270133f063aab6c9977183d9e72835428"
        wallets_plugin._store.add_account({
            "address": addr, "path": "44'/60'/0'/0/0",
            "source": "ledger", "scheme": "BIP-44", "label": "",
        })
        wallets_plugin.rebuild_tree()
        matches = wallets_plugin._tree.findItems(
            addr, Qt.MatchContains | Qt.MatchRecursive, 0
        )
        # rebuild_tree auto-selects the default account (the one we
        # just added). Clear so the test can re-select it and observe
        # the signal firing.
        wallets_plugin._tree.clearSelection()
        with qtbot.waitSignal(
            wallets_plugin.selected_address_changed, timeout=500
        ) as blocker:
            wallets_plugin._tree.setCurrentItem(matches[0])
        assert blocker.args == [addr]

    def test_clearing_selection_emits_none(self, qtbot, wallets_plugin):
        addr = "0x7a16ff8270133f063aab6c9977183d9e72835428"
        wallets_plugin._store.add_account({
            "address": addr, "path": "44'/60'/0'/0/0",
            "source": "ledger", "scheme": "BIP-44", "label": "",
        })
        wallets_plugin.rebuild_tree()
        matches = wallets_plugin._tree.findItems(
            addr, Qt.MatchContains | Qt.MatchRecursive, 0
        )
        wallets_plugin._tree.setCurrentItem(matches[0])
        with qtbot.waitSignal(
            wallets_plugin.selected_address_changed, timeout=500
        ) as blocker:
            wallets_plugin._tree.clearSelection()
        assert blocker.args == [None]

    def test_splitter_state_round_trip(self, wallets_plugin):
        hex_state = wallets_plugin.splitter_state()
        assert hex_state  # non-empty
        # Restore is best-effort and doesn't raise on garbage.
        wallets_plugin.restore_splitter_state("not-valid-hex")
        wallets_plugin.restore_splitter_state(hex_state)
