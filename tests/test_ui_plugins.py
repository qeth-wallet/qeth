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
        plugin._on_page_fetched(
            ETH.chain_id, ADDR.lower(), 1, new_only, True,
        )

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
        plugin._on_page_fetched(
            ETH.chain_id, ADDR.lower(), 1, fetched, True,
        )

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

        rows = [
            _mk("aa", ADDR, 100),               # sent by us
            _mk("bb", "0xstrangeaddress1", 7),  # received
            _mk("cc", "0xstrangeaddress2", 3),  # received
        ]

        class _Source:
            def supports(self, _c):
                return True

            def list_transactions(self, _c, _a, page=1, limit=50):
                return rows

        emitted: list[tuple] = []
        worker = TransactionsWorker(_Source(), ETH, ADDR, page=1)
        worker.fetched.connect(
            lambda cid, addr, idx, txs, more: emitted.append(
                (cid, addr, idx, list(txs), more)
            )
        )
        worker.run()

        assert len(emitted) == 1
        cid, addr, idx, txs, more = emitted[0]
        # Only the sent row survives the filter.
        assert [t.hash for t in txs] == ["0x" + "aa" * 32]
        assert (cid, addr, idx) == (1, ADDR.lower(), 1)

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
                return rows

        emitted = []
        worker = TransactionsWorker(
            _Source(), ETH, ADDR, page=1, sent_only=False,
        )
        worker.fetched.connect(
            lambda _c, _a, _i, txs, _m: emitted.append(list(txs))
        )
        worker.run()

        # Filter off → both rows pass through unchanged.
        assert len(emitted[0]) == 2

    def test_initial_open_renders_whole_cache(self, qtbot, tmp_qeth):
        """The whole on-disk cache lands on the table in one shot when
        the view is first activated. Load-on-scroll handles only the
        network side (fetching older pages from Blockscout) — the
        cache itself is small enough to populate up-front and lets
        the user see everything they've already paged in."""
        from qeth.transactions_cache import TransactionCache
        big = [
            Transaction(
                chain_id=1, hash="0x" + format(i, "064x"),
                block_number=i, timestamp=i, nonce=i,
                from_addr=ADDR, to_addr="0xfeed",
                value_wei=0, gas_used=0, gas_price_wei=0,
                method_id="", input_data="0x", success=True,
            )
            for i in range(1000)
        ]
        TransactionCache().save(ETH.chain_id, ADDR, big)

        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        plugin.on_account_changed(ADDR)

        # Cache hydrated and entire row count on the table.
        key = (ETH.chain_id, ADDR.lower())
        assert len(plugin._cache[key]) == 1000
        assert plugin.widget().table.rowCount() == 1000
        assert plugin._displayed_count[key] == 1000

    def test_render_decoded_lays_out_python_signature(self, qtbot, tmp_qeth):
        """Decoded calls render with type annotations and the function
        name bold — the Python-flavoured ``name: type = value`` form."""
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QTextEdit
        from qeth.plugins.transactions import _render_decoded

        edit = QTextEdit()
        qtbot.addWidget(edit)
        _render_decoded(edit, {
            "function": "transfer",
            "args": [
                {"name": "_to", "type": "address",
                 "value": "0x5d6a4ba137d77df7c3cdd7131c430da5497c7ace"},
                {"name": "_value", "type": "uint256", "value": "500000000"},
            ],
        })
        text = edit.toPlainText()
        # The whole signature is present, in declaration order, with
        # type annotations between name and value.
        assert "transfer(" in text
        assert "_to: address = 0x5d6a4ba137d77df7c3cdd7131c430da5497c7ace" in text
        assert "_value: uint256 = 500000000" in text
        assert text.index("_to") < text.index("_value")

        # Walk the document and confirm the function name's run is
        # bold while the surrounding text isn't. Qt picks an
        # actually-bold-capable family via QFontDatabase, so this
        # checks the live rendered font weight rather than just an
        # HTML attribute the engine might silently drop.
        doc = edit.document()
        block = doc.firstBlock()
        weights_by_text: dict[str, int] = {}
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                weights_by_text[frag.text()] = frag.charFormat().fontWeight()
            it += 1
        # The "transfer" fragment is bold; the "(" fragment around
        # it is rendered at normal weight.
        assert weights_by_text.get("transfer", QFont.Normal) >= QFont.Bold
        assert weights_by_text.get("(", QFont.Bold) < QFont.Bold

    def test_double_click_opens_details_dialog(self, qtbot, tmp_qeth):
        """Double-clicking a row should open TransactionDetailsDialog
        with the right tx and chain wired through. Plugin no-ops the
        ABI fetch worker so the test doesn't hit Blockscout."""
        from qeth.plugins.transactions import (
            TransactionDetailsDialog, TransactionListPanel,
        )
        from qeth.transactions_cache import TransactionCache

        sample_tx = Transaction(
            chain_id=1, hash="0x" + "aa" * 32, block_number=100,
            timestamp=1779618611, nonce=42, from_addr=ADDR,
            to_addr="0xdac17f958d2ee523a2206206994597c13d831ec7",
            value_wei=0, gas_used=63197, gas_price_wei=10**9,
            method_id="0xa9059cbb",
            input_data=(
                "0xa9059cbb"
                "0000000000000000000000005d6a4ba137d77df7c3cdd7131c430da5497c7ace"
                "000000000000000000000000000000000000000000000000000000001dcd6500"
            ),
            success=True,
        )
        TransactionCache().save(ETH.chain_id, ADDR, [sample_tx])

        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        plugin.on_activated()

        # Capture the dialog the plugin opens.
        opened: list[TransactionDetailsDialog] = []
        orig_show = TransactionDetailsDialog.show

        def _capture(self):
            opened.append(self)
            # Don't actually call show() — keeps the test offscreen.

        try:
            TransactionDetailsDialog.show = _capture
            # Emulate double-click → plugin._show_tx_details.
            plugin._panel.tx_details_requested.emit(sample_tx)
        finally:
            TransactionDetailsDialog.show = orig_show

        assert len(opened) == 1
        dialog = opened[0]
        assert dialog.tx is sample_tx
        assert dialog.chain is ETH

    def test_tab_reactivation_preserves_table(self, qtbot, tmp_qeth):
        """Switching away to the Tokens tab and back must NOT throw
        away anything on screen. The plugin sees on_activated for the
        same (chain, addr) it already painted and leaves the panel
        untouched — Qt then preserves both contents and the scrollbar
        position, just like tabs in a browser."""
        from qeth.transactions_cache import TransactionCache
        cache = [
            Transaction(
                chain_id=1, hash="0x" + format(i, "064x"),
                block_number=i, timestamp=i, nonce=i,
                from_addr=ADDR, to_addr="0xfeed",
                value_wei=0, gas_used=0, gas_price_wei=0,
                method_id="", input_data="0x", success=True,
            )
            for i in range(300, 0, -1)
        ]
        TransactionCache().save(ETH.chain_id, ADDR, cache)

        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())

        # First activation paints the entire cache (300 rows).
        plugin.on_activated()
        first_count = plugin.widget().table.rowCount()
        assert first_count == 300

        # Tab switch away then back — second on_activated for the
        # same view must NOT touch the table.
        plugin.on_activated()
        assert plugin.widget().table.rowCount() == first_count

    def test_overlapping_fetch_auto_advances_to_new_data(self, qtbot, tmp_qeth):
        """When a fetched page contains only entries we already have
        (typical after a partial backfill from a prior session), the
        plugin auto-advances to the next page so the load-on-scroll
        UX doesn't dead-end silently. Stops once a page brings new
        rows OR Blockscout reports has_more=False."""
        from qeth.transactions_cache import TransactionCache

        def _mk(n: int) -> Transaction:
            return Transaction(
                chain_id=1, hash="0x" + format(n, "064x"),
                block_number=n, timestamp=n, nonce=n,
                from_addr=ADDR, to_addr="0xfeed",
                value_wei=0, gas_used=0, gas_price_wei=0,
                method_id="", input_data="0x", success=True,
            )

        # Pre-cache nonces 100..149 — that's two Blockscout pages
        # worth of overlap before the real new data shows up.
        cached = [_mk(n) for n in range(149, 99, -1)]
        TransactionCache().save(ETH.chain_id, ADDR, cached)

        class _Source:
            def __init__(self):
                self.calls: list[int] = []

            def supports(self, _c):
                return True

            def list_transactions(self, _c, _a, page=1, limit=50):
                self.calls.append(page)
                if page == 1:
                    return [_mk(n) for n in range(149, 99, -1)]  # overlap
                if page == 2:
                    return [_mk(n) for n in range(149, 99, -1)]  # overlap
                if page == 3:
                    return [_mk(n) for n in range(99, 49, -1)]   # new!
                return []

        source = _Source()
        plugin = TransactionsPlugin(source=source)
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        # Drive each kicked worker synchronously the moment it lands
        # on the host so the auto-advance chain runs end to end.
        original_start = host.start_worker

        def _start(worker):
            original_start(worker)
            worker.run()

        host.start_worker = _start

        plugin.on_activated()   # force_fetch=True path
        # Refresh-newest path: page 1 fetched but no auto-advance.
        assert source.calls == [1]

        # User scrolls past the cache → load-older path kicks in.
        # The plugin auto-advances through page-2 overlap and lands
        # on page 3, which carries genuinely new entries.
        key = (ETH.chain_id, ADDR.lower())
        plugin._displayed_count[key] = len(plugin._cache[key])
        plugin._on_scroll_bottom()
        assert source.calls == [1, 2, 3]
        # The newly-discovered nonces are now in the cache.
        merged = plugin._cache[key]
        assert min(t.nonce for t in merged) == 50

    def test_scroll_bottom_advances_to_next_page(self, qtbot, tmp_qeth):
        """The scrolled_to_bottom signal is what drives the
        load-on-scroll UX: each emission should kick off one
        single-page worker for the next unfetched page."""
        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())

        # Pretend page 1 landed with one sent tx: cursor advances to 2.
        seed = [Transaction(
            chain_id=1, hash="0x" + "aa" * 32, block_number=5,
            timestamp=1, nonce=5, from_addr=ADDR, to_addr="0xbeef",
            value_wei=0, gas_used=0, gas_price_wei=0,
            method_id="", input_data="0x", success=True,
        )]
        plugin._on_page_fetched(ETH.chain_id, ADDR.lower(), 1, seed, True)
        host.started_workers.clear()

        plugin._on_scroll_bottom()
        assert len(host.started_workers) == 1
        assert host.started_workers[0].page == 2

        # Once we record an exhausted history, further scrolls no-op.
        plugin._exhausted.add((ETH.chain_id, ADDR.lower()))
        host.started_workers.clear()
        plugin._on_scroll_bottom()
        assert host.started_workers == []

    def test_worker_signals_has_more_only_on_full_page(self, qtbot, tmp_qeth):
        """Blockscout returns a partial last page; the worker uses
        ``len(raw) >= page_size`` to detect it and tells the caller
        not to ask for more — that's what stops the load-on-scroll
        cascade gracefully."""
        from qeth.plugins.transactions import TransactionsWorker

        def _mk(suffix):
            return Transaction(
                chain_id=1, hash="0x" + suffix * 32, block_number=1,
                timestamp=1, nonce=1, from_addr=ADDR, to_addr="0xfeed",
                value_wei=0, gas_used=0, gas_price_wei=0,
                method_id="", input_data="0x", success=True,
            )

        # Page returns 3 rows out of a 5-row limit — that's a partial
        # page, meaning no more data on the wire.
        class _Source:
            def supports(self, _c):
                return True

            def list_transactions(self, _c, _a, page=1, limit=50):
                return [_mk("aa"), _mk("bb"), _mk("cc")]

        captured = []
        worker = TransactionsWorker(
            _Source(), ETH, ADDR, page=1, page_size=5,
        )
        worker.fetched.connect(
            lambda _c, _a, _i, _txs, more: captured.append(more)
        )
        worker.run()
        assert captured == [False]

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
