"""Tests for the live URL-reachability verdict in ChainRpcDialog — the
red/green line that probes a typed/pasted RPC before the user commits it."""

from types import SimpleNamespace

import qeth.chainlist as cl
from qeth.chain_rpc_dialog import ChainRpcDialog
from qeth.chains import Chain


def _dialog(qtbot, monkeypatch, rpc_url=""):
    # Keep construction hermetic: the chainlist loader and the URL probe both
    # go through qeth.chainlist, so stub it. Empty rpc_url → no probe on open.
    monkeypatch.setattr(cl, "lookup", lambda cid: None)
    monkeypatch.setattr(cl, "probe_rpc", lambda *a, **k: (True, 10.0, None))
    chain = SimpleNamespace(name="Ethereum", chain_id=1, rpc_url=rpc_url)
    dlg = ChainRpcDialog(chain)
    qtbot.addWidget(dlg)
    return dlg


def test_reachable_shows_green_with_latency(qtbot, monkeypatch):
    dlg = _dialog(qtbot, monkeypatch)
    dlg._show_url_verdict("https://x", True, 42.0, None)
    assert dlg.url_status_lbl.text() == "✓ Reachable · 42 ms"
    assert not dlg.url_status_lbl.isHidden()
    ss = dlg.url_status_lbl.styleSheet()
    assert "#d1e7dd" in ss and "border-radius" in ss     # green tinted pill


def test_wrong_chain_is_called_out(qtbot, monkeypatch):
    """A working node for a DIFFERENT chain (the classic paste typo) is flagged
    specifically, not as merely 'unreachable'."""
    dlg = _dialog(qtbot, monkeypatch)
    dlg._show_url_verdict("https://x", False, 40.0, "chain mismatch (137)")
    assert "Wrong chain" in dlg.url_status_lbl.text()
    assert "chain 1" in dlg.url_status_lbl.text()
    ss = dlg.url_status_lbl.styleSheet()
    assert "#f8d7da" in ss and "border-radius" in ss     # red tinted pill


def test_unreachable_shows_red(qtbot, monkeypatch):
    dlg = _dialog(qtbot, monkeypatch)
    dlg._show_url_verdict("https://x", False, None, "timed out")
    assert dlg.url_status_lbl.text().startswith("✗ Not reachable")
    assert "#f8d7da" in dlg.url_status_lbl.styleSheet()   # red tinted pill


def test_non_http_hints_and_does_not_probe(qtbot, monkeypatch):
    dlg = _dialog(qtbot, monkeypatch)
    dlg.url_edit.setText("wss://eth.example")
    dlg._kick_url_probe()
    assert "http" in dlg.url_status_lbl.text().lower()
    assert not dlg._url_workers        # no network probe for a non-http URL


def test_cached_picker_result_shows_without_reprobing(qtbot, monkeypatch):
    """Picking an endpoint the picker already probed green shows the verdict
    instantly instead of firing another probe."""
    dlg = _dialog(qtbot, monkeypatch)
    dlg._results["https://cached.example"] = (7.0, True, True)  # (latency, ok, simv1)
    dlg.url_edit.setText("https://cached.example")
    dlg._kick_url_probe()
    assert dlg.url_status_lbl.text() == "✓ Reachable · 7 ms"
    assert not dlg._url_workers        # served from cache, no worker


def test_stale_verdict_for_old_url_is_ignored(qtbot, monkeypatch):
    """A probe that lands after the user kept typing must not paint its (now
    stale) verdict over the current URL."""
    dlg = _dialog(qtbot, monkeypatch)
    dlg.url_edit.setText("https://current.example")
    dlg._set_url_status("", "none")
    dlg._on_url_probed("https://old.example", True, 5.0, None)   # stale URL
    assert dlg.url_status_lbl.text() == ""      # ignored — no verdict painted


