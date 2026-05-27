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

    def test_pick_mono_font_returns_family_with_bold_variant(self, qtbot):
        """The function name in the decoded-call view stays bold only
        if the chosen monospace family ships a Bold style. The CSS
        ``monospace`` alias on some Linux systems resolves to a
        Regular-only family — must pick a richer one."""
        from PySide6.QtGui import QFontDatabase
        from qeth.plugins.transactions import _pick_mono_font

        font = _pick_mono_font()
        family = font.family()
        styles = QFontDatabase.styles(family)
        # If this fails the test machine literally has no monospace
        # family with bold installed — accept the fallback. But on
        # any normal dev box at least one of DejaVu / Liberation /
        # Noto / etc. should be present.
        if family != "monospace":
            assert any("bold" in s.lower() for s in styles), (
                f"picked font {family!r} has no Bold style: {styles}"
            )

    def test_render_decoded_uses_fixed_pitch_font(self, qtbot, tmp_qeth):
        """Regression: QFontDatabase.systemFont(FixedFont) returned
        ``Ubuntu`` (not actually fixed-pitch) on the developer's
        system, so columns didn't align. The renderer must pick a
        font whose resolved info reports fixedPitch=True."""
        from PySide6.QtGui import QFontInfo
        from PySide6.QtWidgets import QTextEdit
        from qeth.plugins.transactions import _render_decoded

        edit = QTextEdit()
        qtbot.addWidget(edit)
        _render_decoded(edit, {
            "function": "transfer",
            "args": [{"name": "_to", "type": "address", "value": "0x…"}],
        })
        # Walk the first fragment and confirm its font resolves to
        # something Qt's QFontInfo says is fixed-pitch.
        doc = edit.document()
        block = doc.firstBlock()
        it = block.begin()
        frag = it.fragment()
        assert frag.isValid()
        info = QFontInfo(frag.charFormat().font())
        assert info.fixedPitch(), (
            f"Decoded-call font {info.family()!r} is not fixed-pitch — "
            "columns will misalign in the dialog"
        )

    def test_render_decoded_nests_struct_with_indent(self, qtbot, tmp_qeth):
        """Tuple args expand into an indented Python-dict-style
        block: child fields one level deeper, each with its own type
        annotation, closing brace at the parent's indent level."""
        from PySide6.QtWidgets import QTextEdit
        from qeth.plugins.transactions import _render_decoded

        edit = QTextEdit()
        qtbot.addWidget(edit)
        _render_decoded(edit, {
            "function": "register",
            "args": [{
                "name": "data", "type": "tuple", "children": [
                    # Renderer just emits the values verbatim — the
                    # _stringify step is what adds quotes around
                    # string types. We pass the already-quoted form.
                    {"name": "label", "type": "string", "value": '"qeth"'},
                    {"name": "secret", "type": "bytes32",
                     "value": "0x99" + "ff" * 31},
                ],
            }],
        })
        text = edit.toPlainText()
        lines = text.splitlines()
        # Layout: function(\n   …last child without trailing comma\n   …\n)
        assert lines[0] == "register("
        assert lines[1] == "    data: tuple = {"
        # First child has a comma; last child does not. The closing
        # brace also has no trailing comma because data is the last
        # (and only) top-level arg.
        assert lines[2] == '        label: string = "qeth",'
        assert lines[3].startswith("        secret: bytes32 = 0x99")
        assert not lines[3].endswith(",")
        assert lines[4] == "    }"
        assert lines[5] == ")"

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

        # Pre-cache nonces 100..199 — pages of overlap before the
        # real new data. Sized to two full page_size=100 worker
        # batches so the auto-advance walker has something to chew.
        cached = [_mk(n) for n in range(199, 99, -1)]
        TransactionCache().save(ETH.chain_id, ADDR, cached)

        class _Source:
            def __init__(self):
                self.calls: list[int] = []

            def supports(self, _c):
                return True

            def list_transactions(self, _c, _a, page=1, limit=50):
                # Honour ``limit`` — has_more (page is full) is
                # what tells the plugin to keep walking. A hard-
                # coded 50-row return would let the worker decide
                # has_more=False on a 100-row limit and short-
                # circuit the test we're trying to exercise.
                self.calls.append(page)
                if page == 1:
                    return [_mk(n) for n in range(199, 199 - limit, -1)]
                if page == 2:
                    return [_mk(n) for n in range(99, 99 - limit, -1)
                            if n >= 0]
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
        # Page 1 is pure overlap with the disk cache → 0 new rows
        # triggers the auto-advance. Page 2 returns the older
        # nonces, which DO add a full batch of new rows → walker
        # stops.
        assert source.calls == [1, 2]
        key = (ETH.chain_id, ADDR.lower())
        merged = plugin._cache[key]
        assert min(t.nonce for t in merged) == 0

    def test_scroll_bottom_advances_to_next_page(self, qtbot, tmp_qeth):
        """The scrolled_to_bottom signal is what drives the
        load-on-scroll UX: each emission should kick off one
        single-page worker for the next unfetched page."""
        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())

        # Seed page 1 with INITIAL_BATCH-many sent rows so the
        # auto-walk-to-fill-batch doesn't fire from the seed
        # itself — we want this test to exercise the scroll
        # mechanic, not the fill-on-thin-yield walker.
        seed = [Transaction(
            chain_id=1, hash="0x" + format(n, "064x"), block_number=n,
            timestamp=n, nonce=n, from_addr=ADDR, to_addr="0xbeef",
            value_wei=0, gas_used=0, gas_price_wei=0,
            method_id="", input_data="0x", success=True,
        ) for n in range(plugin.INITIAL_BATCH, 0, -1)]
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


