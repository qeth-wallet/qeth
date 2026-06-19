"""Tests for TokensPlugin + TransactionsPlugin in isolation.

These instantiate each plugin against a stub host so the lifecycle
hooks can be driven without a full MainWindow. They lock in the
plugin contract: who owns what, who fires when, and what the
plugins do in response to lifecycle calls.
"""


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
    def __init__(self, chain=ETH, address: str | None = None):
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


# --- TokensPlugin carry-forward -------------------------------------------

ARB = next(c for c in DEFAULT_CHAINS if c.chain_id == 42161)
USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
OTHER = "0x" + "cd" * 20


class TestCarryForwardAbsent:
    """An inconclusive balance read must not drop an already-shown token.
    A token ABSENT from a multicall had its read fail (rate-limited
    upstream / lagging node) — not a real zero, which comes back
    explicitly — so its last-known balance is carried forward. A genuine
    zero (present in the result) still hides. The explorer-fallback path
    also prefers last-known over the explorer's stale value."""

    def _plugin_with_prev(self, tmp_qeth, prev_balance=1000):
        from qeth.wallet_cache import CachedToken, CachedWallet, WalletCache
        p = TokensPlugin.__new__(TokensPlugin)   # skip heavy __init__
        p._wallet_cache = WalletCache()
        p._wallet_cache.save(CachedWallet(
            chain_id=ARB.chain_id, address=ADDR,
            native_balance_wei=0, native_price_usd=None,
            native_balance_updated=0, native_price_updated=0,
            tokens=[CachedToken(
                contract=USDC, symbol="USDC", name="USD Coin", decimals=6,
                logo_uri=None, balance_raw=prev_balance, price_usd=None,
                balance_updated=0, price_updated=0,
            )],
        ))
        return p

    def test_absent_token_is_carried_forward(self, tmp_qeth):
        p = self._plugin_with_prev(tmp_qeth)
        raw: dict = {}                       # multicall omitted USDC (read failed)
        p._carry_forward_absent(ARB, ADDR, raw, [USDC])
        assert raw[USDC] == 1000             # kept its last-known balance

    def test_explicit_zero_is_not_overridden(self, tmp_qeth):
        p = self._plugin_with_prev(tmp_qeth)
        raw = {USDC: 0}                       # conclusive zero — genuinely emptied
        p._carry_forward_absent(ARB, ADDR, raw, [USDC])
        assert raw[USDC] == 0                 # left alone → row hides

    def test_explorer_fallback_prefers_last_known(self, tmp_qeth):
        p = self._plugin_with_prev(tmp_qeth)
        raw = {USDC: 5}                       # block explorer's stale value
        p._carry_forward_absent(ARB, ADDR, raw, [USDC], override_existing=True)
        assert raw[USDC] == 1000              # last-known wins over stale explorer

    def test_unknown_token_stays_absent(self, tmp_qeth):
        p = self._plugin_with_prev(tmp_qeth)
        raw = {}                              # OTHER was never shown before
        p._carry_forward_absent(ARB, ADDR, raw, [OTHER])
        assert raw == {}                      # nothing to carry forward

    def test_no_prior_cache_is_noop(self, tmp_qeth):
        from qeth.wallet_cache import WalletCache
        p = TokensPlugin.__new__(TokensPlugin)
        p._wallet_cache = WalletCache()       # nothing saved
        raw: dict = {}
        p._carry_forward_absent(ARB, ADDR, raw, [USDC])
        assert raw == {}


# --- TransactionsPlugin ----------------------------------------------------

