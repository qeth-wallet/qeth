# ============================================================
# qeth connector — Falkon browser plugin
#
# Lets web dapps talk to a running qeth wallet, the way Frame's browser
# extension exposes the Frame desktop app. The wallet core needs no
# browser-specific code; everything here is glue around qeth's loopback
# JSON-RPC server (127.0.0.1:1248).
#
# How it dodges the page's Content-Security-Policy
# ------------------------------------------------
# A provider injected into the page's JS context is bound by the dapp's
# `connect-src` CSP (and Chromium's Private Network Access), so it can't
# reach loopback on a strict-CSP site. So the networking is done in
# native Python instead, reached over Falkon's per-page QWebChannel:
#
#   provider.js   (MainWorld)   sets window.ethereum, speaks postMessage
#        |  window.postMessage (crosses JS worlds, not subject to CSP)
#   relay.js      (SafeJsWorld) holds the native bridge via
#        |                      window.external.extra.qeth (Falkon maps
#        |                      every registered "qz_<id>" extra object)
#   QethBridge    (Python)      does HTTP to qeth with Qt's own network
#                               stack — outside Chromium, no CSP/PNA.
#
# We register the bridge once via ExternalJsObject.registerExtraObject,
# which adds it (as "qz_qeth") to every page's channel. The relay and
# provider scripts are injected into Falkon's two worlds.
#
# Licensed GPL-3.0-or-later, matching qeth.
# ============================================================

import base64
import logging
import os

import Falkon
from PySide6 import QtCore
from PySide6.QtWebEngineCore import QWebEngineScript

from qeth_connector.bridge import QethBridge

log = logging.getLogger("qeth.falkon")

_DIR = os.path.dirname(os.path.abspath(__file__))
_PROVIDER_SCRIPT = "_qeth_connector_provider"
_RELAY_SCRIPT = "_qeth_connector_relay"
_BRIDGE_ID = "qeth"   # exposed to JS as window.external.extra.qeth

# Falkon's world ids (src/lib/webengine/webpage.h):
#   UnsafeJsWorld = QWebEngineScript::MainWorld        — the page/dapp
#   SafeJsWorld   = QWebEngineScript::ApplicationWorld — privileged,
#       where Falkon's web-channel client lives and window.external is set
_MAIN_WORLD = QWebEngineScript.ScriptWorldId.MainWorld
_SAFE_WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld


def _logo_data_uri():
    """The wallet SVG as a data: URI for the EIP-6963 ``info.icon`` field
    (the spec requires a data URI). Empty string if the file is missing —
    the wallet still works, just without an icon in dapp pickers."""
    path = os.path.join(_DIR, "qeth-icon.svg")
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    return "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii")


def _read(name):
    with open(os.path.join(_DIR, name), encoding="utf-8") as f:
        return f.read()


class QethConnectorPlugin(Falkon.PluginInterface, QtCore.QObject):
    def init(self, state, settingsPath):
        # The native bridge is a single shared object registered onto
        # every page's web channel. Parent it to keep it alive for the
        # plugin's lifetime.
        self._bridge: QethBridge | None = QethBridge(parent=self)
        Falkon.ExternalJsObject.registerExtraObject(_BRIDGE_ID, self._bridge)
        self._install_scripts()

        # A qeth status button on each window's navigation bar (like a browser
        # extension's toolbar icon), all fed by one shared poller. Safe defaults
        # first, then wire it inside a guard: the button is a nicety, and a
        # hiccup here must never take the bridge/provider (the wallet link) down.
        self._poller = None
        self._button_cls = None
        self._buttons: dict = {}
        try:
            self._install_toolbar_button(state)
        except Exception as e:
            log.warning("qeth toolbar status button unavailable: %s", e)

    def _install_toolbar_button(self, state):
        from qeth_connector.toolbar import QethStatusButton, StatusPoller
        app = Falkon.MainApplication.instance()
        if app is None:
            return
        self._poller = StatusPoller(parent=self)
        self._button_cls = QethStatusButton
        plugins = app.plugins()
        plugins.mainWindowCreated.connect(self._add_button)
        plugins.mainWindowDeleted.connect(self._remove_button)
        # Plugins enabled after startup miss mainWindowCreated for the windows
        # already open — seed them (mirrors Falkon's own bundled plugins).
        if state == Falkon.PluginInterface.LateInitState:
            for window in app.windows():
                self._add_button(window)

    def unload(self):
        for window in list(self._buttons):
            self._remove_button(window)
        if self._poller is not None:
            self._poller.stop()
            self._poller = None
        try:
            Falkon.ExternalJsObject.unregisterExtraObject(self._bridge)
        except Exception as e:
            log.debug("unregister bridge: %s", e)
        scripts = self._scripts()
        if scripts is not None:
            for name in (_PROVIDER_SCRIPT, _RELAY_SCRIPT):
                for existing in scripts.find(name):
                    scripts.remove(existing)
        self._bridge = None

    def _add_button(self, window):
        if self._button_cls is None or window in self._buttons:
            return
        try:
            button = self._button_cls(self._poller)
            window.navigationBar().addToolButton(button)
        except Exception as e:
            log.warning("qeth toolbar button not added: %s", e)
            return
        self._buttons[window] = button

    def _remove_button(self, window):
        button = self._buttons.pop(window, None)
        if button is not None:
            try:
                window.navigationBar().removeToolButton(button)
            except Exception as e:
                log.debug("remove toolbar button: %s", e)

    def testPlugin(self):
        # Loaded unconditionally; degrades gracefully when qeth isn't
        # running (requests reject; no retry storm).
        return True

    def showSettings(self, parent=None):
        # No configuration — just a read-only "is the wallet reachable?"
        # status dialog (Preferences → Extensions → Settings).
        from qeth_connector.settings import StatusDialog
        StatusDialog(parent).exec()

    # --- internals ----------------------------------------------------

    def _scripts(self):
        app = Falkon.MainApplication.instance()
        return app.webProfile().scripts() if app is not None else None

    def _make_script(self, name, source, world):
        script = QWebEngineScript()
        script.setName(name)
        script.setSourceCode(source)
        script.setWorldId(world)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setRunsOnSubFrames(True)
        return script

    def _install_scripts(self):
        scripts = self._scripts()
        if scripts is None:
            return
        for name in (_PROVIDER_SCRIPT, _RELAY_SCRIPT):
            for existing in scripts.find(name):
                scripts.remove(existing)

        provider_src = _read("provider.js").replace(
            "__QETH_LOGO_DATA_URI__", _logo_data_uri())
        # provider.js: page world (sets window.ethereum for the dapp).
        scripts.insert(self._make_script(
            _PROVIDER_SCRIPT, provider_src, _MAIN_WORLD))
        # relay.js: Falkon's privileged world (reaches the native bridge
        # and shuttles messages across to the provider).
        scripts.insert(self._make_script(
            _RELAY_SCRIPT, _read("relay.js"), _SAFE_WORLD))


Falkon.registerPlugin(QethConnectorPlugin())
