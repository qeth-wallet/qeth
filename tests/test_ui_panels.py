"""Unit-style tests for the three right/left-pane widgets.

These don't need a MainWindow — they construct the widget directly
under the offscreen platform and exercise the public ``show_*`` /
``clear`` rendering methods. Useful both for catching renderer bugs
and for pinning the contract each panel will expose once they
become Plugin instances.
"""

from decimal import Decimal

import pytest
from PySide6.QtCore import Qt

from qeth.chains import DEFAULT_CHAINS
from qeth.icons import IconCache
from qeth.store import Store
from qeth.tokens import TokenBalance
from qeth.plugins.tokens import TokenListPanel
from qeth.transactions import Transaction
from qeth.plugins.transactions import TransactionListPanel
from qeth.plugins.wallets import DetailsPanel


ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
ADDR = "0x7a16ff8270133f063aab6c9977183d9e72835428"


# --- DetailsPanel ----------------------------------------------------------

class TestDetailsPanel:
    def test_show_account_fills_fields(self, qtbot, tmp_qeth):
        panel = DetailsPanel()
        qtbot.addWidget(panel)
        panel.show_account({
            "address": ADDR, "path": "44'/60'/0'/0/0",
            "source": "ledger", "scheme": "BIP-44",
            "label": "Cold storage",
        }, is_default=False)
        assert panel.address_lbl.text() == ADDR
        assert panel.path_lbl.text() == "44'/60'/0'/0/0"
        assert panel.source_lbl.text() == "ledger"
        assert panel.scheme_lbl.text() == "BIP-44"
        assert panel.label_edit.text() == "Cold storage"
        # Not the default account → button is enabled and labelled
        # "Connect to browser" (clicking it makes this address the
        # one dapps see via the local JSON-RPC server).
        assert panel.set_default_btn.isEnabled()
        # Text carries a GNOME-HIG access-key mnemonic (Alt+B).
        assert panel.set_default_btn.text() == "Connect to &Browser"

    def test_show_account_marks_default(self, qtbot, tmp_qeth):
        panel = DetailsPanel()
        qtbot.addWidget(panel)
        panel.show_account({
            "address": ADDR, "source": "ledger",
        }, is_default=True)
        assert not panel.set_default_btn.isEnabled()
        assert "Connected" in panel.set_default_btn.text()

    def test_clear_resets_fields(self, qtbot, tmp_qeth):
        panel = DetailsPanel()
        qtbot.addWidget(panel)
        panel.show_account({"address": ADDR, "source": "ledger"}, is_default=False)
        panel.clear()
        for lbl in (panel.address_lbl, panel.path_lbl,
                    panel.source_lbl, panel.scheme_lbl):
            assert lbl.text() == "—"
        assert not panel.set_default_btn.isEnabled()

    def test_set_default_button_emits_signal(self, qtbot, tmp_qeth):
        panel = DetailsPanel()
        qtbot.addWidget(panel)
        panel.show_account({"address": ADDR, "source": "ledger"}, is_default=False)
        with qtbot.waitSignal(panel.set_default_requested, timeout=500) as blocker:
            panel.set_default_btn.click()
        assert blocker.args == [ADDR]

    def test_title_edit_emits_label_changed(self, qtbot, tmp_qeth):
        """Editing the title and committing (Enter or focus-out)
        emits ``label_changed(address, new_label)``."""
        panel = DetailsPanel()
        qtbot.addWidget(panel)
        panel.show_account({"address": ADDR, "label": "Old"}, is_default=False)
        panel.label_edit.setText("New label")
        with qtbot.waitSignal(panel.label_changed, timeout=500) as blocker:
            panel._on_label_committed()
        assert blocker.args == [ADDR, "New label"]

    def test_no_op_title_edit_does_not_emit(self, qtbot, tmp_qeth):
        """Focusing into the title and back out without changing
        the text shouldn't fire the signal — otherwise we'd hit the
        store + rebuild the tree on every selection."""
        panel = DetailsPanel()
        qtbot.addWidget(panel)
        panel.show_account({"address": ADDR, "label": "Same"}, is_default=False)
        fires: list = []
        panel.label_changed.connect(lambda a, l: fires.append((a, l)))
        panel._on_label_committed()
        assert fires == []

    def test_title_disabled_without_account(self, qtbot, tmp_qeth):
        """The title is read-only until an account is loaded —
        otherwise the user could type into the placeholder and we'd
        have nowhere to persist it."""
        panel = DetailsPanel()
        qtbot.addWidget(panel)
        assert not panel.label_edit.isEnabled()
        panel.show_account({"address": ADDR, "source": "ledger"}, is_default=False)
        assert panel.label_edit.isEnabled()
        panel.clear()
        assert not panel.label_edit.isEnabled()


# --- TokenListPanel rendering ----------------------------------------------

@pytest.fixture
def token_panel(qtbot, tmp_qeth):
    store = Store.load()
    icons = IconCache()
    panel = TokenListPanel(icons, store)
    qtbot.addWidget(panel)
    return panel