def _tx_send(*, nonce: int):
    """A minimal sent Transaction (from ADDR) at a given nonce."""
    return Transaction(
        chain_id=1, hash="0x" + f"{nonce:064x}", block_number=1, timestamp=1,
        nonce=nonce, from_addr=ADDR, to_addr="0xbeef",
        value_wei=0, gas_used=0, gas_price_wei=0,
        method_id="", input_data="0x", success=True,
    )


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

            def list_transactions(self, _c, _a, page=1, limit=50, before_block=None):
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

            def list_transactions(self, _c, _a, page=1, limit=50, before_block=None):
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

    def test_initial_open_renders_bounded_window(self, qtbot, tmp_qeth):
        """First activation materialises only the first
        ``INITIAL_VISIBLE`` cached rows onto the table — full
        repaints on caches with thousands of entries used to
        freeze the main thread for ~900 ms on busy wallets. The
        rest stays in the in-memory cache for scroll-to-bottom
        to reveal."""
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

        # Cache hydrated fully; only INITIAL_VISIBLE rendered.
        key = (ETH.chain_id, ADDR.lower())
        assert len(plugin._cache[key]) == 1000
        assert plugin.widget().table.rowCount() == plugin.INITIAL_VISIBLE
        assert plugin._displayed_count[key] == plugin.INITIAL_VISIBLE

    def test_scroll_bottom_reveals_more_cache_before_network(
        self, qtbot, tmp_qeth,
    ):
        """When the displayed window is narrower than the cached
        list, scroll-to-bottom must reveal more cached rows
        in-memory before issuing any network call. Only once the
        cache is fully revealed does the load-older-from-network
        path engage."""
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

        key = (ETH.chain_id, ADDR.lower())
        # Track network calls — the cache-reveal path must NOT
        # cause any. We monkey-patch _fetch_page on the instance.
        calls: list = []

        def _fake_fetch(k, addr, page, walk_on_overlap=False, before_block=None):
            calls.append((k, addr, page))
        plugin._fetch_page = _fake_fetch

        # Reveal more from cache: 200 → 400, no network call.
        plugin._on_scroll_bottom()
        assert plugin._displayed_count[key] == 400
        assert plugin.widget().table.rowCount() == 400
        assert calls == []

        # Keep revealing until cache is fully displayed.
        while plugin._displayed_count[key] < 1000:
            plugin._on_scroll_bottom()
        assert plugin.widget().table.rowCount() == 1000
        assert calls == []

        # Now that cache is exhausted, next scroll goes to network.
        plugin._on_scroll_bottom()
        assert len(calls) == 1

    def test_external_send_detected_by_nonce_refetches(self, qtbot, tmp_qeth):
        """An on-chain nonce beyond our highest cached nonce means a tx was
        sent from another client — re-fetch page 1 (clearing the
        'exhausted' short-circuit) to pull it in."""
        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        key = (ETH.chain_id, ADDR.lower())
        # We hold a "complete" history up to nonce 4 and think we're done.
        plugin._cache[key] = [_tx_send(nonce=n) for n in range(5)]
        plugin._exhausted.add(key)
        calls: list = []
        plugin._fetch_page = lambda k, a, page, **kw: calls.append((k, a, page))

        # Chain says 6 txs sent (nonces 0..5) → nonce 5 is new.
        plugin._on_external_nonce(key, 6)
        assert key not in plugin._exhausted        # short-circuit lifted
        assert calls == [(key, ADDR, 1)]           # page-1 re-fetch kicked

    def test_no_refetch_when_nonce_matches_history(self, qtbot, tmp_qeth):
        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        key = (ETH.chain_id, ADDR.lower())
        plugin._cache[key] = [_tx_send(nonce=n) for n in range(5)]  # 0..4
        plugin._exhausted.add(key)
        calls: list = []
        plugin._fetch_page = lambda k, a, page, **kw: calls.append(1)
        plugin._on_external_nonce(key, 5)          # count 5 → last sent = 4 = our max
        assert calls == []                         # nothing new
        assert key in plugin._exhausted            # untouched

    def test_activation_kicks_immediate_nonce_check(self, qtbot, tmp_qeth):
        """Opening the tab must nonce-check *now*, not wait for the 30s
        timer — otherwise a complete-looking cache that's missing a tx
        sent elsewhere stays stale until the first tick."""
        from qeth.plugins.transactions import NonceCheckWorker
        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        # A "complete" history (0..2) so the _is_full_history short-circuit
        # would otherwise skip the fetch entirely.
        plugin._cache[(ETH.chain_id, ADDR.lower())] = [
            _tx_send(nonce=n) for n in range(3)
        ]
        host.started_workers.clear()
        plugin.on_activated()
        assert any(isinstance(w, NonceCheckWorker)
                   for w in host.started_workers)

    def test_nonce_poll_skips_when_no_account_or_error(self, qtbot, tmp_qeth):
        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        key = (ETH.chain_id, ADDR.lower())
        plugin._cache[key] = [_tx_send(nonce=0)]
        calls: list = []
        plugin._fetch_page = lambda *a, **kw: calls.append(1)
        plugin._nonce_in_flight.add(key)
        plugin._on_external_nonce(key, None)       # error result → no-op
        assert calls == [] and key not in plugin._nonce_in_flight

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

    def test_render_decoded_highlights_own_addresses(self, qtbot, tmp_qeth):
        """An address argument that's one of the user's own wallets
        renders bold + italic; a stranger address does not."""
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QTextEdit
        from qeth.plugins.transactions import _render_decoded

        mine = "0x7a16ff8270133f063aab6c9977183d9e72835428"
        other = "0x000000000000000000000000000000000000dead"
        edit = QTextEdit()
        qtbot.addWidget(edit)
        _render_decoded(edit, {
            "function": "transferFrom",
            "args": [
                {"name": "_from", "type": "address", "value": mine},
                {"name": "_to", "type": "address", "value": other},
            ],
        }, None, known_addresses={mine})

        # Collect every text run with bold+italic — the args land in
        # blocks after the first, so walk the whole document.
        bold_italic_runs: list[str] = []
        block = edit.document().firstBlock()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid():
                    cf = frag.charFormat()
                    if cf.fontWeight() >= QFont.Bold and cf.fontItalic():
                        bold_italic_runs.append(frag.text())
                it += 1
            block = block.next()
        # Our address is bold+italic; the stranger isn't.
        assert any(mine in r for r in bold_italic_runs)
        assert not any("dead" in r.lower() for r in bold_italic_runs)

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
            TransactionDetailsDialog,
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

    def test_details_dialog_closes_on_escape(self, qtbot, tmp_qeth):
        """Esc must dismiss the (modeless) details dialog — its Close
        button has no mnemonic, so Escape is the keyboard exit."""
        from unittest.mock import MagicMock
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from qeth.plugins.transactions import TransactionDetailsDialog

        tx = Transaction(
            chain_id=1, hash="0x" + "aa" * 32, block_number=1,
            timestamp=1779618611, nonce=1, from_addr=ADDR,
            to_addr="0x" + "22" * 20, value_wei=0, gas_used=21000,
            gas_price_wei=10**9, method_id="", input_data="0x", success=True,
        )
        dlg = TransactionDetailsDialog(
            tx, ETH, abi_source=MagicMock(), abi_cache=MagicMock(),
            start_worker=lambda w: None, token_info=lambda *a: None,
            icon_cache=MagicMock(), native_price_usd=None,
        )
        qtbot.addWidget(dlg)
        dlg.show()
        assert dlg.isVisible()
        QTest.keyClick(dlg, Qt.Key_Escape)
        assert not dlg.isVisible()

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

        # First activation paints up to INITIAL_VISIBLE rows.
        plugin.on_activated()
        first_count = plugin.widget().table.rowCount()
        assert first_count == min(plugin.INITIAL_VISIBLE, 300)

        # Tab switch away then back — second on_activated for the
        # same view must NOT touch the table.
        plugin.on_activated()
        assert plugin.widget().table.rowCount() == first_count

    def test_scroll_loads_older_via_block_cursor(self, qtbot, tmp_qeth):
        """Older history loads on scroll via the endblock cursor (page
        stays 1, no page × offset cap). The cursor walks down from the
        oldest loaded block until it reaches the start of history."""
        from qeth.transactions_cache import TransactionCache

        def _mk(n: int) -> Transaction:
            return Transaction(
                chain_id=1, hash="0x" + format(n, "064x"),
                block_number=n, timestamp=n, nonce=n,
                from_addr=ADDR, to_addr="0xfeed",
                value_wei=0, gas_used=0, gas_price_wei=0,
                method_id="", input_data="0x", success=True,
            )

        # Cache holds the newest 100 (blocks/nonces 199..100); the older
        # half (99..0) is only on the wire, reachable via the cursor.
        TransactionCache().save(
            ETH.chain_id, ADDR, [_mk(n) for n in range(199, 99, -1)])

        class _Source:
            def __init__(self):
                self.cursors: list = []

            def supports(self, _c):
                return True

            def list_transactions(self, _c, _a, page=1, limit=50,
                                  before_block=None):
                self.cursors.append(before_block)
                top = 199 if before_block is None else before_block
                return [_mk(n) for n in range(top, max(-1, top - limit), -1)
                        if n >= 0]

        source = _Source()
        plugin = TransactionsPlugin(source=source)
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        original_start = host.start_worker

        def _start(worker):
            original_start(worker)
            worker.run()

        host.start_worker = _start

        plugin.on_activated()                  # refresh: newest (cursor None)
        key = (ETH.chain_id, ADDR.lower())
        # Reveal cached rows, then scroll → block-cursor fetches older,
        # walking endblock down until the start of history (nonce 0).
        for _ in range(60):
            if min(t.nonce for t in plugin._cache[key]) == 0:
                break
            plugin._on_scroll_bottom()
        assert min(t.nonce for t in plugin._cache[key]) == 0
        assert None in source.cursors                      # the refresh-newest
        assert any(c is not None for c in source.cursors)  # the cursor fetches

    def test_scroll_bottom_walks_block_cursor(self, qtbot, tmp_qeth):
        """The scrolled_to_bottom signal drives load-on-scroll: once the
        cache is shown, each emission kicks one worker that pages OLDER
        via the block cursor (page stays 1; endblock = oldest loaded
        block) — no page × offset ≤ 10000 cap."""
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
        assert host.started_workers[0].page == 1
        # endblock = oldest loaded block (the seed's lowest block is 1).
        assert host.started_workers[0].before_block == 1

        # Once we record an exhausted history, further scrolls no-op.
        plugin._exhausted.add((ETH.chain_id, ADDR.lower()))
        host.started_workers.clear()
        plugin._on_scroll_bottom()
        assert host.started_workers == []

    def test_refresh_backfills_a_partial_cache_stub(self, qtbot, tmp_qeth):
        """An interrupted earlier load can leave a < INITIAL_BATCH stub in
        cache. A refresh whose page 1 only re-confirms the stub (so it did NOT
        progress) must still walk older to backfill the view — not freeze at
        the stub (the _yb.eth "stuck at 7 of 657" bug)."""
        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        # A 7-row stub already persisted (blocks/nonces 100..106).
        stub = [Transaction(
            chain_id=1, hash="0x" + format(b, "064x"), block_number=b,
            timestamp=b, nonce=b, from_addr=ADDR, to_addr="0xbeef",
            value_wei=0, gas_used=0, gas_price_wei=0,
            method_id="", input_data="0x", success=True,
        ) for b in range(106, 99, -1)]
        plugin._cache[(ETH.chain_id, ADDR.lower())] = list(stub)
        host.started_workers.clear()
        # Refresh: page 1 returns the same stub (overlap -> did not progress).
        plugin._on_page_fetched(ETH.chain_id, ADDR.lower(), 1, list(stub), True)
        # Walks older from the oldest stub block to backfill, despite no progress.
        assert len(host.started_workers) == 1
        assert host.started_workers[0].before_block == 100

    def test_overlap_only_page_skips_the_disk_save(self, qtbot, tmp_qeth):
        """A page-1 refresh that merely re-confirms the cache must NOT
        re-serialize it — json.dumps of a busy wallet's multi-MB cache cost
        ~150 ms on the main thread on every tab open. A page that CHANGES an
        entry (pending→confirmed) must still save."""
        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        key = (ETH.chain_id, ADDR.lower())
        pend = Transaction(
            chain_id=1, hash="0x" + "cd" * 32, block_number=200,
            timestamp=200, nonce=7, from_addr=ADDR, to_addr="0xbeef",
            value_wei=0, gas_used=0, gas_price_wei=0,
            method_id="", input_data="0x", success=True, pending=True,
        )
        plugin._cache[key] = [pend]
        saves: list = []
        plugin._disk_cache.save = (          # type: ignore[method-assign]
            lambda *a, **k: saves.append(a))

        # Pure overlap: the page returns exactly what's cached -> no save.
        plugin._on_page_fetched(ETH.chain_id, ADDR.lower(), 1, [pend], True)
        assert saves == []

        # Same hash, changed fields (confirmed now) -> must save.
        from dataclasses import replace
        confirmed = replace(pend, pending=False, block_number=201)
        plugin._on_page_fetched(
            ETH.chain_id, ADDR.lower(), 1, [confirmed], True)
        assert len(saves) == 1

    def test_walk_advances_by_raw_block_past_received_only_window(self, qtbot, tmp_qeth):
        """Receive-heavy account: an older-walk page brings NO new sent tx
        (the window was all received), but its RAW oldest block reached older
        ground — so the walk keeps going, advancing by that raw block, instead
        of declaring exhaustion (the _yb.eth "stuck at 3 of 657" bug)."""
        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        key = (ETH.chain_id, ADDR.lower())
        stub = [Transaction(
            chain_id=1, hash="0x" + format(b, "064x"), block_number=b,
            timestamp=b, nonce=b, from_addr=ADDR, to_addr="0xbeef",
            value_wei=0, gas_used=0, gas_price_wei=0,
            method_id="", input_data="0x", success=True,
        ) for b in (300, 299, 298)]
        plugin._cache[key] = list(stub)
        host.started_workers.clear()
        # Walk page re-confirms only the boundary sent tx (block 298, overlap),
        # but the raw window reached down to block 250 (received txs stripped).
        plugin._on_page_fetched(ETH.chain_id, ADDR.lower(), 1, [stub[-1]], True,
                                walk_on_overlap=True, raw_oldest=250,
                                requested_before=298)
        assert key not in plugin._exhausted
        assert len(host.started_workers) == 1
        assert host.started_workers[0].before_block == 250   # advanced by raw

    def test_walk_stops_when_raw_cursor_truly_stalls(self, qtbot, tmp_qeth):
        """If the raw cursor can't advance (a single block heavier than a page)
        AND nothing new surfaced, the walk has genuinely bottomed out."""
        plugin = TransactionsPlugin()
        host = _StubHost(address=ADDR)
        plugin.attach(host)
        qtbot.addWidget(plugin.widget())
        key = (ETH.chain_id, ADDR.lower())
        stub = [Transaction(
            chain_id=1, hash="0x" + format(b, "064x"), block_number=b,
            timestamp=b, nonce=b, from_addr=ADDR, to_addr="0xbeef",
            value_wei=0, gas_used=0, gas_price_wei=0,
            method_id="", input_data="0x", success=True,
        ) for b in (300, 299, 298)]
        plugin._cache[key] = list(stub)
        host.started_workers.clear()
        plugin._on_page_fetched(ETH.chain_id, ADDR.lower(), 1, [stub[-1]], True,
                                walk_on_overlap=True, raw_oldest=298,
                                requested_before=298)   # cursor did not advance
        assert key in plugin._exhausted
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

            def list_transactions(self, _c, _a, page=1, limit=50, before_block=None):
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
        assert panel.table.item(0, 0).toolTip() == "Pending"

        plugin._on_receipt_confirmed(ETH, plugin._cache[key][0].hash,
                                      self._confirmed_receipt())
        assert panel.table.item(0, 0).toolTip() == "Success"


