"""Signer plugin REGISTRY + interaction (docs/signers.md steps 1–2): the
``source`` → ``SignerPlugin`` dispatch, and the plugins driving a
``SignerInteraction`` host for any up-front unlock."""

from types import SimpleNamespace

import pytest

from qeth.signers import REGISTRY, signer_for_source
from qeth.signers.base import SignerPlugin

# The signer constructors only stash the store ref (no work at __init__), so a
# bare object stands in.
STORE = SimpleNamespace(accounts=[])
ADDR = "0x" + "ab" * 20


class FakeInteraction:
    """A SignerInteraction stand-in: records prompts, returns a canned secret
    (or None to simulate a cancel)."""

    def __init__(self, secret="sekret"):
        self._secret = secret
        self.secret_calls = []
        self.progress_calls = []

    def progress(self, text):
        self.progress_calls.append(text)

    def request_secret(self, prompt, *, title=""):
        self.secret_calls.append((prompt, title))
        return self._secret

    def exchange_qr(self, payload):
        raise AssertionError("not used in step 2")


def _acct(source):
    return {"address": ADDR, "source": source, "label": ""}


def test_registry_has_the_three_known_sources():
    assert set(REGISTRY) == {"ledger", "hot", "watch_only"}
    assert all(isinstance(p, SignerPlugin) for p in REGISTRY.values())
    assert all(sid == p.source_id for sid, p in REGISTRY.items())


def test_unknown_or_missing_source_has_no_plugin():
    assert signer_for_source(None) is None
    assert signer_for_source("brownie") is None
    assert signer_for_source("") is None


def test_ledger_plugin_builds_a_ledger_signer():
    from qeth.ledger import LedgerSigner
    p = signer_for_source("ledger")
    assert p is not None
    assert p.can_sign() is True
    assert p.display_name == "Ledger"
    assert p.progress_text                          # a spinner label
    ui = FakeInteraction()
    signer = p.make_signer(STORE, _acct("ledger"), ui)
    assert isinstance(signer, LedgerSigner)
    assert ui.secret_calls == []                    # no up-front secret


def test_hot_plugin_prompts_then_builds_a_hot_signer():
    from qeth.hot_wallet import HotWalletSigner
    p = signer_for_source("hot")
    assert p is not None
    assert p.can_sign() is True and p.progress_text
    ui = FakeInteraction(secret="pw")
    signer = p.make_signer(STORE, _acct("hot"), ui)
    assert isinstance(signer, HotWalletSigner)
    assert ui.secret_calls == [(f"Passphrase for {ADDR}:", "Hot wallet")]


def test_hot_plugin_returns_none_when_unlock_cancelled():
    p = signer_for_source("hot")
    ui = FakeInteraction(secret=None)               # user cancelled the prompt
    assert p.make_signer(STORE, _acct("hot"), ui) is None


def test_watch_only_is_registered_but_cannot_sign():
    from qeth.signing import SignerError
    p = signer_for_source("watch_only")
    assert p is not None
    assert p.can_sign() is False
    with pytest.raises(SignerError):
        p.make_signer(STORE, _acct("watch_only"), FakeInteraction())


class TestDispatchThroughMainWindow:
    """ui.py's ``_pick_signer_for`` drives the registry + interaction host:
    routes by ``source``, prompts for the hot passphrase via the host, warns
    (no signer) for watch-only."""

    def _add(self, win, source, **extra):
        addr = "0x" + {"ledger": "11", "hot": "22",
                       "watch_only": "33"}[source] * 20
        win.store.accounts.append(
            {"address": addr, "source": source, "label": "", **extra})
        return addr

    def test_ledger_source_returns_a_ledger_signer(self, mainwindow):
        from qeth.ledger import LedgerSigner
        addr = self._add(mainwindow, "ledger", scheme="Ledger Live",
                         path="m/44'/60'/0'/0/0")
        signer, progress = mainwindow._pick_signer_for(
            mainwindow, addr, FakeInteraction())
        assert isinstance(signer, LedgerSigner)
        assert progress

    def test_hot_source_prompts_then_returns_a_hot_signer(self, mainwindow):
        from qeth.hot_wallet import HotWalletSigner
        addr = self._add(mainwindow, "hot")
        ui = FakeInteraction(secret="sekret")
        signer, _progress = mainwindow._pick_signer_for(mainwindow, addr, ui)
        assert isinstance(signer, HotWalletSigner)
        assert ui.secret_calls == [(f"Passphrase for {addr}:", "Hot wallet")]

    def test_hot_cancel_returns_no_signer(self, mainwindow):
        addr = self._add(mainwindow, "hot")
        ui = FakeInteraction(secret=None)
        signer, _progress = mainwindow._pick_signer_for(mainwindow, addr, ui)
        assert signer is None

    def test_watch_only_warns_and_returns_no_signer(
            self, mainwindow, monkeypatch):
        import qeth.ui as ui
        warned = []
        monkeypatch.setattr(ui, "warn", lambda *a, **k: warned.append(a))
        addr = self._add(mainwindow, "watch_only")
        signer, _progress = mainwindow._pick_signer_for(
            mainwindow, addr, FakeInteraction())
        assert signer is None
        assert warned


class TestBeginSignWiring:
    """The refactored tx-signing flow: _begin_sign builds the interaction host,
    shows the spinner and wires the worker; _on_tx_sign_failed closes the host
    and re-enables Confirm without tearing the sign dialog down."""

    def _spy_interaction_cls(self, instances):
        class SpyInteraction:
            def __init__(self, parent=None, title=""):
                self.progress_calls = []
                self.closed = False
                instances.append(self)

            def progress(self, text):
                self.progress_calls.append(text)

            def close(self):
                self.closed = True
        return SpyInteraction

    def test_begin_sign_shows_spinner_and_starts_worker(
            self, mainwindow, monkeypatch):
        import qeth.ui as ui
        from qeth.signing import SignAndBroadcastWorker
        instances = []
        monkeypatch.setattr(
            ui, "DialogInteraction", self._spy_interaction_cls(instances))
        signer = SimpleNamespace(can_sign=lambda a: True)
        monkeypatch.setattr(
            mainwindow, "_pick_signer_for",
            lambda dlg, addr, it: (signer, "Signing…"))
        started = []
        monkeypatch.setattr(mainwindow, "start_worker", started.append)
        sip = []
        dialog = SimpleNamespace(
            finalised_request=lambda: SimpleNamespace(from_addr=ADDR),
            set_signing_in_progress=sip.append)

        mainwindow._begin_sign(
            dialog, SimpleNamespace(chain_id=1),
            on_broadcast=lambda h: None, on_fail=lambda m: None)

        assert sip == [True]                          # entered signing state
        assert instances[0].progress_calls == ["Signing…"]   # spinner shown
        assert len(started) == 1
        assert isinstance(started[0], SignAndBroadcastWorker)

    def test_on_tx_sign_failed_closes_host_and_reenables(
            self, mainwindow, monkeypatch):
        import qeth.ui as ui
        monkeypatch.setattr(ui, "warn", lambda *a, **k: None)
        instances = []
        host = self._spy_interaction_cls(instances)()
        sip = []
        dialog = SimpleNamespace(set_signing_in_progress=sip.append)
        failed = []
        mainwindow._on_tx_sign_failed("boom", dialog, host, failed.append)
        assert host.closed is True                    # spinner dismissed
        assert sip == [False]                         # Confirm re-enabled
        assert failed == ["boom"]
