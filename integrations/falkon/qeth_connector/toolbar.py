# ============================================================
# qeth connector — navigation-toolbar status button
#
# Adds a qeth button to Falkon's navigation bar (like a browser extension's
# toolbar icon): the icon is bright when the qeth wallet is reachable and
# dimmed when it isn't, and clicking it pops up the current network + account
# — the same "which wallet is connected?" glance the webext popup gives.
#
# A single shared StatusPoller does the networking (native QNetworkAccessManager,
# outside Chromium's CSP/PNA, same as the bridge) and every window's button
# listens to it. The parsing lives in the Qt-free, unit-tested probe module.
# ============================================================

import os

import Falkon
from PySide6.QtCore import QByteArray, QObject, QSize, QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtNetwork import (
    QNetworkAccessManager, QNetworkReply, QNetworkRequest,
)
from PySide6.QtWidgets import QMenu

from qeth_connector import probe

_DIR = os.path.dirname(os.path.abspath(__file__))


class StatusPoller(QObject):
    """Polls qeth on a timer and emits ``changed(Status)`` when the reachable /
    chain / account state moves. Shared across every window's button; parent it
    to the plugin so it lives for the plugin's lifetime."""

    changed = Signal(object)   # probe.Status

    def __init__(self, parent=None):
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)
        self.status = probe.Status()
        self._inflight = False
        self._timer = QTimer(self)
        self._timer.setInterval(probe.POLL_INTERVAL_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self):
        if self._inflight:
            return
        self._inflight = True
        req = QNetworkRequest(QUrl(probe.ENDPOINT))
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader,
                      "application/json")
        req.setTransferTimeout(probe.REQUEST_TIMEOUT_MS)
        reply = self._nam.post(req, QByteArray(probe.batch_body()))
        reply.finished.connect(lambda r=reply: self._done(r))

    def _done(self, reply):
        self._inflight = False
        err = reply.error()
        data = bytes(reply.readAll().data()).decode("utf-8", "replace")
        reply.deleteLater()
        if err != QNetworkReply.NetworkError.NoError:
            st = probe.Status(error=str(err).split(".")[-1])
        else:
            st = probe.parse_status(data)
        if (st.connected, st.chain, st.account) != (
                self.status.connected, self.status.chain, self.status.account):
            self.status = st
            self.changed.emit(st)
        else:
            self.status = st

    def stop(self):
        self._timer.stop()


class QethStatusButton(Falkon.AbstractButtonInterface):
    """A navigation-bar button whose icon tracks wallet reachability and whose
    click shows the current network + account."""

    def __init__(self, poller):
        super().__init__()
        self._poller = poller
        base = QIcon(os.path.join(_DIR, "qeth-icon.svg"))
        self._icon_on = base
        # A dimmed variant for "not connected" — Qt's Disabled rendering
        # desaturates the same mark (mirrors the webext's icon<n>-off.png).
        self._icon_off = QIcon(base.pixmap(QSize(128, 128), QIcon.Mode.Disabled))
        self.setTitle("qeth")
        self.clicked.connect(self._on_clicked)
        poller.changed.connect(self._apply)
        self._apply(poller.status)

    def id(self):
        return "qeth-connector-button"

    def name(self):
        return "qeth wallet"

    def _apply(self, st):
        self.setIcon(self._icon_on if st.connected else self._icon_off)
        if st.connected:
            acct = st.account or "no account selected"
            self.setToolTip(f"qeth — connected ({probe.chain_name(st.chain)})"
                            f"\n{acct}")
        else:
            self.setToolTip("qeth wallet — not running (127.0.0.1:1248)")

    def _on_clicked(self, controller):
        self._poller.refresh()             # freshen while we're at it
        st = self._poller.status
        menu = QMenu()
        if st.connected:
            self._info(menu, "Connected to qeth")
            self._info(menu, f"Network: {probe.chain_name(st.chain)}")
            if st.account:
                short = st.account[:6] + "…" + st.account[-4:]
                act = menu.addAction(f"Account: {short}")
                act.setToolTip("Copy address")
                addr = st.account
                act.triggered.connect(
                    lambda: QGuiApplication.clipboard().setText(addr))
            else:
                self._info(menu, "No account selected in qeth")
        else:
            self._info(menu, "qeth not connected")
            self._info(menu, "Start qeth — it serves 127.0.0.1:1248")
        menu.addSeparator()
        menu.addAction("Recheck", self._poller.refresh)
        menu.addAction("Wallet status…", self._open_dialog)

        menu.popup(controller.callPopupPosition(menu.sizeHint()))
        menu.aboutToHide.connect(controller.callPopupClosed)
        self._menu = menu                  # keep a ref so it isn't GC'd

    @staticmethod
    def _info(menu, text):
        """A non-clickable status line in the popup."""
        action = menu.addAction(text)
        action.setEnabled(False)

    @staticmethod
    def _open_dialog():
        from qeth_connector.settings import StatusDialog
        StatusDialog().exec()
