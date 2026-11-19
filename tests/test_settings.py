"""The settings button and tray entry open one shared, staged settings dialog."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QSystemTrayIcon, QToolButton

from qeth.chain_rpc_dialog import ChainRpcDialog, _ChainlistLoader, _UrlProbeWorker
from qeth.store import Store


@pytest.fixture
def idle_probes(monkeypatch):
    monkeypatch.setattr(_ChainlistLoader, "start", lambda self: None)
    monkeypatch.setattr(_UrlProbeWorker, "start", lambda self: None)


def test_settings_button_has_clear_label_and_stays_available(mainwindow):
    button = mainwindow.settings_btn
    assert button.text().replace("&", "") == "Settings"
    assert button.toolTip() == "Settings"
    assert button.accessibleName() == "Settings"
    assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    assert mainwindow.right_slot._corner.itemAt(0).widget() is button
    assert [
        b for b in mainwindow.findChildren(QToolButton) if b.toolTip() == "Settings"
    ] == [button]
    mainwindow.right_slot.set_active(mainwindow.ens_plugin)
    assert mainwindow.chain_combo.isHidden()
    assert not button.isHidden()
    mainwindow.right_slot.set_active(mainwindow.tokens_plugin)
    assert not button.isHidden()


@pytest.mark.parametrize("accepted", [True, False])
def test_settings_save_or_cancel_all_preferences(
    mainwindow, monkeypatch, idle_probes, accepted
):
    store = mainwindow.store
    store.set_plugin_enabled("ens", False)
    store.set_plugin_enabled("unknown-plugin", False)
    original_url = store.current_chain().rpc_url
    original_key = store.etherscan_api_key
    monkeypatch.setattr(
        mainwindow.rpc, "broadcast_chains_changed", lambda: None, raising=False
    )
    monkeypatch.setattr("qeth.helios.prewarm", lambda chain: None)

    def edit(dlg):
        assert dlg.windowTitle() == "Settings"
        assert not dlg.plugin_checks["ens"].isChecked()
        assert dlg.notifications_check.isChecked()
        dlg.url_edit.setText("https://settings.example")
        dlg.etherscan_edit.setText("new-key")
        dlg.plugin_checks["ens"].setChecked(True)
        dlg.plugin_checks["tokens"].setChecked(False)
        dlg.notifications_check.setChecked(False)
        # Settings are drafts until OK, including the controls formerly in menus.
        assert store.disabled_plugins == {"ens", "unknown-plugin"}
        assert store.notifications_enabled
        buttons = dlg.findChild(QDialogButtonBox)
        assert buttons.standardButtons() == (
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button = buttons.button(
            QDialogButtonBox.StandardButton.Ok
            if accepted
            else QDialogButtonBox.StandardButton.Cancel
        )
        button.click()
        return dlg.result()

    monkeypatch.setattr(ChainRpcDialog, "exec", edit)
    mainwindow.settings_btn.click()
    reloaded = Store.load()
    if accepted:
        assert reloaded.disabled_plugins == {"tokens", "unknown-plugin"}
        assert not reloaded.notifications_enabled
        assert reloaded.current_chain().rpc_url == "https://settings.example"
        assert reloaded.etherscan_api_key == "new-key"
        # Changes to mounted plugins still take effect on the next launch.
        assert mainwindow.tokens_plugin is not None
        assert mainwindow._hint_label.text() == "Restart qeth to apply plugin changes"
    else:
        assert reloaded.disabled_plugins == {"ens", "unknown-plugin"}
        assert reloaded.notifications_enabled
        assert reloaded.current_chain().rpc_url == original_url
        assert reloaded.etherscan_api_key == original_key


def test_unchanged_settings_do_not_write_store(mainwindow, monkeypatch, idle_probes):
    saves = []
    monkeypatch.setattr(mainwindow.store, "save", lambda: saves.append(True))

    def accept(dlg):
        dlg.accept()
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ChainRpcDialog, "exec", accept)
    mainwindow.settings_btn.click()
    assert saves == []


def test_tray_settings_opens_same_window(mainwindow, qapp, monkeypatch):
    from qeth.tray import _TrayController

    calls = []
    monkeypatch.setattr(QSystemTrayIcon, "show", lambda self: None)
    monkeypatch.setattr(mainwindow, "show_settings", lambda: calls.append("settings"))
    tray = _TrayController(qapp, mainwindow)
    monkeypatch.setattr(tray, "_show", lambda: calls.append("show"))
    actions = [a for a in tray.tray.contextMenu().actions() if not a.isSeparator()]
    assert [a.text() for a in actions] == ["Show", "Hide", "Settings", "Exit"]
    actions[2].trigger()
    assert calls == ["show", "settings"]
    tray.deleteLater()