class TestOnTxDropped:
    """A pending tx whose nonce got consumed by a *different* tx must
    flip to the terminal 'dropped' state — not stay pending forever and
    not look like a revert."""

    def _pending(self, **overrides) -> Transaction:
        base = dict(
            chain_id=1, hash="0x" + "ab" * 32, block_number=0,
            timestamp=1700, nonce=5, from_addr=ADDR, to_addr="0xbeef",
            value_wei=0, gas_used=0, gas_price_wei=2 * 10**9,
            method_id="", input_data="0x", success=True, pending=True,
            raw_signed="0xdeadbeef",
        )
        base.update(overrides)
        return Transaction(**base)

    def test_marks_dropped_and_clears_raw(self, qtbot, tmp_qeth):
        plugin = TransactionsPlugin()
        qtbot.addWidget(plugin.widget())
        key = (ETH.chain_id, ADDR.lower())
        plugin._cache[key] = [self._pending()]
        plugin._disk_cache.save(*key, plugin._cache[key])

        h = plugin._cache[key][0].hash
        for _ in range(plugin.DROP_CONFIRM_READINGS):
            plugin._on_tx_dropped(ETH, h)

        tx = plugin._cache[key][0]
        assert tx.pending is False
        assert tx.dropped is True
        assert tx.success is True          # not a revert
        assert tx.raw_signed is None       # no point re-broadcasting a dead nonce
        # Disk round-trip preserves the dropped state.
        assert plugin._disk_cache.load(*key)[0].dropped is True

    def test_contradicting_still_pending_resets_the_count(self, qtbot, tmp_qeth):
        """dropped ×2, STILL PENDING, dropped ×2 — must NOT flip: the
        DROP_CONFIRM_READINGS guard counts CONSECUTIVE readings, and a
        contradicting still-open reading resets the tally. Without the reset
        the count is cumulative over the session and a flappy load-balanced
        RPC still falsely drops a pending tx, just more slowly."""
        plugin = TransactionsPlugin()
        qtbot.addWidget(plugin.widget())
        key = (ETH.chain_id, ADDR.lower())
        plugin._cache[key] = [self._pending()]
        plugin._disk_cache.save(*key, plugin._cache[key])
        h = plugin._cache[key][0].hash

        for _ in range(plugin.DROP_CONFIRM_READINGS - 1):
            plugin._on_tx_dropped(ETH, h)
        plugin._on_tx_still_pending(ETH, h)     # contradiction → tally resets
        for _ in range(plugin.DROP_CONFIRM_READINGS - 1):
            plugin._on_tx_dropped(ETH, h)
        assert plugin._cache[key][0].pending is True    # not believed yet
        plugin._on_tx_dropped(ETH, h)           # 3rd CONSECUTIVE reading
        assert plugin._cache[key][0].dropped is True

    def test_watcher_probes_pending_txs_of_non_selected_accounts(
            self, qtbot, tmp_qeth):
        """The pending sweep walks the whole (chain, account) cache — a tx
        broadcast from account A must keep getting probed/re-broadcast after
        the user switches the UI to account B. Nothing in the tick may
        consult the selected address."""
        from types import SimpleNamespace
        from qeth.plugins.transactions import (
            PendingProbeWorker, PendingTxWatcher,
        )
        plugin = TransactionsPlugin()
        qtbot.addWidget(plugin.widget())
        spawned: list = []
        plugin.host = SimpleNamespace(
            chain_by_id=lambda cid: ETH if cid == ETH.chain_id else None,
            start_worker=spawned.append,
            selected_address="0x" + "99" * 20,   # a DIFFERENT account on screen
        )
        key = (ETH.chain_id, ADDR.lower())
        plugin._cache[key] = [self._pending()]

        watcher = PendingTxWatcher(plugin)
        watcher._tick()

        assert len(spawned) == 1
        worker = spawned[0]
        assert isinstance(worker, PendingProbeWorker)
        assert worker._tx_hash == plugin._cache[key][0].hash
        assert worker._raw == "0xdeadbeef"
        assert worker._rebroadcast is True

    def test_single_reading_keeps_it_pending(self, qtbot, tmp_qeth):
        # One "looks dropped" reading is unreliable behind a load-balanced RPC,
        # so the tx must stay pending for the next tick to re-check — not flip
        # to the terminal dropped state on the strength of a single null receipt.
        plugin = TransactionsPlugin()
        qtbot.addWidget(plugin.widget())
        key = (ETH.chain_id, ADDR.lower())
        plugin._cache[key] = [self._pending()]
        plugin._on_tx_dropped(ETH, plugin._cache[key][0].hash)
        assert plugin._cache[key][0].pending is True
        assert plugin._cache[key][0].dropped is False

    def test_dropped_glyph_distinct_from_revert(self, qtbot, tmp_qeth):
        plugin = TransactionsPlugin()
        panel = plugin.widget()
        qtbot.addWidget(panel)
        key = (ETH.chain_id, ADDR.lower())
        plugin._cache[key] = [self._pending()]
        panel.set_context(ETH, ADDR)
        panel.show_transactions(plugin._cache[key])
        plugin._rendered_for = key
        assert panel.table.item(0, 0).toolTip() == "Pending"

        h = plugin._cache[key][0].hash
        for _ in range(plugin.DROP_CONFIRM_READINGS):
            plugin._on_tx_dropped(ETH, h)
        assert panel.table.item(0, 0).toolTip().startswith("Dropped")   # not ✗


