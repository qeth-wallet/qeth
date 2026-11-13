"""Settings offers staged choices for exactly the optional plugins."""

from qeth.plugin_toggle import build_plugin_checkboxes, optional_manifests


def test_only_optional_plugins_are_offered():
    ids = [m.id for m in optional_manifests()]
    assert ids == ["tokens", "ens", "approvals"]  # wallets + transactions required


def test_choices_use_existing_plugin_names(qtbot):
    checks = build_plugin_checkboxes(())
    assert [c.text() for c in checks.values()] == ["Tokens", "ENS", "Approvals"]
    assert all(c.isChecked() for c in checks.values())
    assert [c.toolTip() for c in checks.values()] == [
        m.description for m in optional_manifests()
    ]


def test_disabled_plugin_is_unchecked(qtbot):
    checks = build_plugin_checkboxes({"tokens"})
    assert not checks["tokens"].isChecked()
    assert checks["ens"].isChecked()


def test_choices_do_not_mutate_stored_preferences(qtbot, tmp_qeth):
    from qeth.store import Store

    store = Store.load()
    checks = build_plugin_checkboxes(store.disabled_plugins)
    checks["ens"].click()
    assert not checks["ens"].isChecked()
    assert store.disabled_plugins == set()
    assert Store.load().disabled_plugins == set()