def test_empty_url_hides_the_verdict(qtbot, monkeypatch):
    dlg = _dialog(qtbot, monkeypatch)
    dlg._show_url_verdict("https://x", True, 1.0, None)          # show something
    assert not dlg.url_status_lbl.isHidden()
    dlg.url_edit.setText("")
    dlg._kick_url_probe()
    assert dlg.url_status_lbl.isHidden()        # empty → collapsed, no stray gap


def _multi_chain_dialog(qtbot, monkeypatch):
    from qeth.chain_rpc_dialog import _ChainlistLoader, _UrlProbeWorker

    # Leave workers alive but idle so tests can deliver results in any order.
    monkeypatch.setattr(_ChainlistLoader, "start", lambda self: None)
    monkeypatch.setattr(_UrlProbeWorker, "start", lambda self: None)
    chains = [Chain("Ethereum", 1, "https://eth.example"),
              Chain("Base", 8453, "https://base.example")]
    dlg = ChainRpcDialog(chains[1], chains=chains, etherscan_api_key="global-key")
    qtbot.addWidget(dlg)
    return dlg, chains


def test_switching_chains_preserves_drafts_and_global_key(qtbot, monkeypatch):
    dlg, chains = _multi_chain_dialog(qtbot, monkeypatch)
    assert dlg.chain_combo.currentData() == 8453
    dlg.url_edit.setText(" https://new-base.example ")
    dlg.chain_combo.setCurrentIndex(0)
    assert dlg.url_edit.text() == "https://eth.example"
    dlg.url_edit.setText("https://new-eth.example")
    dlg.chain_combo.setCurrentIndex(1)
    assert dlg.url_edit.text() == "https://new-base.example"
    assert dlg.etherscan_api_key == "global-key"
    assert dlg.rpc_urls == {1: "https://new-eth.example",
                            8453: "https://new-base.example"}
    assert [chain.rpc_url for chain in chains] == ["https://eth.example",
                                                 "https://base.example"]
    dlg.reject()


def test_switching_resets_picker_and_ignores_previous_chain_results(qtbot, monkeypatch):
    from PySide6.QtCore import Qt

    dlg, _ = _multi_chain_dialog(qtbot, monkeypatch)
    previous = dlg._loader
    entry = SimpleNamespace(name="Base", chain_id=8453,
                            rpcs=[SimpleNamespace(url="https://base-public.example")])
    previous.loaded.emit(entry)
    previous.probed.emit("https://base-public.example", True, 5.0, True)
    previous.probing_done.emit()
    assert dlg.picker.item(0).text().endswith("https://base-public.example")
    # Queue results before switching, as a background worker can do. Even
    # disconnected signals may still deliver events that were already queued.
    previous.loaded.disconnect()
    previous.probed.disconnect()
    previous.probing_done.disconnect()
    previous.failed.disconnect()
    previous.loaded.connect(dlg._on_chainlist_loaded, Qt.ConnectionType.QueuedConnection)
    previous.probed.connect(dlg._on_probed, Qt.ConnectionType.QueuedConnection)
    previous.probing_done.connect(dlg._on_probing_done, Qt.ConnectionType.QueuedConnection)
    previous.failed.connect(dlg._on_chainlist_failed, Qt.ConnectionType.QueuedConnection)
    previous.loaded.emit(entry)
    previous.probed.emit("https://base-public.example", True, 1.0, True)
    previous.probing_done.emit()
    previous.failed.emit("old-chain failure")
    dlg.chain_combo.setCurrentIndex(0)
    qtbot.wait(1)
    assert dlg.picker.count() == 0
    assert dlg.url_status_lbl.text() == "Checking…"
    assert dlg.status_lbl.text() == "Loading from chainlist.org…"
    # Old workers must not populate, sort or overwrite the new chain's picker.
    previous.loaded.emit(entry)
    previous.probed.emit("https://base-public.example", True, 1.0, True)
    previous.probing_done.emit()
    previous.failed.emit("old-chain failure")
    assert dlg.picker.count() == 0
    assert dlg.status_lbl.text() == "Loading from chainlist.org…"
    dlg._loader.loaded.emit(SimpleNamespace(name="Ethereum", chain_id=1,
                                           rpcs=[SimpleNamespace(url="https://eth-public.example")]))
    dlg._loader.probed.emit("https://eth-public.example", True, 8.0, False)
    dlg._loader.probing_done.emit()
    assert dlg.picker.item(0).text().endswith("https://eth-public.example")
    dlg.reject()


