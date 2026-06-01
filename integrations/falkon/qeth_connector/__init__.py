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
    with open(os.path.join(_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


class QethConnectorPlugin(Falkon.PluginInterface, QtCore.QObject):
    def init(self, state, settingsPath):
        # The native bridge is a single shared object registered onto
        # every page's web channel. Parent it to keep it alive for the
        # plugin's lifetime.
        self._bridge = QethBridge(parent=self)
        Falkon.ExternalJsObject.registerExtraObject(_BRIDGE_ID, self._bridge)
        self._install_scripts()

    def unload(self):
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

    def testPlugin(self):
        # Loaded unconditionally; degrades gracefully when qeth isn't
        # running (requests reject; no retry storm).
        return True

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
