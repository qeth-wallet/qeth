"""Chain families (EVM / Tron): account families, the dapp chain, plugin
availability and the family-filtered account tree."""

import asyncio

import pytest

from qeth.address import tron_from_hex
from qeth.chains import DEFAULT_CHAINS, EVM, TRON, Chain
from qeth.store import Store, account_families

TRON_CHAIN = Chain("Tron", 728126428, "https://api.trongrid.io/jsonrpc", "TRX",
                   family=TRON, native_decimals=6)

HOT = "0x" + "11" * 20
EVM_WATCH = "0x" + "22" * 20
TRON_WATCH = "0x" + "33" * 20


def _accounts():
    return [
        {"address": HOT, "source": "hot", "label": ""},
        {"address": EVM_WATCH, "source": "watch_only", "label": ""},
        {"address": TRON_WATCH, "source": "watch_only", "label": "",
         "family": TRON},
    ]


class TestAccountFamilies:
    def test_hot_serves_both_families_others_their_own(self):
        hot, evm_watch, tron_watch = _accounts()
        assert account_families(hot) == {EVM, TRON}
        assert account_families(evm_watch) == {EVM}
        assert account_families(tron_watch) == {TRON}

    def test_accounts_for(self, tmp_qeth):
        s = Store()
        s.accounts = _accounts()
        assert [a["address"] for a in s.accounts_for(TRON)] == [HOT, TRON_WATCH]
        assert [a["address"] for a in s.accounts_for(EVM)] == [HOT, EVM_WATCH]

    def test_a_tron_only_account_never_becomes_the_dapp_default(self, tmp_qeth):
        s = Store()
        s.add_account(_accounts()[2])
        assert s.default_account is None
        s.add_account(_accounts()[1])
        assert s.default_account == EVM_WATCH

    def test_removing_the_default_repoints_to_an_evm_account(self, tmp_qeth):
        s = Store()
        s.accounts = [_accounts()[2], _accounts()[1]]
        s.default_account = TRON_WATCH     # stale / hand-edited
        s.add_account(_accounts()[0])
        s.default_account = HOT
        s.remove_account(HOT)
        assert s.default_account == EVM_WATCH


class TestDappChain:
    def _store(self):
        s = Store()
        s.chains = [*DEFAULT_CHAINS, TRON_CHAIN]
        return s

    def test_follows_the_ui_chain_while_it_is_evm(self, tmp_qeth):
        s = self._store()
        s.set_current_chain(10)
        assert s.dapp_chain().chain_id == 10

    def test_stays_on_the_last_evm_chain_while_on_tron(self, tmp_qeth):
        s = self._store()
        s.set_current_chain(137)
        s.set_current_chain(TRON_CHAIN.chain_id)
        assert s.current_chain().family == TRON
        assert s.dapp_chain().chain_id == 137

    def test_dapp_chain_persists(self, tmp_qeth):
        s = self._store()
        s.set_current_chain(8453)
        s.set_current_chain(TRON_CHAIN.chain_id)
        again = Store.load()
        again.chains.append(TRON_CHAIN)   # custom chains persist as-is anyway
        assert again.dapp_chain().chain_id == 8453


class TestRpcIsEvmOnly:
    def _server(self, tmp_qeth):
        from qeth.rpc import RpcServer
        s = Store()
        s.chains = [*DEFAULT_CHAINS, TRON_CHAIN]
        s.set_current_chain(10)
        s.set_current_chain(TRON_CHAIN.chain_id)
        return RpcServer(s)

    def test_tron_is_not_listed(self, tmp_qeth):
        srv = self._server(tmp_qeth)
        ids = {int(c["chainId"]) for c in srv._ethereum_chains()}
        assert TRON_CHAIN.chain_id not in ids and 10 in ids

    def test_dapps_see_the_last_evm_chain(self, tmp_qeth):
        srv = self._server(tmp_qeth)
        assert srv._chain_for_origin("https://app.example") == 10

    def test_switching_to_tron_is_refused(self, tmp_qeth):
        from qeth.rpc import RpcError
        srv = self._server(tmp_qeth)
        with pytest.raises(RpcError) as exc:
            asyncio.run(srv._dispatch(
                "wallet_switchEthereumChain",
                [{"chainId": hex(TRON_CHAIN.chain_id)}], None))
        assert exc.value.code == 4902