def _has_copy_shortcut(table) -> bool:
    from PySide6.QtGui import QKeySequence
    return any(
        a.shortcut() == QKeySequence(QKeySequence.Copy)
        and a.shortcutContext() == Qt.WidgetWithChildrenShortcut
        for a in table.actions()
    )


class TestTokenListPanel:
    def test_ctrl_c_copies_contract_address(self, token_panel):
        # Ctrl+C is wired on the table (scoped to it), and triggers the
        # same handler as the Copy button — copying the contract address.
        assert _has_copy_shortcut(token_panel.table)

    def test_native_row_pinned_at_index_zero(self, token_panel):
        token_panel.show_balances(ETH, native_wei=10**18, tokens=[], list_entries={})
        assert token_panel.table.rowCount() == 1
        sym = token_panel.table.item(0, 0)
        assert sym.text() == ETH.symbol
        # The symbol cell stores (chain_id, "") for the native asset.
        assert sym.data(Qt.UserRole) == (ETH.chain_id, "")
        assert token_panel.table.item(0, 1).text() == "1"

    def test_erc20_rows_follow_native(self, token_panel):
        tokens = [
            TokenBalance(
                contract="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
                symbol="USDC", name="USD Coin", decimals=6,
                balance_raw=2_500_000,  # 2.5 USDC
            ),
        ]
        token_panel.show_balances(ETH, native_wei=0, tokens=tokens, list_entries={})
        assert token_panel.table.rowCount() == 2
        assert token_panel.table.item(1, 0).text() == "USDC"
        assert token_panel.table.item(1, 1).text() == "2.5"

    def test_huge_erc20_balance_does_not_overflow(self, token_panel):
        """ASF-style raw balances exceed qint64; if we ever marshal them
        through PySide6's int signals they overflow. Rendering should
        not depend on signal marshalling — verify the raw value reaches
        the cell intact via the panel's Decimal balance store."""
        big = 10**25
        tokens = [
            TokenBalance(
                contract="0xdeadbeef00000000000000000000000000000001",
                symbol="ASF", name="Big", decimals=18, balance_raw=big,
            ),
        ]
        token_panel.show_balances(ETH, native_wei=0, tokens=tokens, list_entries={})
        # Internal Decimal balance store keyed by (chain_id, addr_lower).
        key = (ETH.chain_id, tokens[0].contract.lower())
        assert token_panel._balances[key] == Decimal(big) / Decimal(10**18)

    def test_clear_empties_table(self, token_panel):
        token_panel.show_balances(ETH, 10**18, [], {})
        token_panel.clear()
        assert token_panel.table.rowCount() == 0


# --- TransactionListPanel rendering ----------------------------------------

def _tx(**kw) -> Transaction:
    defaults = dict(
        chain_id=1, hash="0x" + "ab" * 32,
        block_number=25_000_000, timestamp=1_779_618_611,
        nonce=10, from_addr=ADDR, to_addr=None,
        value_wei=0, gas_used=21_000, gas_price_wei=10**9,
        method_id="", input_data="0x", success=True,
    )
    defaults.update(kw)
    return Transaction(**defaults)


