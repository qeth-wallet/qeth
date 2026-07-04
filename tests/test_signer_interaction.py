"""DialogInteraction (qeth/signer_interaction.py): the Qt SignerInteraction —
main-thread spinner + secret prompt, and the worker→main marshaling that lets a
signing worker drive UI (step 3's QR) without touching threads."""

import qeth.signer_interaction as si


def _host(qtbot, title="Signing"):
    from PySide6.QtWidgets import QWidget
    parent = QWidget()
    qtbot.addWidget(parent)
    return si.DialogInteraction(parent, title), parent


def test_progress_shows_then_close_dismisses(qtbot):
    host, _p = _host(qtbot)
    host.progress("working…")
    assert host._progress is not None
    assert host._progress.labelText() == "working…"
    host.progress("still working…")               # updates in place, no new dlg
    assert host._progress.labelText() == "still working…"
    host.close()
    assert host._progress is None


def test_request_secret_returns_entered_value(qtbot, monkeypatch):
    seen = {}

    def fake_prompt(parent, title, label, password=False):
        seen.update(title=title, label=label, password=password)
        return "hunter2", True
    monkeypatch.setattr(si, "prompt_text", fake_prompt)
    host, _p = _host(qtbot, title="Hot wallet")
    assert host.request_secret("Passphrase:", title="Hot wallet") == "hunter2"
    assert seen == {"title": "Hot wallet", "label": "Passphrase:",
                    "password": True}


def test_request_secret_cancel_returns_none(qtbot, monkeypatch):
    monkeypatch.setattr(si, "prompt_text", lambda *a, **k: ("", False))
    host, _p = _host(qtbot)
    assert host.request_secret("Passphrase:") is None


def test_call_from_worker_thread_marshals_to_main(qtbot, monkeypatch):
    """A DialogInteraction method called from a WORKER thread runs on the main
    thread (blocking the worker on a Future) and returns its result — the
    plumbing step 3's QR exchange needs."""
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    def fake_prompt(*_a, **_k):
        # record which thread the actual UI work ran on
        fake_prompt.on_main = (
            QThread.currentThread() == QApplication.instance().thread())
        return "scanned", True
    fake_prompt.on_main = None
    monkeypatch.setattr(si, "prompt_text", fake_prompt)
    host, _p = _host(qtbot)

    result = {}

    class Worker(QThread):
        def run(self):
            # NOT the main thread → must marshal via the queued signal
            result["off_main"] = (
                QThread.currentThread() != QApplication.instance().thread())
            result["value"] = host.request_secret("p")

    w = Worker()
    w.start()
    qtbot.waitUntil(lambda: "value" in result, timeout=3000)
    w.wait()

    assert result["off_main"] is True             # the call came from a worker
    assert result["value"] == "scanned"           # …and got its answer back
    assert fake_prompt.on_main is True            # UI ran on the main thread


def test_exchange_qr_not_implemented_until_step3(qtbot):
    import pytest
    host, _p = _host(qtbot)
    with pytest.raises(NotImplementedError):
        host.exchange_qr("ur:eth-sign-request/aeadcylabntfgm")