def test_previous_chain_url_verdict_is_ignored_even_for_same_url(qtbot, monkeypatch):
    dlg, _ = _multi_chain_dialog(qtbot, monkeypatch)
    previous = next(iter(dlg._url_workers))
    dlg.chain_combo.setCurrentIndex(0)
    dlg.url_edit.setText("https://base.example")
    dlg._kick_url_probe()
    previous.probed.emit("https://base.example", True, 1.0, None)
    assert dlg.url_status_lbl.text() == "Checking…"
    current = next(w for w in dlg._url_workers if w._chain_id == 1
                   and w._url == "https://base.example")
    current.probed.emit("https://base.example", False, 1.0, "chain mismatch (8453)")
    assert "Wrong chain" in dlg.url_status_lbl.text()
    assert "chain 1" in dlg.url_status_lbl.text()
    dlg.reject()


def test_mainwindow_saves_multiple_chains_without_changing_active_chain(mainwindow, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from qeth.chain_rpc_dialog import _ChainlistLoader, _UrlProbeWorker

    monkeypatch.setattr(_ChainlistLoader, "start", lambda self: None)
    monkeypatch.setattr(_UrlProbeWorker, "start", lambda self: None)
    monkeypatch.setattr("qeth.helios.prewarm", lambda chain: None)
    broadcasts = []
    monkeypatch.setattr(mainwindow.rpc, "broadcast_chains_changed",
                        lambda: broadcasts.append("chains"), raising=False)
    chains = mainwindow.store.chains[:2]
    active_id = mainwindow.store.current_chain_id

    def edit(dlg):
        for chain in chains:
            dlg.chain_combo.setCurrentIndex(dlg.chain_combo.findData(chain.chain_id))
            dlg.url_edit.setText(f"https://new-{chain.chain_id}.example")
        dlg.etherscan_edit.setText("new-key")
        dlg.accept()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ChainRpcDialog, "exec", edit)
    mainwindow.show_settings()
    assert [chain.rpc_url for chain in chains] == [f"https://new-{chain.chain_id}.example"
                                                 for chain in chains]
    assert mainwindow.store.current_chain_id == active_id
    assert mainwindow.chain_combo.currentData() == active_id
    assert mainwindow.store.etherscan_api_key == "new-key"
    assert broadcasts == ["chains"]
    from qeth.store import Store
    reloaded = Store.load()
    assert [chain.rpc_url for chain in reloaded.chains[:2]] == [chain.rpc_url for chain in chains]


def test_mainwindow_cancel_discards_all_chain_edits(mainwindow, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from qeth.chain_rpc_dialog import _ChainlistLoader, _UrlProbeWorker

    monkeypatch.setattr(_ChainlistLoader, "start", lambda self: None)
    monkeypatch.setattr(_UrlProbeWorker, "start", lambda self: None)
    before = [chain.rpc_url for chain in mainwindow.store.chains]
    key_before = mainwindow.store.etherscan_api_key

    def edit(dlg):
        dlg.url_edit.setText("https://discarded-first.example")
        dlg.chain_combo.setCurrentIndex(1)
        dlg.url_edit.setText("https://discarded-second.example")
        dlg.etherscan_edit.setText("discarded-key")
        dlg.reject()
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ChainRpcDialog, "exec", edit)
    mainwindow.show_settings()
    assert [chain.rpc_url for chain in mainwindow.store.chains] == before
    assert mainwindow.store.etherscan_api_key == key_before