class TestOnReceiptConfirmed:
    """Plugin slot driven by PendingTxWatcher: replace the pending
    entry in the cache with one built from the receipt, persist,
    and re-render the panel if it's currently showing the view."""

    def _pending(self, **overrides) -> Transaction:
        base = dict(
            chain_id=1, hash="0x" + "ab" * 32, block_number=0,
            timestamp=1700, nonce=5, from_addr=ADDR, to_addr="0xbeef",
            value_wei=0, gas_used=0, gas_price_wei=2 * 10**9,
            method_id="", input_data="0x", success=True, pending=True,
        )
        base.update(overrides)
        return Transaction(**base)

    def _confirmed_receipt(self):
        return {
            "blockNumber": "0x1234",
            "gasUsed": "0xc350",
            "status": "0x1",
            "effectiveGasPrice": "0x77359400",
        }

    def test_replaces_pending_entry_and_persists(self, qtbot, tmp_qeth):
        plugin = TransactionsPlugin()
        qtbot.addWidget(plugin.widget())
        key = (ETH.chain_id, ADDR.lower())
        plugin._cache[key] = [self._pending()]
        plugin._disk_cache.save(*key, plugin._cache[key])

        plugin._on_receipt_confirmed(ETH, plugin._cache[key][0].hash,
                                      self._confirmed_receipt())

        tx = plugin._cache[key][0]
        assert tx.pending is False
        assert tx.success is True
        assert tx.block_number == 0x1234
        # Disk round-trip too.
        disk = plugin._disk_cache.load(*key)
        assert disk[0].pending is False
        assert disk[0].block_number == 0x1234

    def test_bails_when_entry_already_confirmed(self, qtbot, tmp_qeth):
        """Race: Blockscout refresh got the confirmed entry into the
        cache before the receipt poll. ``merge_txs`` dedup overrode
        the pending entry; the slot must no-op rather than rewriting
        with potentially-staler data."""
        plugin = TransactionsPlugin()
        qtbot.addWidget(plugin.widget())
        key = (ETH.chain_id, ADDR.lower())
        confirmed = self._pending(block_number=999, pending=False)
        plugin._cache[key] = [confirmed]
        plugin._on_receipt_confirmed(ETH, confirmed.hash,
                                      self._confirmed_receipt())
        # Untouched — same object kept.
        assert plugin._cache[key][0] is confirmed
        assert plugin._cache[key][0].block_number == 999

    def test_no_match_in_cache_is_a_noop(self, qtbot, tmp_qeth):
        """Hash isn't in the cache (e.g. another app removed it).
        Shouldn't raise, shouldn't mutate."""
        plugin = TransactionsPlugin()
        qtbot.addWidget(plugin.widget())
        other = self._pending(hash="0x" + "cd" * 32)
        plugin._cache[(ETH.chain_id, ADDR.lower())] = [other]
        plugin._on_receipt_confirmed(ETH, "0x" + "ee" * 32,
                                      self._confirmed_receipt())
        # Still pending, still in cache.
        assert plugin._cache[(ETH.chain_id, ADDR.lower())][0].pending is True

    def test_rerenders_panel_when_view_is_active(self, qtbot, tmp_qeth):
        """If ``_rendered_for`` matches the cache key, the panel is
        re-rendered so the row's status glyph flips from ⏳ to ✓."""
        plugin = TransactionsPlugin()
        panel = plugin.widget()
        qtbot.addWidget(panel)
        key = (ETH.chain_id, ADDR.lower())
        plugin._cache[key] = [self._pending()]
        panel.set_context(ETH, ADDR)
        panel.show_transactions(plugin._cache[key])
        plugin._rendered_for = key
        assert panel.table.item(0, 0).text() == "⏳"

        plugin._on_receipt_confirmed(ETH, plugin._cache[key][0].hash,
                                      self._confirmed_receipt())
        assert panel.table.item(0, 0).text() == "✓"


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


