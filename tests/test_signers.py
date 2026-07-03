"""Signer plugin REGISTRY (docs/signers.md step 1): the ``source`` →
``SignerPlugin`` dispatch + per-source metadata that replaced the duplicated
``if source == "ledger"/"hot"`` branches in ui.py."""

from types import SimpleNamespace

import pytest

from qeth.signers import REGISTRY, signer_for_source
from qeth.signers.base import SignerPlugin

# The signer constructors only stash the store ref (no work at __init__), so a
# bare object stands in.
STORE = SimpleNamespace(accounts=[])
ADDR = "0x" + "ab" * 20


def test_registry_has_the_three_known_sources():
    assert set(REGISTRY) == {"ledger", "hot", "watch_only"}
    assert all(isinstance(p, SignerPlugin) for p in REGISTRY.values())
    # the key each plugin is filed under matches its own source_id
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
    assert p.secret_prompt(ADDR) is None            # no up-front secret
    assert isinstance(p.make_signer(STORE, None), LedgerSigner)


def test_hot_plugin_prompts_then_builds_a_hot_signer():
    from qeth.hot_wallet import HotWalletSigner
    p = signer_for_source("hot")
    assert p is not None
    assert p.can_sign() is True
    assert p.display_name == "Hot wallet"
    assert p.progress_text
    assert p.secret_prompt(ADDR) == f"Passphrase for {ADDR}:"
    assert isinstance(p.make_signer(STORE, "pw"), HotWalletSigner)


def test_watch_only_is_registered_but_cannot_sign():
    from qeth.signing import SignerError
    p = signer_for_source("watch_only")
    assert p is not None
    assert p.can_sign() is False
    assert p.secret_prompt(ADDR) is None
    with pytest.raises(SignerError):
        p.make_signer(STORE, None)


class TestDispatchThroughMainWindow:
    """ui.py's ``_pick_signer_for`` drives the registry: routes by the account's
    ``source``, prompts for the hot-wallet passphrase up front, and warns (no
    signer) for watch-only. Exercises the refactored dispatch end-to-end."""

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
        signer, progress = mainwindow._pick_signer_for(mainwindow, addr)
        assert isinstance(signer, LedgerSigner)
        assert progress                                   # spinner label set

    def test_hot_source_prompts_then_returns_a_hot_signer(
            self, mainwindow, monkeypatch):
        import qeth.ui as ui
        from qeth.hot_wallet import HotWalletSigner
        seen = {}

        def fake_prompt(dialog, title, label, password=False):
            seen.update(title=title, label=label, password=password)
            return "sekret", True
        monkeypatch.setattr(ui, "prompt_text", fake_prompt)
        addr = self._add(mainwindow, "hot")
        signer, _progress = mainwindow._pick_signer_for(mainwindow, addr)
        assert isinstance(signer, HotWalletSigner)
        assert seen == {"title": "Hot wallet",
                        "label": f"Passphrase for {addr}:", "password": True}

    def test_hot_cancel_returns_none(self, mainwindow, monkeypatch):
        import qeth.ui as ui
        monkeypatch.setattr(ui, "prompt_text", lambda *a, **k: ("", False))
        addr = self._add(mainwindow, "hot")
        assert mainwindow._pick_signer_for(mainwindow, addr) == (None, None)

    def test_watch_only_warns_and_returns_none(self, mainwindow, monkeypatch):
        import qeth.ui as ui
        warned = []
        monkeypatch.setattr(ui, "warn", lambda *a, **k: warned.append(a))
        addr = self._add(mainwindow, "watch_only")
        assert mainwindow._pick_signer_for(mainwindow, addr) == (None, None)
        assert warned                                     # showed "no signer"
