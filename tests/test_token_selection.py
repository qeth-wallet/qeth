"""The Tokens tab must keep the SELECTED TOKEN selected across a refresh.

Qt tracks selection by row index, but a token's row is not stable: the table
re-sorts by Value (USD) descending, and ``show_balances`` rewrites every row
from scratch. So when a send confirms — the sent token's value drops, or a
received token appears above the selection — the untouched row index silently
lands on a *different* token. That's what the user sees as "the cursor jumped".
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

ETH = SimpleNamespace(chain_id=1, name="Ethereum", symbol="ETH")
AAA = "0x" + "aa" * 20
BBB = "0x" + "bb" * 20
CCC = "0x" + "cc" * 20
DDD = "0x" + "dd" * 20


def _tok(addr, sym, units):
    from qeth.token_discovery import TokenBalance
    return TokenBalance(contract=addr, symbol=sym, name=sym, decimals=18,
                        balance_raw=int(units * 10**18))


def _prices(*addrs):
    """$1 each, so a token's USD value is just its balance."""
    from qeth.pricing import Price
    return {a: Price(Decimal("1"), 1, "test") for a in ("", *addrs)}


def _selected(panel):
    """The (chain_id, contract) of the selected row, or None."""
    items = panel.table.selectedItems()
    if not items:
        return None
    sym = panel.table.item(items[0].row(), 0)
    return sym.data(Qt.ItemDataRole.UserRole) if sym else None


def _row_of(panel, addr):
    for r in range(panel.table.rowCount()):
        it = panel.table.item(r, 0)
        key = it.data(Qt.ItemDataRole.UserRole) if it else None
        if key and key[1] == addr:
            return r
    raise AssertionError(f"{addr} not in the table")


def _order(panel):
    return [panel.table.item(r, 0).text() for r in range(panel.table.rowCount())]


@pytest.fixture
def panel(tmp_qeth, qtbot):
    from qeth.icons import IconCache
    from qeth.plugins.tokens import TokenListPanel
    from qeth.store import Store
    p = TokenListPanel(IconCache(), Store.load())
    qtbot.addWidget(p)
    # AAA $300, BBB $200, CCC $100 -> Value-desc order AAA, BBB, CCC
    p.render_full(ETH, 10**18,
                  [_tok(AAA, "AAA", 300), _tok(BBB, "BBB", 200),
                   _tok(CCC, "CCC", 100)],
                  {}, _prices(AAA, BBB, CCC))
    return p


def test_received_token_appearing_on_top_does_not_move_the_selection(panel):
    """The reported bug. A confirmed tx brings in a new token worth more than
    everything else; the full rebuild re-sorts it to the top and every row
    below shifts. Selecting BBB and refreshing used to leave AAA selected."""
    panel.table.selectRow(_row_of(panel, BBB))
    assert _selected(panel)[1] == BBB

    panel.render_full(ETH, 10**18,
                      [_tok(AAA, "AAA", 300), _tok(BBB, "BBB", 200),
                       _tok(CCC, "CCC", 100), _tok(DDD, "DDD", 999)],
                      {}, _prices(AAA, BBB, CCC, DDD))

    assert _order(panel)[0] == "DDD", "expected the new token to sort on top"
    assert _selected(panel)[1] == BBB


def test_sending_the_selected_token_keeps_it_selected_as_it_sinks(panel):
    """The other half: the selected token is the one that was just sent, so
    its value drops and the re-sort moves it DOWN past the others."""
    panel.table.selectRow(_row_of(panel, AAA))

    panel.render_full(ETH, 10**18,
                      [_tok(AAA, "AAA", 5), _tok(BBB, "BBB", 200),
                       _tok(CCC, "CCC", 100)],
                      {}, _prices(AAA, BBB, CCC))

    assert _order(panel).index("AAA") > _order(panel).index("CCC"), \
        "expected the spent token to sink below the others"
    assert _selected(panel)[1] == AAA


def test_in_place_balance_update_keeps_the_selection(panel):
    """The contract set is unchanged, so the plugin takes the in-place path
    (update_balances_if_set_unchanged + set_prices) rather than a rebuild.
    Rows keep their identity, but re-enabling sort still reorders them."""
    panel.table.selectRow(_row_of(panel, CCC))

    assert panel.update_balances_if_set_unchanged(
        ETH, 10**18,
        [_tok(AAA, "AAA", 1), _tok(BBB, "BBB", 200), _tok(CCC, "CCC", 100)],
    )
    panel.set_prices(ETH.chain_id, _prices(AAA, BBB, CCC), force=True)

    assert _selected(panel)[1] == CCC


def test_reprice_that_reorders_keeps_the_selection(panel):
    """A price refresh alone can reorder the table."""
    from qeth.pricing import Price
    panel.table.selectRow(_row_of(panel, CCC))

    # CCC's unit price jumps 10x -> $1000, sorting it to the top.
    prices = _prices(AAA, BBB)
    prices[CCC] = Price(Decimal("10"), 2, "test")
    panel.set_prices(ETH.chain_id, prices)

    assert _order(panel)[0] == "CCC"
    assert _selected(panel)[1] == CCC


def test_selection_is_cleared_when_the_selected_token_disappears(panel):
    """Spent to zero / hidden / dust-filtered: there is nothing to re-select.
    Clearing is the point — leaving the index alone is exactly how it ended
    up pointing at an unrelated token."""
    panel.table.selectRow(_row_of(panel, BBB))

    panel.render_full(ETH, 10**18,
                      [_tok(AAA, "AAA", 300), _tok(CCC, "CCC", 100)],
                      {}, _prices(AAA, CCC))

    assert _selected(panel) is None
    assert panel.table.currentRow() == -1


def test_a_refresh_does_not_invent_a_selection(panel):
    """Nothing selected must stay nothing selected."""
    panel.table.clearSelection()

    panel.render_full(ETH, 10**18,
                      [_tok(AAA, "AAA", 300), _tok(BBB, "BBB", 200),
                       _tok(CCC, "CCC", 100), _tok(DDD, "DDD", 999)],
                      {}, _prices(AAA, BBB, CCC, DDD))

    assert _selected(panel) is None


def test_the_native_row_survives_a_refresh_too(panel):
    """Send works on the native row as well, so it must anchor like any other."""
    panel.table.selectRow(_row_of(panel, panel.NATIVE_CONTRACT))
    assert _selected(panel)[1] == panel.NATIVE_CONTRACT

    panel.render_full(ETH, 500 * 10**18,
                      [_tok(AAA, "AAA", 300), _tok(BBB, "BBB", 200),
                       _tok(CCC, "CCC", 100)],
                      {}, _prices(AAA, BBB, CCC))

    assert _selected(panel)[1] == panel.NATIVE_CONTRACT


def test_switching_chain_clears_rather_than_reselects(panel):
    """The identity key carries the chain id, so a token selected on
    Ethereum can't be re-anchored onto a different chain's row."""
    op = SimpleNamespace(chain_id=10, name="Optimism", symbol="ETH")
    panel.table.selectRow(_row_of(panel, AAA))

    panel.render_full(op, 10**18, [_tok(AAA, "AAA", 300)], {}, _prices(AAA))

    assert _selected(panel) is None
