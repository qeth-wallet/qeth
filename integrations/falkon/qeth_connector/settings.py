# ============================================================
# qeth connector — status dialog
#
# Shown from Falkon's Preferences → Extensions "Settings" button. There
# is nothing to configure — the connector is zero-config — so this just
# reports whether the qeth wallet is reachable on 127.0.0.1:1248, and if
# so which account and network it's currently on. A "Recheck" button
# re-probes.
#
# The probe uses Qt's native HTTP (QNetworkAccessManager), the same
# stack the bridge uses, so it doesn't depend on the wallet UI or any
# page context. Requests run sequentially (chainId, then accounts) — one
# request at a time keeps it robust against connection reuse while qeth
# is also serving the page provider.
# ============================================================

import json
import os

from PySide6.QtCore import Qt, QByteArray, QUrl
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import (
    QNetworkAccessManager, QNetworkReply, QNetworkRequest,
)
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLayout, QStyle,
    QVBoxLayout,
)

_ENDPOINT = "http://127.0.0.1:1248/"
_DIR = os.path.dirname(os.path.abspath(__file__))


def _connector_version():
    """Plugin version, read from metadata.desktop (the single source of
    truth Falkon also reads). Empty string if unreadable."""
    try:
        with open(os.path.join(_DIR, "metadata.desktop"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("X-Falkon-Version="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""

# Friendly names for the chains qeth ships with; anything else falls
# back to "Chain <id>". Purely cosmetic for the status line.
_CHAIN_NAMES = {
    1: "Ethereum", 10: "Optimism", 56: "BNB Chain", 100: "Gnosis",
    137: "Polygon", 8453: "Base", 42161: "Arbitrum", 43114: "Avalanche",
}


def _chain_name(hex_id):
    try:
        cid = int(hex_id, 16)
    except (TypeError, ValueError):
        return str(hex_id)
    return _CHAIN_NAMES.get(cid, f"Chain {cid}")


class StatusDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("qeth wallet connector")
        # Wide enough that a full 0x… address sits on one line.
        self.setMinimumWidth(480)
        self._nam = QNetworkAccessManager(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(16)
        # Grow the window to fit its contents. The detail line (account +
        # network) is filled in asynchronously after the dialog is shown,
        # so without this the window keeps its initial, shorter size and
        # the account line ends up clipped below the bottom edge.
        outer.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        header = QHBoxLayout()
        header.setSpacing(14)
        self._icon = QLabel()
        self._icon.setFixedWidth(48)
        self._icon.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        header.addWidget(self._icon, 0, Qt.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(6)
        self._status = QLabel()
        sf = self._status.font()
        sf.setBold(True)
        sf.setPointSizeF(sf.pointSizeF() * 1.15)
        self._status.setFont(sf)
        self._status.setWordWrap(True)
        self._detail = QLabel()
        self._detail.setTextFormat(Qt.RichText)
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text_col.addWidget(self._status)
        text_col.addWidget(self._detail)
        text_col.addStretch(1)
        header.addLayout(text_col, 1)
        outer.addLayout(header)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self._recheck = buttons.addButton("Recheck", QDialogButtonBox.ActionRole)
        self._recheck.clicked.connect(self._probe)
        buttons.rejected.connect(self.reject)

        # Bottom row: muted version on the left, buttons on the right.
        bottom = QHBoxLayout()
        version = _connector_version()
        if version:
            vlabel = QLabel(f"qeth connector {version}")
            vf = vlabel.font()
            vf.setPointSizeF(max(7.0, vf.pointSizeF() * 0.85))
            vlabel.setFont(vf)
            vlabel.setEnabled(False)   # theme-safe muting via disabled text colour
            bottom.addWidget(vlabel)
        bottom.addStretch(1)
        bottom.addWidget(buttons)
        outer.addLayout(bottom)

        self._probe()

    # --- icon helper (theme icons, with style-standard fallbacks) -----

    def _set_icon(self, *theme_names, fallback):
        icon = QIcon()
        for name in theme_names:
            icon = QIcon.fromTheme(name)
            if not icon.isNull():
                break
        if icon.isNull():
            icon = self.style().standardIcon(fallback)
        self._icon.setPixmap(icon.pixmap(48, 48))

    # --- probe --------------------------------------------------------

    def _probe(self):
        self._recheck.setEnabled(False)
        self._chain = None
        self._account = None
        self._error = None
        self._set_icon("view-refresh", "content-loading",
                       fallback=QStyle.StandardPixmap.SP_BrowserReload)
        self._status.setText("Checking connection…")
        self._detail.setText("")

        # One batched JSON-RPC request — a single round-trip carries both
        # the chain id (id 1) and the account (id 2). A second separate
        # request was unreliable from the plugin's network manager, so
        # everything goes in one POST, which qeth answers as a JSON array.
        req = QNetworkRequest(QUrl(_ENDPOINT))
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader,
                      "application/json")
        req.setTransferTimeout(4000)
        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
            {"jsonrpc": "2.0", "id": 2, "method": "eth_accounts", "params": []},
        ]
        body = json.dumps(batch).encode("utf-8")
        reply = self._nam.post(req, QByteArray(body))
        reply.finished.connect(lambda r=reply: self._done(r))

    def _done(self, reply):
        err = reply.error()
        data = bytes(reply.readAll().data()).decode("utf-8", "replace")
        reply.deleteLater()
        if err != QNetworkReply.NetworkError.NoError:
            self._error = str(err).split(".")[-1]
            self._render()
            return
        try:
            envs = json.loads(data)
        except Exception as e:
            self._error = str(e)
            self._render()
            return
        if not isinstance(envs, list):
            envs = [envs]
        for env in envs:
            rid = env.get("id")
            if env.get("error"):
                if rid == 1:   # chain id failing means the link is broken
                    self._error = env["error"].get("message", "error")
                continue
            result = env.get("result")
            if rid == 1:
                self._chain = result
            elif rid == 2 and isinstance(result, list) and result:
                self._account = result[0]
        self._render()

    # --- render -------------------------------------------------------

    def _render(self):
        self._recheck.setEnabled(True)
        if self._error is None and self._chain is not None:
            self._set_icon("network-transmit-receive", "dialog-ok-apply",
                           fallback=QStyle.StandardPixmap.SP_DialogApplyButton)
            self._status.setText("Connected to qeth")
            account = self._account or "No account selected in qeth"
            # Don't wrap the address — let the window widen to keep it on
            # one line (nicer than breaking a 0x… hash mid-line).
            self._detail.setWordWrap(False)
            self._detail.setText(
                f"Network: <b>{_chain_name(self._chain)}</b><br>"
                f"Account: {account}"
            )
        else:
            self._set_icon("network-offline", "dialog-warning",
                           fallback=QStyle.StandardPixmap.SP_MessageBoxWarning)
            self._status.setText("Not connected")
            # The hint is a sentence — wrap it rather than widening a lot.
            self._detail.setWordWrap(True)
            detail = (
                "The qeth wallet doesn't seem to be running. Start qeth — it "
                "serves the connector on <code>127.0.0.1:1248</code> — then "
                "press Recheck."
            )
            if self._error:
                detail += f"<br>({self._error})"
            self._detail.setText(detail)

        # The detail line is filled in here, after the dialog is already
        # on screen, so the window won't grow on its own — actively size
        # it to fit. Grow-only, so a user who enlarged it keeps their size
        # on Recheck. activate() forces the layout to recompute first so
        # sizeHint reflects the new (wrapped) detail height.
        self.layout().activate()
        hint = self.sizeHint()
        self.resize(max(self.width(), hint.width()),
                    max(self.height(), hint.height()))