class TestSiblingHeldContracts:
    """Cross-wallet token cross-check: when refreshing wallet B, we
    extend the multicall set with contracts that any of our OTHER
    wallets hold a positive balance of on the same chain. Catches
    intra-wallet transfers ahead of Blockscout's next discovery
    cycle (which can lag by minutes)."""

    USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    DAI = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
    A = "0x7a16ff8270133f063aab6c9977183d9e72835428"
    B = "0xa9D1e08C7793af67e9d92fe308d5697FB81d3E43"

    def _populate(self, plugin, *, sibling_addr, contracts_with_balance,
                  contracts_zero=()):
        from qeth.wallet_cache import CachedToken, CachedWallet
        # Register the sibling as one of "our" accounts.
        plugin._store.add_account({
            "address": sibling_addr, "source": "watch_only",
            "label": "Sibling",
        })
        cw = CachedWallet(
            chain_id=1, address=sibling_addr.lower(),
            native_balance_wei=0,
        )
        for c in contracts_with_balance:
            cw.tokens.append(CachedToken(
                contract=c.lower(), symbol="X", name="X",
                decimals=6, logo_uri=None, balance_raw=1_000_000,
                price_usd=None, balance_updated=0, price_updated=0,
            ))
        for c in contracts_zero:
            cw.tokens.append(CachedToken(
                contract=c.lower(), symbol="X", name="X",
                decimals=6, logo_uri=None, balance_raw=0,
                price_usd=None, balance_updated=0, price_updated=0,
            ))
        plugin._wallet_cache.save(cw)

    def test_returns_contracts_held_by_other_wallets(
        self, tokens_plugin,
    ):
        self._populate(
            tokens_plugin, sibling_addr=self.B,
            contracts_with_balance=[self.USDC, self.DAI],
        )
        out = tokens_plugin._sibling_held_contracts(1, self.A)
        assert {c.lower() for c in out} == {
            self.USDC.lower(), self.DAI.lower(),
        }

    def test_excludes_self_address(self, tokens_plugin):
        # Cache the CURRENT wallet too — it must NOT appear in its
        # own sibling list (would cause double-counting).
        self._populate(
            tokens_plugin, sibling_addr=self.A,
            contracts_with_balance=[self.USDC],
        )
        out = tokens_plugin._sibling_held_contracts(1, self.A)
        assert out == set()

    def test_includes_zero_balance_holdings(self, tokens_plugin):
        # A wallet that recently sent its full USDC stash to another
        # of our wallets would have balance_raw = 0 in cache. If we
        # filtered those out, the recipient's cross-check would
        # never query USDC and we'd miss the inbound. Include them.
        self._populate(
            tokens_plugin, sibling_addr=self.B,
            contracts_with_balance=[self.USDC],
            contracts_zero=[self.DAI],
        )
        out = tokens_plugin._sibling_held_contracts(1, self.A)
        assert {c.lower() for c in out} == {
            self.USDC.lower(), self.DAI.lower(),
        }

    def test_scoped_to_chain_id(self, tokens_plugin):
        # Sibling caches are per (chain, address). A DAI holding on
        # mainnet must not leak into the Polygon multicall set.
        self._populate(
            tokens_plugin, sibling_addr=self.B,
            contracts_with_balance=[self.USDC],
        )
        out_polygon = tokens_plugin._sibling_held_contracts(137, self.A)
        assert out_polygon == set()

    def test_handles_no_other_wallets(self, tokens_plugin):
        # Only the current wallet in the store — must return empty,
        # never raise.
        tokens_plugin._store.add_account({
            "address": self.A, "source": "ledger", "label": "Me",
        })
        out = tokens_plugin._sibling_held_contracts(1, self.A)
        assert out == set()