class TestPendingProbeWorker:
    """Receipt → nonce → re-broadcast diagnosis. We drive run()
    synchronously with a faked EthClient and capture the emitted
    signal."""

    HASH = "0x" + "ab" * 32

    def _worker(self, monkeypatch, *, receipt, latest_nonce, nonce=5,
                raw="0xraw", rebroadcast=True):
        import qeth.plugins.transactions as txmod

        sent: list = []

        class _FakeClient:
            def __init__(self, chain):
                pass

            def rpc(self, method, params):
                assert method == "eth_getTransactionReceipt"
                return receipt

            def get_transaction_count(self, address, block="pending"):
                assert block == "latest"
                # web3.py rejects non-checksum addresses — mimic that so
                # a regression (passing the lowercased from_addr straight
                # through) fails here instead of silently never dropping.
                from eth_utils import to_checksum_address
                if address != to_checksum_address(address):
                    raise ValueError("web3.py only accepts checksum addresses")
                return latest_nonce

            def send_raw_transaction(self, raw_tx):
                sent.append(raw_tx)
                return "0xresent"

        monkeypatch.setattr(txmod, "EthClient", _FakeClient)
        worker = txmod.PendingProbeWorker(
            ETH, self.HASH, ADDR, nonce, raw, rebroadcast,
        )
        return worker, sent

    def _capture(self, worker):
        out = {}
        worker.confirmed.connect(lambda c, h, r: out.update(kind="confirmed", r=r))
        worker.dropped.connect(lambda c, h: out.update(kind="dropped"))
        worker.still_pending.connect(lambda c, h: out.update(kind="pending"))
        worker.failed.connect(lambda c, h, m: out.update(kind="failed", msg=m))
        return out

    def test_receipt_present_confirms(self, qtbot, monkeypatch):
        worker, _ = self._worker(
            monkeypatch, receipt={"status": "0x1"}, latest_nonce=99,
        )
        out = self._capture(worker)
        worker.run()
        assert out["kind"] == "confirmed"

    def test_nonce_consumed_drops(self, qtbot, monkeypatch):
        # No receipt, but the account's mined nonce is already past ours.
        worker, sent = self._worker(
            monkeypatch, receipt=None, latest_nonce=6, nonce=5,
        )
        out = self._capture(worker)
        worker.run()
        assert out["kind"] == "dropped"
        assert sent == []          # never re-broadcast a dead nonce

    def test_open_nonce_rebroadcasts_and_stays_pending(self, qtbot, monkeypatch):
        worker, sent = self._worker(
            monkeypatch, receipt=None, latest_nonce=5, nonce=5,
            raw="0xRAW", rebroadcast=True,
        )
        out = self._capture(worker)
        worker.run()
        assert out["kind"] == "pending"
        assert sent == ["0xRAW"]   # re-pushed the stored raw bytes

    def test_rebroadcast_suppressed_when_not_requested(self, qtbot, monkeypatch):
        worker, sent = self._worker(
            monkeypatch, receipt=None, latest_nonce=5, nonce=5,
            rebroadcast=False,
        )
        out = self._capture(worker)
        worker.run()
        assert out["kind"] == "pending"
        assert sent == []          # capped / no raw → no re-send

    def test_rebroadcast_error_is_swallowed(self, qtbot, monkeypatch):
        import qeth.plugins.transactions as txmod

        class _FakeClient:
            def __init__(self, chain): pass
            def rpc(self, m, p): return None
            def get_transaction_count(self, a, block="pending"): return 5
            def send_raw_transaction(self, raw):
                raise Exception("already known")

        monkeypatch.setattr(txmod, "EthClient", _FakeClient)
        worker = txmod.PendingProbeWorker(ETH, self.HASH, ADDR, 5, "0xr", True)
        out = self._capture(worker)
        worker.run()
        assert out["kind"] == "pending"   # "already known" must not fail it

    def test_rebroadcasts_even_when_receipt_check_times_out(self, qtbot, monkeypatch):
        """The probe RPC itself failing (e.g. DRPC 408) is exactly when a
        dropped tx needs re-pushing — the re-broadcast must still fire,
        not be suppressed by the failed receipt lookup."""
        import qeth.plugins.transactions as txmod
        sent: list = []

        class _FlakyClient:
            def __init__(self, chain): pass
            def rpc(self, m, p):
                raise Exception("408 Client Error: Request Timeout")
            def send_raw_transaction(self, raw):
                sent.append(raw); return "0xresent"

        monkeypatch.setattr(txmod, "EthClient", _FlakyClient)
        worker = txmod.PendingProbeWorker(ETH, self.HASH, ADDR, 5, "0xRAW", True)
        out = self._capture(worker)
        worker.run()
        assert sent == ["0xRAW"]          # re-broadcast fired despite the timeout
        assert out["kind"] == "failed"    # …and the RPC trouble is still surfaced

    def test_no_rebroadcast_on_timeout_when_capped(self, qtbot, monkeypatch):
        import qeth.plugins.transactions as txmod
        sent: list = []

        class _FlakyClient:
            def __init__(self, chain): pass
            def rpc(self, m, p): raise Exception("timeout")
            def send_raw_transaction(self, raw): sent.append(raw)

        monkeypatch.setattr(txmod, "EthClient", _FlakyClient)
        # rebroadcast=False (cap reached / no raw) → don't re-push on failure.
        worker = txmod.PendingProbeWorker(ETH, self.HASH, ADDR, 5, "0xr", False)
        worker.run()
        assert sent == []


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

    def test_zero_balance_dropped_even_when_custom_or_pinned(
        self, tokens_plugin, monkeypatch,
    ):
        """Discovery drops any exactly-zero balance — including custom-added AND
        pinned (force-shown) tokens. Non-zero ones surface. ('pin'/'add' mean
        show-when-held, not show-a-zero.)"""
        host = _StubHost(address=ADDR)
        tokens_plugin.attach(host)
        tokens_plugin.widget()                       # build the panel
        czero = "0x" + "c0" * 20   # custom, zero
        cnon = "0x" + "c1" * 20    # custom, non-zero
        pzero = "0x" + "c2" * 20   # pinned, zero
        tokens_plugin._store.add_custom_token(1, czero)
        tokens_plugin._store.add_custom_token(1, cnon)
        tokens_plugin._store.force_show_token(1, pzero)

        view_key = (1, ADDR.lower())
        tokens_plugin._displayed_view = view_key
        captured: dict = {}
        monkeypatch.setattr(
            tokens_plugin, "_compute_visible_tokens",
            lambda chain, toks, prices, show_all=None: captured.update(t=toks) or [])
        pv = {
            "view_key": view_key, "chain": ETH, "address": ADDR, "native_wei": 0,
            "metadata": {czero: ("CZ", "Zero", 18), cnon: ("CN", "NonZero", 18),
                         pzero: ("PZ", "PinZero", 18)},
            "balances_raw": {czero: 0, cnon: 7, pzero: 0},
        }
        tokens_plugin._on_combined_ready(pv, 1, {})
        shown = {t.contract.lower() for t in captured["t"]}
        assert cnon in shown                       # non-zero surfaces
        assert czero not in shown                  # zero custom dropped
        assert pzero not in shown                  # zero pinned dropped too

    def test_custom_token_exempt_from_dust_filter(self, tokens_plugin):
        """A custom token with any non-zero balance shows even below the dust
        threshold; an ordinary dust token is filtered."""
        from decimal import Decimal
        from qeth.tokens import TokenBalance
        from qeth.prices import Price
        host = _StubHost()
        tokens_plugin.attach(host)
        custom = "0x" + "ca" * 20
        ordinary = "0x" + "0d" * 20
        tokens_plugin._store.add_custom_token(1, custom)
        toks = [
            TokenBalance(contract=custom, symbol="CUS", name="Custom",
                         decimals=18, balance_raw=1),
            TokenBalance(contract=ordinary, symbol="ORD", name="Ordinary",
                         decimals=18, balance_raw=1),
        ]
        tiny = Price(price_usd=Decimal("0.00000001"), timestamp=0,
                     source="test")  # sub-dust value
        prices = {custom: tiny, ordinary: tiny}
        visible = {t.symbol
                   for t in tokens_plugin._compute_visible_tokens(ETH, toks, prices)}
        assert "CUS" in visible        # custom: shown despite dust value
        assert "ORD" not in visible    # ordinary dust token: filtered

    def test_fresh_wallet_sets_displayed_view_before_discovery(
        self, tokens_plugin,
    ):
        # Regression: imported / newly-added wallets had no cache,
        # so the early-render branch (which is the only place
        # _displayed_view was being set) didn't fire. Discovery
        # would complete, but _on_combined_ready's stale-results
        # guard then dropped the results — the panel never
        # rendered the new wallet's tokens.
        host = _StubHost(address=ADDR)
        tokens_plugin._token_lists._loaded = True   # don't gate on net
        tokens_plugin.attach(host)
        # No cache for ADDR yet (fresh-import scenario).
        assert tokens_plugin._wallet_cache.load(1, ADDR) is None
        # _refresh kicks discovery; we just verify the view was
        # registered so the subsequent on_combined_ready won't
        # consider its results stale.
        tokens_plugin._refresh(ADDR)
        assert tokens_plugin._displayed_view == (1, ADDR.lower())

    def test_revisiting_fresh_wallet_during_in_flight_discovery_clears_panel(
        self, tokens_plugin,
    ):
        # Click sequence: fresh wallet B (no cache) → wallet A
        # (any) → fresh wallet B again, all while B's first
        # discovery is still running. The in_flight guard used to
        # return early before _displayed_view + panel-clear ran —
        # so the panel would keep showing A's rows AND B's
        # eventually-completed discovery would be dropped as
        # stale. Both fixed: panel clears + displayed_view updates
        # even when piggy-backing on an existing in-flight pass.
        host = _StubHost(address=ADDR)
        tokens_plugin._token_lists._loaded = True
        tokens_plugin.attach(host)

        # First click on B → marks in-flight + sets displayed_view.
        B = ADDR
        tokens_plugin._refresh(B)
        assert tokens_plugin._displayed_view == (1, B.lower())
        assert (1, B.lower()) in tokens_plugin._discovery_in_flight

        # Simulate clicking wallet A by directly nudging displayed_view
        # to a different key (as A's _refresh would).
        A_key = (1, "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43")
        tokens_plugin._displayed_view = A_key

        # Second click on B while its discovery is still in flight.
        # _displayed_view MUST switch back to B (so the in-flight
        # result isn't dropped) and the panel MUST be cleared (so
        # A's rows don't linger on B's view).
        tokens_plugin.widget()  # ensure panel exists
        # Seed the panel with something so the clear is observable.
        tokens_plugin.widget().show_balances(
            host.current_chain(), 10**18, [], {},
        )
        assert tokens_plugin.widget().table.rowCount() >= 1

        tokens_plugin._refresh(B)
        assert tokens_plugin._displayed_view == (1, B.lower())
        # Panel cleared by show_message.
        assert tokens_plugin.widget().table.rowCount() == 0


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


class TestCuratedListAsDiscoverySource:
    """Token lists (Uniswap/CoinGecko/Curve/1inch) are exposed via
    ``TokenLists.addresses_for_chain`` and unioned into the
    multicall set — so a wallet that holds e.g. USDC will get it
    discovered even when Blockscout hasn't indexed it for that
    holder yet. Frame-style scan, but reusing the lists we already
    fetch + cache."""

    def test_addresses_for_chain_filters_by_chain(self):
        from qeth.tokenlists import TokenListEntry, TokenLists
        lists = TokenLists()
        lists._index = {
            (1, "0xa"): TokenListEntry(
                chain_id=1, address="0xa", symbol="USDC", name="USDC",
                decimals=6, source="t",
            ),
            (1, "0xb"): TokenListEntry(
                chain_id=1, address="0xb", symbol="DAI", name="DAI",
                decimals=18, source="t",
            ),
            (137, "0xc"): TokenListEntry(
                chain_id=137, address="0xc", symbol="USDT", name="USDT",
                decimals=6, source="t",
            ),
        }
        assert sorted(lists.addresses_for_chain(1)) == ["0xa", "0xb"]
        assert lists.addresses_for_chain(137) == ["0xc"]
        assert lists.addresses_for_chain(42161) == []

    def test_metadata_prefill_populates_cache_from_lists(
        self, tokens_plugin,
    ):
        # MetadataWorker is the slow path (multicall name/symbol/
        # decimals on chain). The curated lists already carry that
        # data — prefilling means MetadataWorker can be skipped
        # entirely for curated contracts on first refresh.
        from qeth.tokenlists import TokenListEntry
        usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        tokens_plugin._token_lists._index = {
            (1, usdc): TokenListEntry(
                chain_id=1, address=usdc, symbol="USDC",
                name="USD Coin", decimals=6, source="uniswap",
            ),
        }
        # Cache is empty before prefill.
        assert tokens_plugin._token_metadata.missing(1, [usdc]) == [usdc]
        tokens_plugin._prefill_metadata_from_token_lists()
        # And populated after.
        assert tokens_plugin._token_metadata.missing(1, [usdc]) == []
        got = tokens_plugin._token_metadata.get(1, usdc)
        assert got["symbol"] == "USDC"
        assert got["name"] == "USD Coin"
        assert got["decimals"] == 6

    def test_metadata_prefill_handles_no_lists_loaded(
        self, tokens_plugin,
    ):
        # Pre-load state must not raise — the empty index walks
        # cleanly and the cache stays untouched.
        tokens_plugin._token_lists._index = {}
        tokens_plugin._prefill_metadata_from_token_lists()
        assert tokens_plugin._token_metadata.missing(
            1, ["0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"]
        ) == ["0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"]