class TestSlotAvailability:
    def _slot(self, qtbot):
        from unittest.mock import MagicMock
        from PySide6.QtWidgets import QWidget
        from qeth.plugin import Plugin, Slot

        class P(Plugin):
            def __init__(self, name):
                super().__init__()
                self.name = name
                self._w = QWidget()
                self.accounts: list = []
                self.chains = 0

            def widget(self):
                return self._w

            def on_account_changed(self, address):
                self.accounts.append(address)

            def on_chain_changed(self):
                self.chains += 1

        slot = Slot()
        qtbot.addWidget(slot)
        a, b = P("A"), P("B")
        host = MagicMock()
        slot.add_plugin(a, host)
        slot.add_plugin(b, host)
        return slot, a, b

    def test_hidden_plugin_gets_no_broadcasts_and_is_resynced(self, qtbot):
        slot, a, b = self._slot(qtbot)
        slot.set_plugin_available(b, False)
        slot.broadcast_account_changed("0xabc")
        slot.broadcast_chain_changed()
        assert b.accounts == [] and b.chains == 0
        assert a.accounts == ["0xabc"] and a.chains == 1
        slot.set_plugin_available(b, True, "0xabc")
        assert b.accounts == ["0xabc"] and b.chains == 1
        assert slot.available_plugins() == [a, b]

    def test_hiding_the_active_plugin_switches_away(self, qtbot):
        slot, a, b = self._slot(qtbot)
        slot.set_active(b)
        assert slot.active() is b
        slot.set_plugin_available(b, False)
        assert slot.active() is a
        slot.set_active(b)                   # refused while hidden
        assert slot.active() is a
        assert slot.available_plugins() == [a]

    def test_the_tab_bar_regrows_when_hidden_tabs_come_back(self, qtbot):
        """Started with tabs hidden (qeth launched on Tron), the tab row laid
        the bar out narrow; showing them again must widen it — QTabBar doesn't
        signal its new size hint on its own, and the returning tabs ended up
        squeezed behind scroll arrows."""
        from unittest.mock import MagicMock
        from PySide6.QtWidgets import QApplication, QWidget
        from qeth.plugin import Plugin, Slot

        class P(Plugin):
            def __init__(self, name):
                super().__init__()
                self.name = name
                self._w = QWidget()

            def widget(self):
                return self._w

        slot = Slot()
        qtbot.addWidget(slot)
        tabs = [P(n) for n in ("Tokens", "Transactions", "ENS", "Approvals")]
        for t in tabs:
            slot.add_plugin(t, MagicMock())
        for t in tabs[2:]:
            slot.set_plugin_available(t, False)
        slot.resize(1000, 300)
        slot.show()
        qtbot.waitExposed(slot)
        bar = slot._tab_bar
        narrow = bar.width()
        for t in tabs[2:]:
            slot.set_plugin_available(t, True, "0xabc")
        QApplication.processEvents()          # the layout request
        assert bar.width() >= bar.sizeHint().width() > narrow


