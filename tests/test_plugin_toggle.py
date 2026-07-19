"""The shared plugin on/off menu (tray submenu + config gear both use it)."""

from types import SimpleNamespace

from qeth.plugin_toggle import build_plugin_toggle_menu, optional_manifests


def _fake_store(disabled=()):
    calls: list = []
    store = SimpleNamespace(disabled_plugins=set(disabled))
    store.set_plugin_enabled = lambda pid, on: calls.append((pid, on))
    return store, calls


def test_only_optional_plugins_are_offered():
    ids = [m.id for m in optional_manifests()]
    assert ids == ["tokens", "ens"]          # wallets + transactions required


def test_menu_lists_optional_with_enabled_checked(qtbot):
    store, _ = _fake_store()
    menu = build_plugin_toggle_menu(store)
    acts = menu.actions()
    assert [a.text() for a in acts] == ["Tokens", "ENS"]
    assert all(a.isCheckable() and a.isChecked() for a in acts)


def test_disabled_plugin_is_unchecked(qtbot):
    store, _ = _fake_store({"tokens"})
    menu = build_plugin_toggle_menu(store)
    by = {a.text(): a for a in menu.actions()}
    assert not by["Tokens"].isChecked()
    assert by["ENS"].isChecked()


def test_toggling_calls_store_and_callback(qtbot):
    store, calls = _fake_store()
    changes: list = []
    menu = build_plugin_toggle_menu(
        store, on_toggled=lambda pid, on: changes.append((pid, on)))
    tokens_act = next(a for a in menu.actions() if a.text() == "Tokens")
    tokens_act.trigger()                       # was checked → now unchecked
    assert calls == [("tokens", False)]        # store.set_plugin_enabled called
    assert changes == [("tokens", False)]      # restart-nudge callback fired


def test_toggle_persists_through_a_real_store(qtbot, tmp_qeth):
    from qeth.store import Store
    store = Store.load()
    menu = build_plugin_toggle_menu(store)
    next(a for a in menu.actions() if a.text() == "ENS").trigger()
    assert "ens" in store.disabled_plugins
    assert Store.load().disabled_plugins == {"ens"}     # persisted to disk