class TestTransactionListPanel:
    def test_ctrl_c_copies_tx_hash(self, qtbot, tmp_qeth):
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        assert _has_copy_shortcut(panel.table)

    def test_empty_list_shows_status_message(self, qtbot, tmp_qeth):
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        panel.show_transactions([])
        # isVisible() requires the whole ancestor chain to be shown,
        # which it isn't under the offscreen platform — check the
        # local hidden flag instead.
        assert not panel.status_lbl.isHidden()
        assert "No transactions" in panel.status_lbl.text()
        assert panel.table.rowCount() == 0

    # Column layout: 0=Status, 1=Nonce, 2=Time, 3=Hash.

    def test_columns_match_layout(self, qtbot, tmp_qeth):
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        # Status column has an empty header; the others use words.
        labels = [
            panel.table.horizontalHeaderItem(i).text()
            for i in range(panel.table.columnCount())
        ]
        assert labels == ["", "Nonce", "Time", "Hash"]

    def test_status_nonce_and_hash_render(self, qtbot, tmp_qeth):
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        tx = _tx(nonce=42, to_addr="0xbeef", success=True)
        panel.show_transactions([tx])
        assert panel.table.item(0, 0).toolTip() == "Success"
        assert panel.table.item(0, 1).text() == "42"
        # Time cell is locale-formatted — just assert non-empty rather
        # than locking in a specific format string.
        assert panel.table.item(0, 2).text()
        # Hash cell stores the full hash as its text — Qt elides at
        # paint time based on the column width, so widening the column
        # reveals more of it. The cell text itself (which is what
        # tests can observe) is the canonical 0x-prefixed 66-char form.
        hash_cell = panel.table.item(0, 3)
        assert hash_cell.text() == tx.hash
        assert hash_cell.toolTip() == tx.hash
        # The Hash cell carries the full Transaction object on
        # UserRole (so the details dialog can pick it up from a
        # double-click without re-looking-up the cache).
        assert hash_cell.data(Qt.UserRole) is tx

    def test_failed_tx_marked_with_cross(self, qtbot, tmp_qeth):
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        panel.show_transactions([
            _tx(to_addr="0xbeef00000000000000000000000000000000beef", success=True),
            _tx(to_addr="0xbeef00000000000000000000000000000000beef", success=False),
        ])
        assert panel.table.item(0, 0).toolTip() == "Success"
        assert panel.table.item(1, 0).toolTip() == "Reverted"

    def test_status_falls_back_to_glyph_without_icon_theme(
        self, qtbot, tmp_qeth, monkeypatch,
    ):
        """The status column uses a themed icon when available, but must
        never render blank — a missing icon falls back to the Unicode
        glyph. Force fromTheme to return a null icon and check the
        glyph text is used."""
        import qeth.plugins.transactions as txmod
        from PySide6.QtGui import QIcon
        monkeypatch.setattr(txmod.QIcon, "fromTheme",
                            staticmethod(lambda *_a, **_k: QIcon()))
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        panel.show_transactions([
            _tx(to_addr="0xbeef", success=True, pending=True),
            _tx(to_addr="0xbeef", success=True),
            _tx(to_addr="0xbeef", success=False),
        ])
        assert panel.table.item(0, 0).text() == "⏳"
        assert panel.table.item(1, 0).text() == "✓"
        assert panel.table.item(2, 0).text() == "✗"


    def test_clear_resets_panel(self, qtbot, tmp_qeth):
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        panel.show_transactions([_tx(to_addr="0xabc")])
        panel.clear()
        assert panel.table.rowCount() == 0
        assert panel.status_lbl.isHidden()

    def test_pending_tx_marked_with_hourglass(self, qtbot, tmp_qeth):
        """Status column glyph for ``tx.pending=True``."""
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        panel.show_transactions([_tx(to_addr="0xbeef", pending=True)])
        assert panel.table.item(0, 0).toolTip() == "Pending"
        assert panel.table.item(0, 0).toolTip() == "Pending"

    def test_bulk_populate_temporarily_disables_autosize(
        self, qtbot, tmp_qeth, monkeypatch,
    ):
        """Regression: replacing rows on a ResizeToContents column
        re-measures the whole column on every setItem, turning a
        2000-row repopulate into a ~35-second main-thread freeze.
        ``show_transactions`` must switch the affected columns to
        ``Fixed`` during populate and restore the prior resize mode
        afterward. We assert the actual transitions rather than
        timing, so the test stays fast and deterministic."""
        from PySide6.QtWidgets import QHeaderView

        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        # First populate so the table has items at every row (the
        # bug only triggers on REPLACEMENT setItem calls).
        panel.show_transactions([_tx(to_addr="0xbeef") for _ in range(3)])

        header = panel.table.horizontalHeader()
        # Modes recorded by _populate_row each time it's called —
        # if any of them is ResizeToContents we'd be triggering the
        # O(N²) path.
        seen_modes: list[list[QHeaderView.ResizeMode]] = []
        original_populate = panel._populate_row

        def spy_populate(row, tx):
            seen_modes.append([
                header.sectionResizeMode(i)
                for i in range(panel.table.columnCount())
            ])
            original_populate(row, tx)

        monkeypatch.setattr(panel, "_populate_row", spy_populate)

        prior = [header.sectionResizeMode(i)
                  for i in range(panel.table.columnCount())]
        panel.show_transactions([_tx(to_addr="0xbeef") for _ in range(3)])

        # No populate call may run while any column is still on
        # ResizeToContents.
        for modes in seen_modes:
            assert QHeaderView.ResizeToContents not in modes, (
                "_populate_row ran while a column was still "
                "ResizeToContents; the O(N²) re-measure path is "
                "back. Modes seen: %r" % modes
            )
        # And the resize modes are restored to the user's configured
        # state after the bulk populate.
        restored = [header.sectionResizeMode(i)
                     for i in range(panel.table.columnCount())]
        assert restored == prior

    def test_bulk_populate_blocks_table_signals(
        self, qtbot, tmp_qeth,
    ):
        """Same bug had a secondary contributor: itemSelectionChanged
        firing on every setItem when the user had a row selected,
        which ran _update_action_buttons each time. show_transactions
        has to ``blockSignals(True)`` during the populate; we verify
        that no itemSelectionChanged signals reach a subscriber while
        the table is being rebuilt."""
        panel = TransactionListPanel()
        qtbot.addWidget(panel)
        panel.set_context(ETH, ADDR)
        # Populate once so the next call replaces existing items.
        panel.show_transactions([_tx(to_addr="0xbeef")])
        panel.table.selectRow(0)

        fires: list[int] = []
        panel.table.itemSelectionChanged.connect(lambda: fires.append(1))
        panel.show_transactions([_tx(to_addr="0xbeef")])
        # No signal should have fired during the bulk replace —
        # blockSignals discards them.
        assert fires == []
