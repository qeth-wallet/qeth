"""Trezor account import wiring: the _add_trezor flow persists trezor accounts
keyed on the wallet's root fingerprint, and the tree shows a 'Trezor' root."""

from types import SimpleNamespace

from PySide6.QtWidgets import QDialog

from qeth.ledger import DiscoveredAccount


def _plugin(qtbot, accounts):
    from qeth.plugins.wallets import WalletsPlugin
    from qeth.store import Store
    store = Store.load()
    store.accounts = list(accounts)
    plugin = WalletsPlugin(store)
    qtbot.addWidget(plugin.widget())
    plugin._rebuild_tree()
    return plugin, store


def _stub_dialog(picked, xfp):
    class _AddStub:
        def __init__(self, chain, parent=None, existing_addresses=None):
            self.scheme_combo = SimpleNamespace(currentData=lambda: "BIP44 Standard")
            self.fingerprint = xfp

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected_accounts(self):
            return list(picked)

        def discovered_accounts(self):
            return list(picked)
    return _AddStub


def _run_add(plugin, monkeypatch, picked, xfp):
    import qeth.plugins.wallets as wallets_mod
    monkeypatch.setattr(wallets_mod, "AddTrezorDialog", _stub_dialog(picked, xfp))
    plugin.host = SimpleNamespace(
        current_chain=lambda: SimpleNamespace(chain_id=1),
        status_message=lambda *a, **k: None)
    monkeypatch.setattr(plugin, "_kick_ens_label_lookups", lambda addrs: None)
    plugin._add_trezor()


def test_add_trezor_persists_accounts_under_a_trezor_tree(qtbot, tmp_qeth, monkeypatch):
    plugin, store = _plugin(qtbot, [])
    picked = [DiscoveredAccount(address="0x" + "ab" * 20,
                                path="44'/60'/0'/0/0", index=0, nonce=3)]
    _run_add(plugin, monkeypatch, picked, "0x9bd26194")

    [acct] = [a for a in store.accounts if a.get("source") == "trezor"]
    assert acct["path"] == "44'/60'/0'/0/0"
    assert acct["scheme"] == "BIP44 Standard"
    assert acct["xfp"] == "0x9bd26194"
    assert acct["tree"] == picked[0].address.lower()
    root = plugin._tree.topLevelItem(0)
    assert root.text(0) == "Trezor (1)"
    assert [root.child(i).text(0) for i in range(root.childCount())] == [
        "BIP44 Standard (m/44'/60'/0'/0/i)"]


def test_a_second_scan_of_the_same_wallet_joins_its_tree(qtbot, tmp_qeth, monkeypatch):
    first = DiscoveredAccount(address="0x" + "ab" * 20,
                              path="44'/60'/0'/0/0", index=0, nonce=3)
    plugin, store = _plugin(qtbot, [])
    _run_add(plugin, monkeypatch, [first], "0x9bd26194")
    # Same fingerprint, a new address → the same device tree, not a new one.
    second = DiscoveredAccount(address="0x" + "cd" * 20,
                               path="44'/60'/0'/0/1", index=1, nonce=0)
    _run_add(plugin, monkeypatch, [second], "0x9bd26194")

    trees = {a["tree"] for a in store.accounts if a.get("source") == "trezor"}
    assert trees == {first.address.lower()}