class TestReceiptTransferScan:
    """When a tx confirms, parse the receipt logs for ERC-20
    Transfer events whose from/to is one of our wallets. Those
    contracts join the next multicall set for the affected wallet —
    catches swap output tokens at chain-head speed instead of
    waiting minutes for Blockscout to index them."""

    TRANSFER_TOPIC0 = (
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )
    USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    ME = "0x747e9Baf074A770655d0C2EF4A46dB83Ad1Ed93F"
    UNI_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"

    def _topic_addr(self, addr: str) -> str:
        # ERC-20 Transfer encodes addresses as 32-byte left-padded
        # hex strings in topics[1] / topics[2].
        return "0x" + "0" * 24 + addr[2:].lower()

    def _swap_receipt(self) -> dict:
        # Synthesised receipt: ME sends USDT to the router, gets
        # USDC back. The dust filter would still drop unrelated
        # transfers in real receipts; here we only assert which
        # contracts get extracted.
        return {
            "logs": [
                {
                    "address": self.USDT,
                    "topics": [
                        self.TRANSFER_TOPIC0,
                        self._topic_addr(self.ME),
                        self._topic_addr(self.UNI_ROUTER),
                    ],
                    "data": "0x" + "00" * 32,
                },
                {
                    "address": self.USDC,
                    "topics": [
                        self.TRANSFER_TOPIC0,
                        self._topic_addr(self.UNI_ROUTER),
                        self._topic_addr(self.ME),
                    ],
                    "data": "0x" + "00" * 32,
                },
            ],
        }

    def test_extracts_contracts_from_swap_receipt(self, tokens_plugin):
        tokens_plugin._store.add_account({
            "address": self.ME, "source": "hot", "label": "Me",
        })
        tokens_plugin.note_receipt_logs(ETH, self._swap_receipt())
        key = (ETH.chain_id, self.ME.lower())
        contracts = {c.lower() for c in tokens_plugin._receipt_contracts[key]}
        assert contracts == {self.USDC.lower(), self.USDT.lower()}

    def test_ignores_transfers_not_touching_our_wallets(
        self, tokens_plugin,
    ):
        # Two unrelated addresses — neither is in our store.
        other_a = "0x1111111111111111111111111111111111111111"
        other_b = "0x2222222222222222222222222222222222222222"
        receipt = {
            "logs": [{
                "address": self.USDC,
                "topics": [
                    self.TRANSFER_TOPIC0,
                    self._topic_addr(other_a),
                    self._topic_addr(other_b),
                ],
                "data": "0x" + "00" * 32,
            }],
        }
        tokens_plugin.note_receipt_logs(ETH, receipt)
        assert tokens_plugin._receipt_contracts == {}

    def test_ignores_erc721_transfers(self, tokens_plugin):
        # ERC-721 Transfer has the same selector but a 4th
        # indexed topic (the tokenId). len(topics)!=3 must skip.
        tokens_plugin._store.add_account({
            "address": self.ME, "source": "hot", "label": "Me",
        })
        nft = "0x1234567890123456789012345678901234567890"
        receipt = {
            "logs": [{
                "address": nft,
                "topics": [
                    self.TRANSFER_TOPIC0,
                    self._topic_addr(self.UNI_ROUTER),
                    self._topic_addr(self.ME),
                    "0x" + "00" * 31 + "01",  # tokenId
                ],
                "data": "0x",
            }],
        }
        tokens_plugin.note_receipt_logs(ETH, receipt)
        assert tokens_plugin._receipt_contracts == {}

    def test_handles_empty_or_missing_logs(self, tokens_plugin):
        # Three flavours of "nothing to do" — must all no-op
        # without raising.
        tokens_plugin.note_receipt_logs(ETH, {})
        tokens_plugin.note_receipt_logs(ETH, {"logs": []})
        tokens_plugin.note_receipt_logs(ETH, None)
        assert tokens_plugin._receipt_contracts == {}

    def test_logless_receipt_from_our_wallet_refreshes_displayed_view(
        self, tokens_plugin, monkeypatch,
    ):
        """A confirmed tx changed the sender's NATIVE balance by
        construction (gas + value) even when it emitted no Transfer
        events — a plain send, or custom events only (TAC bridge /
        system-contract calls). The displayed view must refresh
        immediately, not wait for the 60 s sweep (the only floor on
        chains with no working ws)."""
        from types import SimpleNamespace
        tokens_plugin._store.add_account({
            "address": self.ME, "source": "hot", "label": "Me",
        })
        tokens_plugin.host = SimpleNamespace(
            selected_address=self.ME,
            current_chain=lambda: ETH,
        )
        called = []
        monkeypatch.setattr(tokens_plugin, "_invalidate_view_and_refresh",
                            lambda: called.append(True))
        tokens_plugin.note_receipt_logs(
            ETH, {"from": self.ME.lower(), "to": self.UNI_ROUTER.lower(),
                  "logs": []})
        assert called == [True]
        # …but a logless receipt between strangers must not refresh.
        called.clear()
        tokens_plugin.note_receipt_logs(
            ETH, {"from": "0x" + "11" * 20, "to": "0x" + "22" * 20,
                  "logs": []})
        assert called == []

    def test_handles_attributedict_receipt_from_web3py(self, tokens_plugin):
        # web3.py 7 returns receipts as AttributeDict which is a
        # Mapping but NOT a dict subclass. The earlier
        # ``isinstance(receipt, dict)`` early-return skipped every
        # real receipt — discovery never picked up the post-tx
        # balances. Real receipts must reach the loop.
        from web3.datastructures import AttributeDict
        tokens_plugin._store.add_account({
            "address": self.ME, "source": "hot", "label": "Me",
        })
        receipt = AttributeDict({
            "logs": [AttributeDict({
                "address": self.USDC,
                "topics": [
                    self.TRANSFER_TOPIC0,
                    self._topic_addr(self.UNI_ROUTER),
                    self._topic_addr(self.ME),
                ],
                "data": "0x" + "00" * 32,
            })],
        })
        tokens_plugin.note_receipt_logs(ETH, receipt)
        key = (ETH.chain_id, self.ME.lower())
        assert self.USDC.lower() in {
            c.lower() for c in tokens_plugin._receipt_contracts[key]
        }

    def test_handles_hexbytes_topics_from_web3py(self, tokens_plugin):
        # web3.py 7 returns log topics as HexBytes — str(HexBytes(b'…'))
        # is the BYTES literal, not the hex form. The earlier
        # str(topics[0]).lower() == TRANSFER_TOPIC0 check silently
        # never matched. Real receipts must work end-to-end.
        from hexbytes import HexBytes
        tokens_plugin._store.add_account({
            "address": self.ME, "source": "hot", "label": "Me",
        })
        receipt = {
            "logs": [{
                "address": self.USDT,
                "topics": [
                    HexBytes(self.TRANSFER_TOPIC0),
                    HexBytes(self._topic_addr(self.UNI_ROUTER)),
                    HexBytes(self._topic_addr(self.ME)),
                ],
                "data": HexBytes("0x" + "00" * 32),
            }],
        }
        tokens_plugin.note_receipt_logs(ETH, receipt)
        key = (ETH.chain_id, self.ME.lower())
        contracts = {c.lower() for c in tokens_plugin._receipt_contracts[key]}
        assert contracts == {self.USDT.lower()}

    def test_credits_received_tokens_directly_to_recipient_cache(
        self, tokens_plugin,
    ):
        # The user's exact failing case: sender wallet broadcasts a
        # USDT transfer to receiver wallet. After the receipt
        # confirms, the receiver's CACHE must hold USDT — even if
        # the receiver isn't the current view (so the next time
        # the user opens it, USDT is already there).
        from qeth.tokenlists import TokenListEntry
        USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
        tokens_plugin._store.add_account({
            "address": self.ME, "source": "hot", "label": "Me",
        })
        # Curated entry → metadata prefill knows USDT.
        tokens_plugin._token_lists._index = {
            (1, USDT): TokenListEntry(
                chain_id=1, address=USDT, symbol="USDT",
                name="Tether USD", decimals=6, source="curated",
            ),
        }
        tokens_plugin._prefill_metadata_from_token_lists()

        # Synthesize a Transfer(ME ← someone, 500 USDT).
        # 500 * 1e6 = 500_000_000 = 0x1DCD6500 padded to 32 bytes.
        value_hex = "0x" + "0" * 56 + "1dcd6500"
        sender = "0xD4fB1AEC8bEEb79c66603016A7c204FCbAA56E3B"
        receipt = {
            "logs": [{
                "address": USDT,
                "topics": [
                    self.TRANSFER_TOPIC0,
                    self._topic_addr(sender),
                    self._topic_addr(self.ME),
                ],
                "data": value_hex,
            }],
        }
        tokens_plugin.note_receipt_logs(ETH, receipt)

        # Cache now has USDT @ 500_000_000 for ME.
        cached = tokens_plugin._wallet_cache.load(1, self.ME.lower())
        assert cached is not None
        usdt_entries = [t for t in cached.tokens
                        if t.contract.lower() == USDT.lower()]
        assert len(usdt_entries) == 1
        assert usdt_entries[0].balance_raw == 500_000_000
        assert usdt_entries[0].symbol == "USDT"

    def test_credit_bumps_existing_token_balance(self, tokens_plugin):
        # If the recipient already had some of this token cached,
        # apply the delta in place rather than appending a dup.
        from qeth.wallet_cache import CachedToken, CachedWallet
        USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
        tokens_plugin._store.add_account({
            "address": self.ME, "source": "hot", "label": "Me",
        })
        # Pre-seed cache with 100 USDT.
        cw = CachedWallet(
            chain_id=1, address=self.ME.lower(),
            native_balance_wei=0,
        )
        cw.tokens.append(CachedToken(
            contract=USDT, symbol="USDT", name="Tether USD",
            decimals=6, logo_uri=None,
            balance_raw=100_000_000, price_usd=None,
            balance_updated=0, price_updated=0,
        ))
        tokens_plugin._wallet_cache.save(cw)

        # Receive another 500 USDT.
        value_hex = "0x" + "0" * 56 + "1dcd6500"
        receipt = {
            "logs": [{
                "address": USDT,
                "topics": [
                    self.TRANSFER_TOPIC0,
                    self._topic_addr(self.UNI_ROUTER),
                    self._topic_addr(self.ME),
                ],
                "data": value_hex,
            }],
        }
        tokens_plugin.note_receipt_logs(ETH, receipt)

        cached = tokens_plugin._wallet_cache.load(1, self.ME.lower())
        usdt_entries = [t for t in cached.tokens
                        if t.contract.lower() == USDT.lower()]
        assert len(usdt_entries) == 1  # no duplicate
        # 100 + 500 = 600 USDT raw.
        assert usdt_entries[0].balance_raw == 600_000_000

    def test_credit_skips_unknown_token_without_metadata(
        self, tokens_plugin,
    ):
        # If the inbound contract isn't in our metadata cache (not
        # curated, never seen), don't fabricate a CachedToken with
        # placeholder symbol/decimals — wait for the next full
        # discovery to fetch metadata properly.
        unknown = "0x" + "01" * 20
        tokens_plugin._store.add_account({
            "address": self.ME, "source": "hot", "label": "Me",
        })
        # Token_metadata cache empty (no prefill).
        value_hex = "0x" + "0" * 56 + "1dcd6500"
        receipt = {
            "logs": [{
                "address": unknown,
                "topics": [
                    self.TRANSFER_TOPIC0,
                    self._topic_addr(self.UNI_ROUTER),
                    self._topic_addr(self.ME),
                ],
                "data": value_hex,
            }],
        }
        tokens_plugin.note_receipt_logs(ETH, receipt)

        cached = tokens_plugin._wallet_cache.load(1, self.ME.lower())
        # Either no cache file at all OR an empty token list —
        # either way, no garbage entry was added.
        if cached is not None:
            assert not [t for t in cached.tokens
                        if t.contract.lower() == unknown.lower()]
        # And the contract IS stashed for the next discovery,
        # which will fetch metadata properly.
        key = (1, self.ME.lower())
        assert unknown in {
            c.lower() for c in tokens_plugin._receipt_contracts[key]
        }

    def test_receipt_extras_drained_into_discovery(self, tokens_plugin):
        # Stash some contracts under (chain, addr) directly, then
        # simulate on_discovered consuming them by popping the
        # same key. After pop, the dict shouldn't carry them.
        key = (ETH.chain_id, self.ME.lower())
        tokens_plugin._receipt_contracts[key] = {self.USDC, self.USDT}
        # Simulate on_discovered's drain.
        drained = tokens_plugin._receipt_contracts.pop(key, set())
        assert {c.lower() for c in drained} == {
            self.USDC.lower(), self.USDT.lower(),
        }
        assert key not in tokens_plugin._receipt_contracts