# --- WalletsPlugin ----------------------------------------------------------

@pytest.fixture
def wallets_plugin(qtbot, tmp_qeth):
    from qeth.store import Store
    from qeth.plugins.wallets import WalletsPlugin
    store = Store.load()
    plugin = WalletsPlugin(store)
    qtbot.addWidget(plugin.widget())
    return plugin


class TestTokensStartupNonBlocking:
    """Pin the no-wait-for-token-lists startup behaviour: when the
    wallet cache holds tokens for the selected view, the panel must
    render them straight away, even when the curated token lists
    haven't loaded yet (slow network)."""

    def test_cached_wallet_renders_before_lists_load(self, qtbot, tmp_qeth):
        from qeth.plugins.tokens import TokensPlugin
        from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache

        # Pre-populate the wallet cache for a real ledger account.
        wallet_addr = "0x7a16ff8270133f063aab6c9977183d9e72835428"
        WalletCache().save(CachedWallet(
            chain_id=1,
            address=wallet_addr,
            native_balance_wei=10**18,
            tokens=[
                CachedToken(
                    contract="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                    symbol="USDC", name="USD Coin", decimals=6,
                    balance_raw=1_000_000,
                ),
            ],
        ))

        from qeth.store import Store
        store = Store.load()
        plugin = TokensPlugin(store)

        class _StubHost:
            selected_address = wallet_addr
            def current_chain(self):
                from qeth.chains import DEFAULT_CHAINS
                return next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
            def start_worker(self, w):
                pass   # don't actually start network workers
            def status_message(self, *a, **kw):
                pass

        plugin.attach(_StubHost())
        qtbot.addWidget(plugin.widget())

        # Crucial: lists haven't loaded (the test fixture neutralized
        # the TokenListsLoader worker). Without the cached-render
        # branch, the panel would sit on "Loading token lists…".
        assert plugin._token_lists.loaded is False
        plugin.on_account_changed(wallet_addr)

        # Native + cached ERC-20 → 2 rows immediately.
        assert plugin.widget().table.rowCount() == 2
        # And specifically the USDC symbol made it onto the table.
        symbols = [
            plugin.widget().table.item(r, 0).text()
            for r in range(plugin.widget().table.rowCount())
        ]
        assert "USDC" in symbols


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

    def test_select_address_focuses_matching_leaf(self, qtbot, wallets_plugin):
        """``select_address`` is what MainWindow calls after a
        broadcast so the user lands on the from account. Walks the
        tree and sets currentItem to the matching leaf. Case-
        insensitive to be lenient with whatever case the request
        carried."""
        addr1 = "0x7a16ff8270133f063aab6c9977183d9e72835428"
        addr2 = "0x" + "11" * 20
        wallets_plugin._store.add_account({
            "address": addr1, "path": "44'/60'/0'/0/0",
            "source": "ledger", "scheme": "BIP-44", "label": "",
        })
        wallets_plugin._store.add_account({
            "address": addr2, "path": "44'/60'/0'/0/1",
            "source": "ledger", "scheme": "BIP-44", "label": "",
        })
        wallets_plugin.rebuild_tree()
        # Select addr2 first, then explicitly switch to addr1 via
        # the API under test.
        matches = wallets_plugin._tree.findItems(
            addr2, Qt.MatchContains | Qt.MatchRecursive, 0
        )
        wallets_plugin._tree.setCurrentItem(matches[0])
        assert wallets_plugin.selected_address == addr2

        assert wallets_plugin.select_address(addr1.upper()) is True
        # Selection moved to addr1.
        assert wallets_plugin.selected_address == addr1

    def test_select_address_returns_false_when_missing(self, wallets_plugin):
        """Unknown address → no selection change, returns False."""
        addr = "0x7a16ff8270133f063aab6c9977183d9e72835428"
        wallets_plugin._store.add_account({
            "address": addr, "path": "44'/60'/0'/0/0",
            "source": "ledger", "scheme": "BIP-44", "label": "",
        })
        wallets_plugin.rebuild_tree()
        ghost = "0x" + "ff" * 20
        assert wallets_plugin.select_address(ghost) is False
        # Original selection (default account) preserved.
        assert wallets_plugin.selected_address == addr

    def test_select_address_with_empty_string(self, wallets_plugin):
        """Defensive: empty / None addr can come from
        ``req.from_addr`` if the request was malformed; should be a
        clean False, not an exception."""
        assert wallets_plugin.select_address("") is False

    def test_double_click_sets_default(self, qtbot, wallets_plugin):
        """Double-click on an address leaf connects it to the
        browser (sets it as the store's default_account). Same
        action as the button + right-click menu item; just the
        no-friction path."""
        addr1 = "0x7a16ff8270133f063aab6c9977183d9e72835428"
        addr2 = "0x" + "11" * 20
        wallets_plugin._store.add_account({
            "address": addr1, "path": "44'/60'/0'/0/0",
            "source": "ledger", "scheme": "BIP-44", "label": "",
        })
        wallets_plugin._store.add_account({
            "address": addr2, "path": "44'/60'/0'/0/1",
            "source": "ledger", "scheme": "BIP-44", "label": "",
        })
        wallets_plugin.rebuild_tree()
        # default starts as addr1 (first-added). Double-click addr2.
        assert wallets_plugin._store.default_account == addr1
        matches = wallets_plugin._tree.findItems(
            addr2, Qt.MatchContains | Qt.MatchRecursive, 0
        )
        wallets_plugin._on_tree_double_clicked(matches[0], 0)
        assert wallets_plugin._store.default_account == addr2

    def test_double_click_on_group_row_is_noop(self, qtbot, wallets_plugin):
        """Group rows ("Ledger", scheme subgroups) don't carry an
        address — double-click does nothing."""
        addr = "0x7a16ff8270133f063aab6c9977183d9e72835428"
        wallets_plugin._store.add_account({
            "address": addr, "path": "44'/60'/0'/0/0",
            "source": "ledger", "scheme": "BIP-44", "label": "",
        })
        wallets_plugin.rebuild_tree()
        ledger_root = wallets_plugin._tree.topLevelItem(0)
        # Should not raise. Default stays.
        wallets_plugin._on_tree_double_clicked(ledger_root, 0)
        assert wallets_plugin._store.default_account == addr

    def test_add_watch_only_dialog_accepts_valid_address(self, qtbot, tmp_qeth):
        """Lower-case input is checksum-normalised on accept; the
        result_account() dict has the right shape for
        Store.add_account."""
        from qeth.plugins.wallets import AddWatchOnlyDialog
        dlg = AddWatchOnlyDialog(set())
        qtbot.addWidget(dlg)
        addr_lower = "0x7a16ff8270133f063aab6c9977183d9e72835428"
        dlg.address_edit.setText(addr_lower)
        dlg.label_edit.setText("Treasury")
        # Add button enables once the address parses.
        assert dlg.add_btn.isEnabled()
        dlg._on_accept()
        acct = dlg.result_account()
        # EIP-55 mixed case applied.
        assert acct["address"] == "0x7a16fF8270133F063aAb6C9977183D9e72835428"
        assert acct["source"] == "watch_only"
        assert acct["label"] == "Treasury"

    def test_add_watch_only_dialog_rejects_duplicates(self, qtbot, tmp_qeth):
        """Address already in the wallet → error shown; dialog does
        not accept."""
        from qeth.plugins.wallets import AddWatchOnlyDialog
        existing = {"0x7a16fF8270133F063aAb6C9977183D9e72835428"}
        dlg = AddWatchOnlyDialog(existing)
        qtbot.addWidget(dlg)
        dlg.address_edit.setText(
            "0x7a16ff8270133f063aab6c9977183d9e72835428"
        )
        dlg._on_accept()
        # ``isVisible`` returns False under the offscreen platform
        # plugin (no real display), so check the inverse of the
        # underlying setVisible call.
        assert not dlg.error_lbl.isHidden()
        assert "already" in dlg.error_lbl.text().lower()

    def test_add_watch_only_dialog_rejects_garbage_input(self, qtbot, tmp_qeth):
        """Add button stays disabled for malformed input."""
        from qeth.plugins.wallets import AddWatchOnlyDialog
        dlg = AddWatchOnlyDialog(set())
        qtbot.addWidget(dlg)
        dlg.address_edit.setText("not an address")
        assert not dlg.add_btn.isEnabled()

    def test_add_watch_only_dialog_fills_label_from_ens(self, qtbot, tmp_qeth):
        """When ENS reverse-resolution returns a verified name, the
        Label field is auto-populated — unless the user has typed
        their own label first, in which case we never overwrite."""
        from qeth.plugins.wallets import AddWatchOnlyDialog
        dlg = AddWatchOnlyDialog(set())
        qtbot.addWidget(dlg)
        addr = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        dlg.address_edit.setText(addr)
        # Simulate the worker resolving by calling the slot directly.
        dlg._on_ens_resolved(addr, "vitalik.eth")
        assert dlg.label_edit.text() == "vitalik.eth"

    def test_add_watch_only_dialog_does_not_overwrite_user_label(
        self, qtbot, tmp_qeth,
    ):
        from qeth.plugins.wallets import AddWatchOnlyDialog
        dlg = AddWatchOnlyDialog(set())
        qtbot.addWidget(dlg)
        addr = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        dlg.address_edit.setText(addr)
        dlg.label_edit.setText("My label")
        dlg._on_ens_resolved(addr, "vitalik.eth")
        assert dlg.label_edit.text() == "My label"

    def test_add_watch_only_dialog_ignores_stale_ens_result(
        self, qtbot, tmp_qeth,
    ):
        """If the user has typed a different address by the time
        the lookup returns, the result for the old address must be
        ignored — otherwise stale results would clobber the label
        the user is currently expecting."""
        from qeth.plugins.wallets import AddWatchOnlyDialog
        dlg = AddWatchOnlyDialog(set())
        qtbot.addWidget(dlg)
        dlg.address_edit.setText("0x" + "11" * 20)
        # Resolver returns a name for a now-stale address.
        dlg._on_ens_resolved(
            "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "vitalik.eth",
        )
        assert dlg.label_edit.text() == ""

    def test_watch_only_appears_in_tree(self, qtbot, wallets_plugin):
        """After _add_watch_only persists an account, _rebuild_tree
        surfaces it under the "Watch only (N)" group."""
        wallets_plugin._store.add_account({
            "address": "0x" + "11" * 20,
            "source": "watch_only",
            "label": "Cold storage",
        })
        wallets_plugin.rebuild_tree()
        # Walk top-level items to find the "Watch only (1)" group.
        groups = [
            wallets_plugin._tree.topLevelItem(i).text(0)
            for i in range(wallets_plugin._tree.topLevelItemCount())
        ]
        assert any(g.startswith("Watch only (1)") for g in groups), groups

    def test_watch_only_disables_connect_to_browser(self, qtbot, wallets_plugin):
        """The Connect-to-browser button must be off for watch-only
        accounts — they have no signing key, so dapps that try to
        sign from them would fail mid-flow. Better to gate up front."""
        addr = "0x" + "22" * 20
        wallets_plugin._store.add_account({
            "address": addr, "source": "watch_only", "label": "",
        })
        wallets_plugin.rebuild_tree()
        # Simulate selecting the watch-only leaf.
        wallets_plugin.select_address(addr)
        assert not wallets_plugin._details.set_default_btn.isEnabled()
        assert "Watch-only" in wallets_plugin._details.set_default_btn.text()

    def test_add_hot_wallet_dialog_gating(self, qtbot, tmp_qeth):
        """Walks the Add button's enable/disable through every
        gate: empty field, bad key, bad passphrase, mismatched
        passphrase, short passphrase, all-valid."""
        from qeth.plugins.wallets import AddHotWalletDialog
        dlg = AddHotWalletDialog()
        qtbot.addWidget(dlg)

        # Everything empty.
        assert not dlg.gen_btn.isEnabled()

        # Bad private key — disabled with a hint in match_lbl.
        dlg.pk_edit.setText("not a key")
        assert not dlg.gen_btn.isEnabled()
        assert "64 hex" in dlg.match_lbl.text()

        # Valid private key but no passphrase yet.
        dlg.pk_edit.setText("a" * 64)
        assert not dlg.gen_btn.isEnabled()

        # Short passphrase: disabled. Length feedback goes in the
        # inline ``pass_status_lbl`` next to the form, not the
        # bottom match_lbl, so the user sees it as they type.
        dlg.pass1_edit.setText("short")
        dlg.pass2_edit.setText("short")
        assert not dlg.gen_btn.isEnabled()
        assert "5/8" in dlg.pass_status_lbl.text()

        # Mismatched: different hint, still inline.
        dlg.pass1_edit.setText("longenoughpass")
        dlg.pass2_edit.setText("longenoughpasS")
        assert not dlg.gen_btn.isEnabled()
        assert "don't match" in dlg.pass_status_lbl.text()

        # All valid → enabled, inline says "ok".
        dlg.pass1_edit.setText("longenoughpass")
        dlg.pass2_edit.setText("longenoughpass")
        assert dlg.gen_btn.isEnabled()
        assert "ok" in dlg.pass_status_lbl.text().lower()
        assert dlg.match_lbl.text() == ""

    def test_add_hot_wallet_dialog_dice_fills_field(self, qtbot, tmp_qeth):
        """Clicking the dice button populates the private-key
        field with a 64-char hex value."""
        from qeth.plugins.wallets import AddHotWalletDialog
        dlg = AddHotWalletDialog()
        qtbot.addWidget(dlg)
        assert dlg.pk_edit.text() == ""
        dlg.dice_btn.click()
        text = dlg.pk_edit.text()
        assert len(text) == 64
        # All hex characters.
        int(text, 16)

    def test_add_ledger_dialog_constructs(self, qtbot, wallets_plugin):
        """Smoke test for the Add account dialog. The dialog is built
        lazily on button click, so an undeclared import (the
        QComboBox import that went missing in the wallets-plugin
        refactor) only blew up when the user actually tried to add
        an account. Constructing it directly here catches that
        whole class of bug at test time."""
        from qeth.chains import DEFAULT_CHAINS
        from qeth.plugins.wallets import AddLedgerDialog
        dlg = AddLedgerDialog(DEFAULT_CHAINS[0])
        qtbot.addWidget(dlg)

    def test_splitter_state_round_trip(self, wallets_plugin):
        hex_state = wallets_plugin.splitter_state()
        assert hex_state  # non-empty
        # Restore is best-effort and doesn't raise on garbage.
        wallets_plugin.restore_splitter_state("not-valid-hex")
        wallets_plugin.restore_splitter_state(hex_state)
