# ============================================================
# qeth connector — native networking bridge
#
# A QObject exposed to web pages (via Falkon's per-page QWebChannel, in
# the privileged SafeJsWorld) that relays JSON-RPC between a dapp and the
# qeth wallet server on 127.0.0.1:1248.
#
# Why this exists: a provider injected into the page's own JS context is
# bound by the page's Content-Security-Policy (`connect-src`) and
# Chromium's Private Network Access, so it cannot reach loopback on a
# strict-CSP dapp. This bridge does the networking with Qt's *native*
# stack (QNetworkAccessManager), which is outside Chromium entirely —
# no CSP, no PNA — and hands results back over the web channel.
#
# Transport is plain HTTP request/response. We deliberately avoid
# QtWebSockets (not present in every PySide6 build, e.g. system Qt here),
# so live wallet events are surfaced by the page-side provider polling
# the (locally-served, cheap) eth_chainId / eth_accounts instead of a
# push subscription.
#
# One bridge object is shared across all pages (Falkon registers extra
# objects globally), so every call carries a connection id ``cid`` that
# scopes replies to the originating frame, and the dapp ``origin`` so
# qeth's per-origin chain tracking behaves exactly as for a direct
# connection.
# ============================================================

import json
import logging

from PySide6.QtCore import QByteArray, QObject, QUrl, Signal, Slot
from PySide6.QtNetwork import (
    QNetworkAccessManager, QNetworkReply, QNetworkRequest,
)

log = logging.getLogger("qeth.falkon.bridge")

_ENDPOINT = "http://127.0.0.1:1248/"


def _dapp_origin(origin):
    """The ``Origin`` header value to forward for a dapp request, or ``""`` for
    none. Only an http(s) origin identifies a website. A ``file://`` page's
    ``window.location.origin`` collapses to a shared ``"file://"`` (and other
    schemes to an opaque ``"null"``), so those are treated as origin-less rather
    than letting every local file share one per-origin slot in qeth. Mirrors
    originOf() in the webext background and _effective_origin() in qeth/rpc.py."""
    o = (origin or "").strip()
    return o if o.lower().startswith(("http://", "https://")) else ""


class QethBridge(QObject):
    # cid, json-text — emitted for every reply, routed back to the frame
    # whose relay sent the request.
    message = Signal(str, str)

    def __init__(self, endpoint=_ENDPOINT, parent=None):
        super().__init__(parent)
        self._endpoint = endpoint
        self._nam = QNetworkAccessManager(self)

    @Slot(str, str, str)
    def send(self, cid, origin, text):
        """Forward one JSON-RPC message (``text``) to qeth over native
        HTTP, tagging the upstream request with the dapp ``origin`` so
        qeth scopes chain state per-origin. The reply (or a synthesized
        JSON-RPC error on transport failure) comes back via ``message``."""
        req = QNetworkRequest(QUrl(self._endpoint))
        req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader,
                      "application/json")
        dapp = _dapp_origin(origin)
        if dapp:
            req.setRawHeader(b"Origin", dapp.encode("ascii", "ignore"))
        reply = self._nam.post(req, QByteArray(text.encode("utf-8")))

        def done():
            body = bytes(reply.readAll().data()).decode("utf-8", "replace")
            err = reply.error()
            reply.deleteLater()
            if err != QNetworkReply.NetworkError.NoError and not body.strip():
                body = self._error_envelope(text, str(err).split(".")[-1])
            self.message.emit(cid, body)

        reply.finished.connect(done)

    def _error_envelope(self, request_text, detail):
        """Build a JSON-RPC error response carrying the request's id, so
        the page-side provider rejects the matching promise instead of
        hanging when qeth is unreachable."""
        rid = None
        try:
            rid = json.loads(request_text).get("id")
        except Exception:
            pass
        return json.dumps({
            "jsonrpc": "2.0", "id": rid,
            "error": {"code": -32603, "message": "qeth unreachable: " + detail},
        })