# --- WalletsPlugin ----------------------------------------------------------

@pytest.fixture
def wallets_plugin(qtbot, tmp_qeth):
    from qeth.store import Store
    from qeth.plugins.wallets import WalletsPlugin
    store = Store.load()
    plugin = WalletsPlugin(store)
    qtbot.addWidget(plugin.widget())
    return plugin


class TestWalletsTreeExpansion:
    """Switching the default account rebuilds the tree; the user's
    collapse/expand state must survive the rebuild (it used to be reset
    to all-expanded every time)."""

    def _plugin(self, qtbot, default):
        from qeth.store import Store
        from qeth.plugins.wallets import WalletsPlugin
        a1 = "0x" + "11" * 20
        a2 = "0x" + "22" * 20
        store = Store.load()
        store.accounts = [
            {"address": a1, "source": "ledger", "scheme": "Ledger Live", "label": ""},
            {"address": a2, "source": "ledger", "scheme": "Ledger Live", "label": ""},
        ]
        store.default_account = a1 if default == 1 else a2
        plugin = WalletsPlugin(store)
        qtbot.addWidget(plugin.widget())
        plugin._rebuild_tree()
        return plugin, store, a1, a2

    def test_collapsed_group_survives_default_switch(self, qtbot, tmp_qeth):
        plugin, store, a1, a2 = self._plugin(qtbot, default=1)
        tree = plugin._tree
        ledger_root = tree.topLevelItem(0)
        assert ledger_root.isExpanded()          # default: expanded on first build
        ledger_root.setExpanded(False)           # user collapses it
        # switching the default account triggers a rebuild
        store.default_account = a2
        plugin._rebuild_tree()
        assert tree.topLevelItem(0).isExpanded() is False   # stays collapsed

    def test_expanded_group_still_expands(self, qtbot, tmp_qeth):
        # the inverse: an expanded group stays expanded across rebuild
        plugin, store, a1, a2 = self._plugin(qtbot, default=1)
        store.default_account = a2
        plugin._rebuild_tree()
        assert plugin._tree.topLevelItem(0).isExpanded() is True


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

    def test_action_widgets_are_account_buttons(self, wallets_plugin):
        # Add / Copy / Remove mount on the slot's bottom row (symmetric
        # with the Tokens panel), so action_widgets() exposes them.
        wallets_plugin.widget()  # ensure the buttons are built
        actions = wallets_plugin.action_widgets()
        assert actions == wallets_plugin._account_buttons
        assert len(actions) == 3  # Add, Copy, Remove

    def test_account_buttons_mirror_action_enabled(self, wallets_plugin):
        # Copy/Remove are QPushButtons (styled like the Tokens Send
        # button), no longer QToolButton.setDefaultAction — so their
        # enabled state is wired to the action via enabledChanged. Guard
        # that wiring: the buttons must track the actions both ways.
        wallets_plugin.widget()
        _add, copy_btn, remove_btn = wallets_plugin.action_widgets()
        assert not copy_btn.isEnabled() and not remove_btn.isEnabled()
        wallets_plugin.act_copy.setEnabled(True)
        wallets_plugin.act_remove.setEnabled(True)
        assert copy_btn.isEnabled() and remove_btn.isEnabled()
        wallets_plugin.act_copy.setEnabled(False)
        assert not copy_btn.isEnabled()

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
        dlg._on_ens_reverse(addr, "vitalik.eth")
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
        dlg._on_ens_reverse(addr, "vitalik.eth")
        assert dlg.label_edit.text() == "My label"

    def test_add_watch_only_dialog_resolves_ens_name(self, qtbot, tmp_qeth):
        """Typing an ENS name forward-resolves to an address: Add enables,
        the resolved address shows, the name pre-fills the Label, and accept
        stores the resolved (checksummed) address — not the name text."""
        from qeth.plugins.wallets import AddWatchOnlyDialog
        dlg = AddWatchOnlyDialog(set())
        qtbot.addWidget(dlg)
        dlg.address_edit.setText("vitalik.eth")
        # A name (not a 0x address) leaves Add disabled until it resolves.
        assert not dlg.add_btn.isEnabled()
        dlg._on_ens_forward(
            "vitalik.eth", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
        assert dlg.add_btn.isEnabled()
        assert dlg.resolved_lbl.isVisibleTo(dlg)
        assert dlg.label_edit.text() == "vitalik.eth"   # default label
        dlg._on_accept()
        acct = dlg.result_account()
        assert acct["address"] == "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
        assert acct["source"] == "watch_only"

    def test_add_watch_only_dialog_unresolved_ens_stays_disabled(
        self, qtbot, tmp_qeth,
    ):
        from qeth.plugins.wallets import AddWatchOnlyDialog
        dlg = AddWatchOnlyDialog(set())
        qtbot.addWidget(dlg)
        dlg.address_edit.setText("does-not-exist-zzz.eth")
        dlg._on_ens_forward("does-not-exist-zzz.eth", "")   # empty = not found
        assert not dlg.add_btn.isEnabled()
        assert dlg.resolved_lbl.isVisibleTo(dlg)                 # shows "not found"

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
        dlg._on_ens_reverse(
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


class TestDetailsEventsView:
    """The transaction-details events list: decode receipt logs, default
    to Transfer/Approval events touching our wallets, toggle to show all,
    prefix known tokens with their symbol."""

    def _dialog(self, qtbot, *, token_info=None):
        from unittest.mock import MagicMock
        from qeth.plugins.transactions import TransactionDetailsDialog
        tx = Transaction(
            chain_id=1, hash="0x" + "aa" * 32, block_number=1, timestamp=1700,
            nonce=1, from_addr=ADDR, to_addr="0x" + "22" * 20, value_wei=0,
            gas_used=21000, gas_price_wei=10**9, method_id="", input_data="0x",
            success=True,
        )
        abi_cache = MagicMock()
        abi_cache.load.return_value = None   # nothing cached → fetch path
        dlg = TransactionDetailsDialog(
            tx, ETH, abi_source=MagicMock(), abi_cache=abi_cache,
            start_worker=lambda w: None,
            token_info=token_info or (lambda cid, a: None),
            icon_cache=None, native_price_usd=None, known_addresses=[ADDR],
        )
        qtbot.addWidget(dlg)
        return dlg

    def _logs(self):
        from qeth.abi import _TRANSFER_TOPIC, _APPROVAL_TOPIC
        def ta(a): return "0x" + "00" * 12 + a[2:]
        usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        return [
            # Transfer TO us — should pass the default filter.
            {"address": usdc, "topics": [_TRANSFER_TOPIC, ta("0x" + "bb" * 20),
                                          ta(ADDR)], "data": "0x" + f"{100:064x}"},
            # Approval between two strangers — filtered out by default.
            {"address": usdc, "topics": [_APPROVAL_TOPIC, ta("0x" + "cc" * 20),
                                          ta("0x" + "dd" * 20)],
             "data": "0x" + f"{5:064x}"},
            # Unknown event — only shown under "show all", rendered raw.
            {"address": "0x" + "ee" * 20, "topics": ["0x" + "de" * 32], "data": "0x"},
        ]

    def test_events_live_in_their_own_tab(self, qtbot, tmp_qeth):
        from PySide6.QtWidgets import QTabWidget
        dlg = self._dialog(qtbot)
        tabs = dlg.findChild(QTabWidget)
        assert tabs is not None
        assert [tabs.tabText(i) for i in range(tabs.count())] == [
            "&Details", "&Events"]

    def test_default_filters_to_our_transfers(self, qtbot, tmp_qeth):
        dlg = self._dialog(qtbot)
        dlg._events.set_logs(self._logs())
        text = dlg._events.events_view.toPlainText()
        assert "Transfer(" in text
        assert ADDR.lower() in text.lower()
        assert "Approval(" not in text          # stranger approval hidden
        assert "unknown event" not in text       # unknown hidden by default
        assert dlg._events.show_all_events_btn.isEnabled()

    def test_show_all_reveals_every_event(self, qtbot, tmp_qeth):
        dlg = self._dialog(qtbot)
        dlg._events.set_logs(self._logs())
        dlg._events._on_show_all_events(True)
        text = dlg._events.events_view.toPlainText()
        assert "Transfer(" in text
        assert "Approval(" in text               # now visible
        assert "unknown event" in text           # raw fallback

    def test_show_all_names_unknown_event_once_abi_arrives(self, qtbot, tmp_qeth):
        from qeth.abi import _event_topic0
        dlg = self._dialog(qtbot)
        contract = "0x" + "ee" * 20
        topic = _event_topic0("Deposit(address,uint256)")
        log = {"address": contract,
               "topics": [topic, "0x" + "00" * 12 + ADDR[2:]],
               "data": "0x" + f"{42:064x}"}
        dlg._events.set_logs([log])
        dlg._events._on_show_all_events(True)
        # Not cached → renders raw and a fetch is kicked for the contract.
        assert "unknown event" in dlg._events.events_view.toPlainText()
        assert contract in dlg._events._abi_inflight
        # Simulate the ABI landing → the event is named + decoded.
        abi = [{"type": "event", "name": "Deposit", "anonymous": False,
                "inputs": [
                    {"name": "dst", "type": "address", "indexed": True},
                    {"name": "wad", "type": "uint256", "indexed": False}]}]
        dlg._events._on_event_abi_ready(contract, abi)
        text = dlg._events.events_view.toPlainText()
        assert "Deposit(" in text and "dst" in text and "wad" in text
        assert "unknown event" not in text

    def test_event_amounts_get_human_readable_comment(self, qtbot, tmp_qeth):
        from types import SimpleNamespace
        usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        token_info = lambda cid, a: (
            SimpleNamespace(symbol="USDC", decimals=6)
            if a.lower() == usdc else None
        )
        dlg = self._dialog(qtbot, token_info=token_info)
        from qeth.abi import _TRANSFER_TOPIC, _APPROVAL_TOPIC
        def ta(a): return "0x" + "00" * 12 + a[2:]
        max_uint = (1 << 256) - 1
        dlg._events.set_logs([
            {"address": usdc, "topics": [_TRANSFER_TOPIC, ta("0x" + "bb" * 20),
                                          ta(ADDR)], "data": "0x" + f"{5_000_000:064x}"},
            {"address": usdc, "topics": [_APPROVAL_TOPIC, ta(ADDR),
                                          ta("0x" + "cc" * 20)],
             "data": "0x" + f"{max_uint:064x}"},
        ])
        text = dlg._events.events_view.toPlainText()
        assert "# 5 USDC" in text                 # transfer amount
        assert "# unlimited USDC" in text          # max approval

    def test_known_token_gets_symbol_prefix(self, qtbot, tmp_qeth):
        from types import SimpleNamespace
        usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        token_info = lambda cid, a: (
            SimpleNamespace(symbol="USDC") if a.lower() == usdc else None
        )
        dlg = self._dialog(qtbot, token_info=token_info)
        dlg._events.set_logs(self._logs())
        assert "USDC" in dlg._events.events_view.toPlainText()


class _SimList:
    """Live filtered view over a worker list, exposing only the entries
    of ``cls`` (the dialogs also start AbiFetch / GasSuggestion workers on
    construction; tests want to count just the simulation ones)."""

    def __init__(self, backing, cls):
        self._backing = backing
        self._cls = cls

    def _items(self):
        return [w for w in self._backing if isinstance(w, self._cls)]

    def __len__(self):
        return len(self._items())

    def __iter__(self):
        return iter(self._items())

    def __getitem__(self, i):
        return self._items()[i]

    def __bool__(self):
        return bool(self._items())


class TestEventPreviewTab:
    """Send / dapp-Sign dialogs gain an 'Events' tab that previews the
    tx's logs via local revm simulation — lazy on tab-open, cached until
    the tx params change, and graceful when the fork engine is absent."""

    def _started(self):
        # The dialogs also kick AbiFetch / GasSuggestion workers on
        # construction; the helper returns only the simulation workers.
        from qeth.plugins.transactions import SimulateWorker
        all_workers: list = []
        sims = _SimList(all_workers, SimulateWorker)
        return sims, all_workers.append

    def _send(self, qtbot, started):
        from unittest.mock import MagicMock
        from qeth.plugins.transactions import SendTokenDialog
        abi_cache = MagicMock()
        abi_cache.load.return_value = None
        asset = {"symbol": "USDC", "decimals": 6, "is_native": False,
                 "contract": "0x" + "a0" * 20, "balance_raw": 10 ** 12}
        dlg = SendTokenDialog(
            asset, ETH, ADDR, abi_source=MagicMock(), abi_cache=abi_cache,
            start_worker=started, token_info=lambda c, a: None,
            icon_cache=None, native_price_usd=None, known_addresses=[ADDR],
        )
        qtbot.addWidget(dlg)
        return dlg

    def _sign(self, qtbot, started, *, to_addr="0x" + "22" * 20,
              data="0xa9059cbb"):
        from unittest.mock import MagicMock
        from qeth.plugins.transactions import SignTransactionDialog
        from qeth.signing import SigningRequest
        abi_cache = MagicMock()
        abi_cache.load.return_value = None
        req = SigningRequest(chain_id=1, from_addr=ADDR, to_addr=to_addr,
                             value_wei=0, data=data)
        dlg = SignTransactionDialog(
            req, ETH, abi_source=MagicMock(), abi_cache=abi_cache,
            start_worker=started, token_info=lambda c, a: None,
            icon_cache=None, native_price_usd=None, known_addresses=[ADDR],
        )
        qtbot.addWidget(dlg)
        return dlg

    def test_replace_mode_locks_nonce_and_floors_fees(self, qtbot, tmp_qeth):
        """Speed up / cancel re-uses SignTransactionDialog in replace mode:
        the nonce is locked to the pending tx's and the suggested fees are
        clamped UP to the bump floor (even when the network has since
        dipped below it)."""
        from unittest.mock import MagicMock
        from qeth.plugins.transactions import SignTransactionDialog
        from qeth.signing import ReplacementFloor, SigningRequest
        _, started = self._started()
        floor = ReplacementFloor(max_fee_per_gas=60_000_000_000,
                                 max_priority_fee_per_gas=3_000_000_000)
        req = SigningRequest(chain_id=1, from_addr=ADDR, to_addr="0x" + "22" * 20,
                             value_wei=0, data="0x", nonce=7,
                             max_fee_per_gas=60_000_000_000,
                             max_priority_fee_per_gas=3_000_000_000)
        abi_cache = MagicMock()
        abi_cache.load.return_value = None
        dlg = SignTransactionDialog(
            req, ETH, abi_source=MagicMock(), abi_cache=abi_cache,
            start_worker=started, token_info=lambda c, a: None,
            icon_cache=None, native_price_usd=None, known_addresses=[ADDR],
            fixed_nonce=7, fee_floor=floor,
            replace_label="Speed Up Transaction",
        )
        qtbot.addWidget(dlg)
        assert dlg.windowTitle() == "Speed Up Transaction"
        # Network suggestion comes back BELOW the floor + a different nonce.
        dlg._on_gas_suggested({
            "base_fee": 20_000_000_000, "estimated_gas": 21000, "gas": 21000,
            "max_fee_per_gas": 25_000_000_000,
            "max_priority_fee_per_gas": 1_000_000_000,
            "nonce": 99,
        })
        assert dlg.spin_max_fee.value() == 60.0      # clamped up to floor (gwei)
        assert dlg.spin_priority.value() == 3.0
        fin = dlg.finalised_request()
        assert fin.nonce == 7                        # fixed, not the suggested 99
        assert fin.max_fee_per_gas == 60_000_000_000

    def test_gnosis_tiny_tip_survives_the_spinbox(self, qtbot, tmp_qeth):
        """Gnosis idle: base fee ~610 kwei → suggested tip ~30 kwei =
        0.000030528 gwei, below the spinbox's default 4 decimals.
        QDoubleSpinBox rounds the STORED value, so without widening the
        precision the tip quantizes to 0.0 and the tx is signed with a
        0-wei tip Gnosis rejects ("FeeTooLow … 0 < 1") — silently
        undoing apply_gas_policy's floor."""
        _, started = self._started()
        dlg = self._sign(qtbot, started)
        dlg._on_gas_suggested({
            "base_fee": 610_575, "estimated_gas": 21_000, "gas": 21_000,
            "max_fee_per_gas": 1_221_150,         # 2 × base
            "max_priority_fee_per_gas": 30_528,   # 5 % of base
            "nonce": 0,
        })
        fin = dlg.finalised_request()
        assert fin.max_priority_fee_per_gas == 30_528
        assert fin.max_fee_per_gas == 1_221_150

    def test_one_wei_tip_floor_survives_the_spinbox(self, qtbot, tmp_qeth):
        """The 1-wei policy floor (fully idle Gnosis) needs the full
        9 decimals to round-trip."""
        _, started = self._started()
        dlg = self._sign(qtbot, started)
        dlg._on_gas_suggested({
            "base_fee": 18, "estimated_gas": 21_000, "gas": 21_000,
            "max_fee_per_gas": 36, "max_priority_fee_per_gas": 1,
            "nonce": 0,
        })
        assert dlg.finalised_request().max_priority_fee_per_gas == 1

    def test_both_dialogs_have_an_events_tab(self, qtbot, tmp_qeth):
        _, started = self._started()
        for dlg in (self._send(qtbot, started), self._sign(qtbot, started)):
            assert [dlg._tabs.tabText(i) for i in range(dlg._tabs.count())] \
                == ["&Details", "&Events"]

    def test_simulation_note_shows_as_placeholder(self, qtbot, tmp_qeth):
        """A SimulationNote outcome (e.g. calldata to a code-less TAC
        system contract) must surface its text — not render as an empty
        events list implying 'this tx does nothing'."""
        from qeth.simulate import SimulationNote
        _, started = self._started()
        dlg = self._send(qtbot, started)
        dlg._sim_key = ("k",)
        dlg._sim_done = False
        dlg._on_sim_ready(("k",), SimulationNote("(target has no code…)"))
        assert "no code" in dlg._events.events_view.toPlainText()

    def test_busy_spinner_animates_then_stops(self, qtbot, tmp_qeth):
        """set_busy shows an animated spinner; a result/placeholder stops
        the timer so it can't tick into a settled pane."""
        _, started = self._started()
        ev = self._send(qtbot, started)._events
        ev.set_busy("simulating…")
        assert ev._spin_timer.isActive()
        first = ev.events_view.toPlainText()
        assert "simulating…" in first
        ev._tick_spinner()                       # advance one frame
        assert ev.events_view.toPlainText() != first   # the glyph moved
        assert "simulating…" in ev.events_view.toPlainText()
        ev.set_logs([])                          # result arrives
        assert not ev._spin_timer.isActive()
        ev.set_busy("again…")
        ev.set_placeholder("(done)")             # or a placeholder
        assert not ev._spin_timer.isActive()

    def test_verified_badge_tracks_result_type(self, qtbot, tmp_qeth):
        """VerifiedLogs (Helios-backed simulation) shows the ⚡ verified
        badge; a plain unverified result hides it again; placeholders
        (new sim starting, failures) clear it."""
        from qeth.simulate import VerifiedLogs
        _, started = self._started()
        dlg = self._send(qtbot, started)
        ev = dlg._events
        assert ev.verified_lbl.isHidden()              # default: off
        dlg._sim_key, dlg._sim_done = ("k",), False
        dlg._on_sim_ready(("k",), VerifiedLogs([]))
        assert not ev.verified_lbl.isHidden()          # verified → badge
        dlg._sim_key, dlg._sim_done = ("k2",), False
        dlg._on_sim_ready(("k2",), [])                 # plain unverified
        assert ev.verified_lbl.isHidden()
        dlg._sim_key, dlg._sim_done = ("k3",), False
        dlg._on_sim_ready(("k3",), VerifiedLogs([]))
        ev.set_placeholder("(simulating…)")            # new sim starting
        assert ev.verified_lbl.isHidden()

    def test_send_blocked_until_inputs_valid(self, qtbot, tmp_qeth, monkeypatch):
        import qeth.simulate as sim
        monkeypatch.setattr(sim, "fork_available", lambda: True)
        workers, started = self._started()
        dlg = self._send(qtbot, started)
        dlg._maybe_simulate()                 # no recipient/amount yet
        assert "valid recipient" in dlg._events.events_view.toPlainText()
        assert not workers                    # nothing simulated

    def test_send_simulates_and_caches_until_inputs_change(
            self, qtbot, tmp_qeth, monkeypatch):
        import qeth.simulate as sim
        from qeth.plugins.transactions import SimulateWorker
        monkeypatch.setattr(sim, "fork_available", lambda: True)
        workers, started = self._started()
        dlg = self._send(qtbot, started)
        dlg.recipient_edit.setText("0x" + "bb" * 20)
        dlg.amount_edit.setText("5")
        dlg._maybe_simulate()
        assert len(workers) == 1 and isinstance(workers[0], SimulateWorker)
        dlg._maybe_simulate()                 # re-open, unchanged → cached
        assert len(workers) == 1
        dlg.amount_edit.setText("6")          # params change → re-simulate
        dlg._maybe_simulate()
        assert len(workers) == 2

    def test_send_ready_renders_simulated_transfer(self, qtbot, tmp_qeth):
        from qeth.abi import _TRANSFER_TOPIC
        _, started = self._started()
        dlg = self._send(qtbot, started)
        def ta(a): return "0x" + "00" * 12 + a[2:]
        usdc = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        logs = [{"address": usdc,
                 "topics": [_TRANSFER_TOPIC, ta(ADDR), ta("0x" + "bb" * 20)],
                 "data": "0x" + f"{100:064x}"}]
        dlg._sim_key = ("k",)                 # pretend this sim is active
        dlg._sim_done = False                  # …and in flight
        dlg._on_sim_ready(("k",), logs)
        text = dlg._events.events_view.toPlainText()
        assert "Transfer(" in text
        assert ADDR.lower() in text.lower()   # our wallet (sender) shown

    def test_sim_ready_ignores_stale_result(self, qtbot, tmp_qeth):
        _, started = self._started()
        dlg = self._send(qtbot, started)
        dlg._events.set_placeholder("(simulating…)")
        dlg._sim_key = ("current",)
        dlg._on_sim_ready(("old",), [])       # superseded → ignored
        assert "simulating" in dlg._events.events_view.toPlainText()

    def test_revert_shows_red_banner_and_events_note(
            self, qtbot, tmp_qeth, monkeypatch):
        from qeth.simulate import RevertNote
        _, started = self._started()
        dlg = self._send(qtbot, started)
        dlg._sim_key = ("k",)
        dlg._sim_done = False
        dlg._on_sim_ready(("k",), RevertNote("ERC20: insufficient allowance"))
        # Prominent red warning above Confirm (isHidden, not isVisible — the
        # dialog isn't shown in the test, but the banner is explicitly shown).
        banner = dlg.revert_banner()
        assert not banner.isHidden()
        assert "revert" in banner.text().lower()
        assert "insufficient allowance" in banner.text()
        # ...mirrored in the Events tab.
        assert "reverts" in dlg._events.events_view.toPlainText()

    def test_verified_revert_is_labelled(self, qtbot, tmp_qeth):
        from qeth.simulate import RevertNote
        _, started = self._started()
        dlg = self._send(qtbot, started)
        dlg._sim_key = ("k",)
        dlg._sim_done = False
        dlg._on_sim_ready(("k",), RevertNote("boom", verified=True))
        assert "verified" in dlg.revert_banner().text().lower()

    def test_inconclusive_sim_does_not_warn_of_revert(
            self, qtbot, tmp_qeth, monkeypatch):
        # None now means 'couldn't tell' (no route / transient), NOT a revert —
        # so no red banner and no revert wording.
        import qeth.simulate as sim
        monkeypatch.setattr(sim, "fork_available", lambda: True)
        _, started = self._started()
        dlg = self._send(qtbot, started)
        dlg._sim_key = ("k",)
        dlg._sim_done = False
        dlg._on_sim_ready(("k",), None)
        assert dlg.revert_banner().isHidden()
        assert "revert" not in dlg._events.events_view.toPlainText().lower()

    def test_no_route_shows_unavailable_hint(
            self, qtbot, tmp_qeth, monkeypatch):
        # No fork engine AND this endpoint already learned to lack simulateV1
        # → neither route can run, so show the 'no simulation' note.
        import qeth.simulate as sim
        monkeypatch.setattr(sim, "fork_available", lambda: False)
        monkeypatch.setitem(sim._SIMV1_SUPPORT, ETH.rpc_url, False)
        workers, started = self._started()
        dlg = self._send(qtbot, started)
        dlg.recipient_edit.setText("0x" + "bb" * 20)
        dlg.amount_edit.setText("5")
        dlg._maybe_simulate()
        text = dlg._events.events_view.toPlainText()
        assert "eth_simulateV1" in text and "py-evm" in text
        assert not workers                    # no worker kicked

    def test_simv1_endpoint_simulates_without_fork_engine(
            self, qtbot, tmp_qeth, monkeypatch):
        # No fork engine but the endpoint's simulateV1 support is unprobed →
        # still kick the worker (it'll try the fast path).
        import qeth.simulate as sim
        from qeth.plugins.transactions import SimulateWorker
        monkeypatch.setattr(sim, "fork_available", lambda: False)
        sim._SIMV1_SUPPORT.pop(ETH.rpc_url, None)
        workers, started = self._started()
        dlg = self._send(qtbot, started)
        dlg.recipient_edit.setText("0x" + "bb" * 20)
        dlg.amount_edit.setText("5")
        dlg._maybe_simulate()
        assert len(workers) == 1 and isinstance(workers[0], SimulateWorker)

    def test_sign_simulates_fixed_request_once(
            self, qtbot, tmp_qeth, monkeypatch):
        import qeth.simulate as sim
        monkeypatch.setattr(sim, "fork_available", lambda: True)
        workers, started = self._started()
        dlg = self._sign(qtbot, started)
        dlg._maybe_simulate()
        assert len(workers) == 1
        dlg._maybe_simulate()                 # fixed tx → cached
        assert len(workers) == 1

    def test_sign_contract_creation_blocked(self, qtbot, tmp_qeth):
        _, started = self._started()
        dlg = self._sign(qtbot, started, to_addr=None, data="0x60806040")
        dlg._maybe_simulate()
        assert "contract creation" in dlg._events.events_view.toPlainText()

    def test_timeout_resolves_tab_and_ignores_late_result(
            self, qtbot, tmp_qeth, monkeypatch):
        # A slow fork (Arbitrum-style) must not spin the tab forever: the
        # timeout resolves it, and a late worker result is ignored.
        import qeth.simulate as sim
        monkeypatch.setattr(sim, "fork_available", lambda: True)
        _, started = self._started()
        dlg = self._send(qtbot, started)
        dlg.recipient_edit.setText("0x" + "bb" * 20)
        dlg.amount_edit.setText("5")
        dlg._maybe_simulate()
        assert not dlg._sim_done                 # in flight, timer running
        key = dlg._sim_key
        dlg._on_sim_timeout()
        assert dlg._sim_done
        assert "timed out" in dlg._events.events_view.toPlainText()
        # The worker eventually returns — must be ignored, not re-rendered.
        dlg._on_sim_ready(key, [{"address": "0x" + "a0" * 20,
                                 "topics": [], "data": "0x"}])
        assert "timed out" in dlg._events.events_view.toPlainText()

    def test_close_detaches_in_flight_worker(
            self, qtbot, tmp_qeth, monkeypatch):
        # The dialogs are non-modal; a worker can outlive a closed dialog.
        # Closing must disconnect it so a late `ready` can't reach the
        # (deleted) dialog.
        import qeth.simulate as sim
        monkeypatch.setattr(sim, "fork_available", lambda: True)
        _, started = self._started()
        dlg = self._send(qtbot, started)
        dlg.recipient_edit.setText("0x" + "bb" * 20)
        dlg.amount_edit.setText("5")
        dlg._maybe_simulate()
        w = dlg._sim_worker
        assert w is not None
        dlg.reject()                             # emits finished → _detach_sim
        assert dlg._sim_worker is None
        # Late emit on the detached worker is now a no-op (no slot), so the
        # placeholder is untouched and nothing raises.
        before = dlg._events.events_view.toPlainText()
        w.ready.emit([{"address": "0x" + "a0" * 20, "topics": [], "data": "0x"}])
        assert dlg._events.events_view.toPlainText() == before