class TestMainWindowOnTron:
    @pytest.fixture
    def win(self, qtbot, tmp_qeth, fake_rpc, hermetic_mainwindow):
        from qeth.ui import MainWindow
        store = Store.load()
        store.chains.append(TRON_CHAIN)
        store.accounts = _accounts()
        store.default_account = HOT
        w = MainWindow(store, fake_rpc)
        qtbot.addWidget(w)
        return w

    def _switch(self, win, chain_id):
        idx = win.chain_combo.findData(chain_id)
        assert idx >= 0
        win.chain_combo.setCurrentIndex(idx)

    def _tree_rows(self, win):
        from PySide6.QtCore import Qt
        out = []
        tree = win.tree
        it = tree.topLevelItem(0)
        while it is not None:
            addr = it.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(addr, str):
                out.append((addr, it.text(0).strip(" []")))
            it = tree.itemBelow(it)
        return out

    def test_tree_lists_the_family_in_its_form(self, win):
        assert {a for a, _ in self._tree_rows(win)} == {HOT, EVM_WATCH}
        self._switch(win, TRON_CHAIN.chain_id)
        rows = dict(self._tree_rows(win))
        assert set(rows) == {HOT, TRON_WATCH}
        assert rows[HOT] == tron_from_hex(HOT)
        self._switch(win, 1)
        assert {a for a, _ in self._tree_rows(win)} == {HOT, EVM_WATCH}

    def test_selection_moves_off_an_account_the_family_hides(self, win):
        win.wallets_plugin.select_address(EVM_WATCH)
        self._switch(win, TRON_CHAIN.chain_id)
        assert win.wallets_plugin.selected_address in (HOT, TRON_WATCH)

    def test_evm_only_tabs_hide_on_tron(self, win):
        ens = win.plugins.get("ens")
        approvals = win.plugins.get("approvals")
        self._switch(win, TRON_CHAIN.chain_id)
        for p in (ens, approvals):
            if p is not None:
                assert not win.right_slot.is_plugin_available(p)
        assert win.right_slot.is_plugin_available(win.plugins["transactions"])
        self._switch(win, 1)
        for p in (ens, approvals):
            if p is not None:
                assert win.right_slot.is_plugin_available(p)

    def test_dapps_are_not_moved_to_tron(self, win, fake_rpc, monkeypatch):
        pushed = []
        monkeypatch.setattr(fake_rpc, "set_rpc_chain", pushed.append,
                            raising=False)
        self._switch(win, TRON_CHAIN.chain_id)
        assert pushed == []
        self._switch(win, 10)
        assert pushed == [10]


class TestAddedAccountsAreRevealed:
    """An account added from a view of another family must not vanish: the
    wallet switches to a network it works on and selects it (the report:
    a Tron watch-only address added from Ethereum was 'nowhere to be seen')."""

    @pytest.fixture
    def win(self, qtbot, tmp_qeth, fake_rpc, hermetic_mainwindow):
        from qeth.ui import MainWindow
        store = Store.load()
        store.chains.append(TRON_CHAIN)
        store.accounts = [_accounts()[1]]            # one EVM watch-only
        store.set_current_chain(10)                  # viewing Optimism
        w = MainWindow(store, fake_rpc)
        qtbot.addWidget(w)
        return w

    def _add_watch(self, win, monkeypatch, account):
        import qeth.plugins.wallets as wallets_mod
        from PySide6.QtWidgets import QDialog

        class FakeDialog:
            def __init__(self, *a, **k):
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            def result_account(self):
                return dict(account)
        monkeypatch.setattr(wallets_mod, "AddWatchOnlyDialog", FakeDialog)
        win.wallets_plugin._add_watch_only()

    def test_tron_address_added_on_an_evm_view(self, win, monkeypatch):
        self._add_watch(win, monkeypatch, _accounts()[2])
        assert win.store.current_chain().chain_id == TRON_CHAIN.chain_id
        assert win.wallets_plugin.selected_address == TRON_WATCH

    def test_evm_address_added_on_tron_goes_to_the_last_evm_network(
            self, win, monkeypatch):
        win.chain_combo.setCurrentIndex(win.chain_combo.findData(TRON_CHAIN.chain_id))
        new = {"address": "0x" + "44" * 20, "source": "watch_only", "label": ""}
        self._add_watch(win, monkeypatch, new)
        assert win.store.current_chain().chain_id == 10    # back to Optimism
        assert win.wallets_plugin.selected_address == new["address"]

    def test_no_switch_when_it_already_shows(self, win, monkeypatch):
        new = {"address": "0x" + "55" * 20, "source": "watch_only", "label": ""}
        self._add_watch(win, monkeypatch, new)
        assert win.store.current_chain().chain_id == 10
        assert win.wallets_plugin.selected_address == new["address"]


