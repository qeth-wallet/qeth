"""Tests for the live URL-reachability verdict in ChainRpcDialog — the
red/green line that probes a typed/pasted RPC before the user commits it."""

from types import SimpleNamespace

import qeth.chainlist as cl
from qeth.chain_rpc_dialog import ChainRpcDialog


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
