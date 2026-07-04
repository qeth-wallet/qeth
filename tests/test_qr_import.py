"""QR account import wiring (step 3d-2): the _add_qr flow persists a qr account,
and the tree shows an 'Air-gapped' root for it."""

from types import SimpleNamespace

from PySide6.QtWidgets import QDialog

from qeth.ledger import DiscoveredAccount
from qeth.qr.account import AccountKey


def _plugin(qtbot, accounts):
    from qeth.plugins.wallets import WalletsPlugin
    from qeth.store import Store
    store = Store.load()
    store.accounts = list(accounts)
    plugin = WalletsPlugin(store)
    qtbot.addWidget(plugin.widget())
    plugin._rebuild_tree()
    return plugin, store


def _roots(plugin):
    tree = plugin._tree
    return [tree.topLevelItem(i).text(0).split(" (")[0]
            for i in range(tree.topLevelItemCount())]


def test_qr_account_shows_air_gapped_root_with_full_path_subgroup(qtbot, tmp_qeth):
    plugin, _ = _plugin(qtbot, [
        {"address": "0x" + "11" * 20, "source": "qr",
         "path": "m/44'/60'/0'/0/0", "scheme": "BIP44 (…/0/i)",
         "xfp": "0x12345678", "label": ""},
    ])
    assert _roots(plugin) == ["Air-gapped"]
    # Grouped by scheme, labelled with the FULL derivation path (… expanded).
    root = plugin._tree.topLevelItem(0)
    assert [root.child(i).text(0) for i in range(root.childCount())] == [
        "BIP44 (m/44'/60'/0'/0/i)"]


def test_ledger_scheme_labels_show_full_paths():
    from qeth.plugins.wallets import _ledger_scheme_label, _scheme_label
    assert _ledger_scheme_label("Legacy") == "Legacy (m/44'/60'/0'/i)"
    assert _ledger_scheme_label("Ledger Live") == "Ledger Live (m/44'/60'/i'/0/0)"
    assert _ledger_scheme_label("BIP44 Standard") == "BIP44 Standard (m/44'/60'/0'/0/i)"
    assert _ledger_scheme_label("Weird") == "Weird"          # unknown → unchanged
    # …and through _scheme_label on a stored ledger account record.
    assert _scheme_label({"scheme": "Legacy", "path": "m/44'/60'/0'/7"}) \
        == "Legacy (m/44'/60'/0'/i)"


def test_add_qr_scans_derives_and_persists(qtbot, tmp_qeth, monkeypatch):
    import qeth.qr.account as account_mod
    import qeth.qr_exchange_dialog as exdlg
    import qeth.plugins.wallets as wallets_mod

    plugin, store = _plugin(qtbot, [])

    class _ScanStub:
        def __init__(self, **kw):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def scanned_ur(self):
            return "ur:crypto-hdkey/stub"
    monkeypatch.setattr(exdlg, "QRScanDialog", _ScanStub)
    monkeypatch.setattr(account_mod, "parse_account_export", lambda ur: AccountKey(
        pubkey=b"", chain_code=b"", origin_path=[44, True, 60, True, 0, True],
        source_fingerprint=0x12345678, parent_fingerprint=0))

    picked = DiscoveredAccount(
        address="0x" + "ab" * 20, path="m/44'/60'/0'/0/0", index=0, nonce=3)

    class _AddStub:
        def __init__(self, key, chain, parent=None):
            self.scheme_combo = SimpleNamespace(
                currentData=lambda: "BIP44 (…/0/i)")   # the key, via item data

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selected_accounts(self):
            return [picked]
    monkeypatch.setattr(wallets_mod, "AddQRWalletDialog", _AddStub)

    plugin.host = SimpleNamespace(
        current_chain=lambda: SimpleNamespace(chain_id=1),
        status_message=lambda *a, **k: None)
    monkeypatch.setattr(plugin, "_kick_ens_label_lookups", lambda addrs: None)

    plugin._add_qr()

    qr_accts = [a for a in store.accounts if a.get("source") == "qr"]
    assert len(qr_accts) == 1
    acct = qr_accts[0]
    assert acct["address"] == picked.address
    assert acct["path"] == "m/44'/60'/0'/0/0"
    assert acct["scheme"] == "BIP44 (…/0/i)"
    assert acct["xfp"] == "0x12345678"      # master fingerprint, hex