class TestConnectedAccountPerFamily:
    """Each family has its own connected ("default for dapps") account. A
    double-click on a Tron view connects the Tron account and must NOT
    touch the EVM one — that is eth_accounts, and a Tron-only account there
    would hand EVM dapps an address they can't use (the report: double-click
    on Tron 'did not make it default', silently changing the EVM one)."""

    TREZOR_TRON = "0x" + "66" * 20

    @pytest.fixture
    def win(self, qtbot, tmp_qeth, fake_rpc, hermetic_mainwindow):
        from qeth.ui import MainWindow
        store = Store.load()
        store.chains.append(TRON_CHAIN)
        store.accounts = [*_accounts(),
                          {"address": self.TREZOR_TRON, "source": "trezor",
                           "path": "44'/195'/0'/0/0", "label": "", "family": TRON}]
        store.default_account = EVM_WATCH
        w = MainWindow(store, fake_rpc)
        qtbot.addWidget(w)
        idx = w.chain_combo.findData(TRON_CHAIN.chain_id)
        w.chain_combo.setCurrentIndex(idx)
        return w

    def _item(self, win, addr):
        from PySide6.QtCore import Qt
        it = win.tree.topLevelItem(0)
        while it is not None:
            if it.data(0, Qt.ItemDataRole.UserRole) == addr:
                return it
            it = win.tree.itemBelow(it)
        raise AssertionError(addr)

    def test_double_click_on_tron_connects_the_tron_account(self, win):
        wp = win.wallets_plugin
        wp._on_tree_double_clicked(self._item(win, self.TREZOR_TRON), 0)
        assert win.store.default_for(TRON)[0] == self.TREZOR_TRON
        assert win.store.default_account == EVM_WATCH        # eth_accounts untouched
        assert self._item(win, self.TREZOR_TRON).text(0).startswith("[")

    def test_the_evm_view_keeps_its_own_marker(self, win):
        win.wallets_plugin._on_tree_double_clicked(self._item(win, HOT), 0)
        assert win.store.default_for(TRON)[0] == HOT
        win.chain_combo.setCurrentIndex(win.chain_combo.findData(1))
        assert self._item(win, EVM_WATCH).text(0).startswith("[")
        assert not self._item(win, HOT).text(0).startswith("[")

    def test_a_tron_connect_does_not_ping_evm_dapps(self, win, fake_rpc, monkeypatch):
        pushed = []
        monkeypatch.setattr(fake_rpc, "broadcast_accounts_changed", pushed.append)
        win._pushed_accounts = [EVM_WATCH]
        win.wallets_plugin._on_tree_double_clicked(self._item(win, self.TREZOR_TRON), 0)
        assert pushed == []


def test_family_defaults_persist_and_repoint(tmp_qeth):
    s = Store()
    s.chains = [*DEFAULT_CHAINS, TRON_CHAIN]
    for a in _accounts():
        s.add_account(a)
    # The first account of each family became its default.
    assert s.default_for(EVM)[0] == HOT and s.default_for(TRON)[0] == HOT
    s.set_default_account(TRON_WATCH, None, family=TRON)
    again = Store.load()
    assert again.default_for(TRON)[0] == TRON_WATCH
    assert again.default_account == HOT
    again.remove_account(TRON_WATCH)
    assert again.default_for(TRON)[0] == HOT     # repointed within the family
    assert again.account_for_signing(HOT, family=TRON)["source"] == "hot"
