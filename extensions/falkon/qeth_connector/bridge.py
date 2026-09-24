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
import os

from PySide6.QtCore import QByteArray, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtNetwork import (
    QNetworkAccessManager, QNetworkReply, QNetworkRequest,
)

log = logging.getLogger("qeth.falkon.bridge")

_ENDPOINT = "http://127.0.0.1:1248/"

# The bundled TronWeb (the unmodified npm dist — see tronweb/SOURCE.txt), run
# on demand in a frame whose page uses Tron (provider.js).
_TRONWEB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "tronweb", "TronWeb.js")
# QWebEngineScript.ScriptWorldId: the page's own world, and Falkon's
# SafeJsWorld (ApplicationWorld), where relay.js runs.
_MAIN_WORLD = 0
_SAFE_WORLD = 1
# How long the frame-finding probe may take before loadTronWeb gives up.
_PROBE_TIMEOUT_MS = 5000


def _origin_of(url):
    """``scheme://host[:port]`` of a QUrl — ``window.location.origin``'s form
    (a default port is absent from both)."""
    port = url.port()
    return f"{url.scheme()}://{url.host()}" + (f":{port}" if port != -1 else "")


def _frames(page):
    """Every frame of ``page`` that can run a script: the whole frame tree on
    Qt >= 6.8 (QWebEngineFrame), else the page itself — its main frame."""
    main = getattr(page, "mainFrame", None)
    if main is None:
        return [page]
    out, stack = [], [main()]
    while stack:
        frame = stack.pop()
        out.append(frame)
        stack.extend(frame.children())
    return out


def _web_views():
    """Every open web view (Falkon's tabs are QWebEngineView subclasses).

    The VIEWS, not their pages: PyFalkon ties a page's Python wrapper to its
    view's, so once the view's wrapper is collected the page's reads as
    "Internal C++ object … already deleted" — though the page lives on. So a
    caller keeps the view referenced for as long as it uses the page."""
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication
    return [w for w in QApplication.allWidgets() if isinstance(w, QWebEngineView)]


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
    # cid, ok, error — the answer to loadTronWeb.
    tronWebLoaded = Signal(str, bool, str)

    def __init__(self, endpoint=_ENDPOINT, parent=None, *,
                 views=_web_views, tronweb_path=_TRONWEB):
        super().__init__(parent)
        self._endpoint = endpoint
        self._nam = QNetworkAccessManager(self)
        self._views = views
        self._tronweb_path = tronweb_path
        self._tronweb_src = None

    @Slot(str, str, str)
    def loadTronWeb(self, cid, token, origin):
        """Run TronWeb in the frame whose relay asked (it holds ``token`` in
        the SafeJsWorld, which no page script can reach), in that frame's
        main world. Native injection: the page's CSP doesn't apply. The
        answer comes back as ``tronWebLoaded(cid, ok, error)`` — on EVERY
        path: an exception escaping a web-channel slot is swallowed, and the
        page would wait out its own timeout (a dapp's "connecting…" forever)."""
        try:
            self._load_tronweb(cid, token, origin)
        except Exception as e:
            log.exception("loading TronWeb failed")
            self.tronWebLoaded.emit(cid, False, f"qeth couldn't load TronWeb: {e}")

    def _candidate_frames(self, views, origin):
        frames = []
        for view in views:
            try:
                frames += [f for f in _frames(view.page())
                           if not origin or _origin_of(f.url()) == origin]
            except RuntimeError:
                # A view or page Qt already deleted — Falkon keeps a closed
                # tab's widgets until a later deleteLater.
                continue
        return frames

    def _load_tronweb(self, cid, token, origin):
        # The views stay referenced until the answer (see _web_views).
        state = {"left": 0, "done": False, "views": self._views()}

        def finish(ok, msg=""):
            if not state["done"]:
                state["done"] = True
                state["views"] = []
                self.tronWebLoaded.emit(cid, ok, msg)

        if self._tronweb_src is None:
            try:
                with open(self._tronweb_path, encoding="utf-8") as f:
                    self._tronweb_src = f.read()
            except OSError as e:
                log.warning("TronWeb bundle unreadable: %s", e)
                return finish(False, "TronWeb is missing from the qeth connector")
        src = self._tronweb_src
        probe = "window.__qethTronToken === " + json.dumps(token)

        def answered(frame, result):
            state["left"] -= 1
            if state["done"]:
                return
            if result is True:
                try:
                    frame.runJavaScript(src, _MAIN_WORLD, lambda _r: finish(True))
                except RuntimeError:
                    finish(False, "the page asking for Tron went away")
            elif state["left"] == 0:
                # A sub-frame on Qt < 6.8 (no frame tree to search).
                finish(False, "qeth can't load Tron into this frame under Falkon")

        for frame in self._candidate_frames(state["views"], origin):
            try:
                frame.runJavaScript(probe, _SAFE_WORLD,
                                    lambda r, fr=frame: answered(fr, r))
            except RuntimeError:       # deleted between listing and probing
                continue
            state["left"] += 1
        if not state["left"]:
            return finish(False, "qeth couldn't find the page asking for Tron")
        # A frame torn down mid-probe never calls back; don't let that hang.
        QTimer.singleShot(_PROBE_TIMEOUT_MS, lambda: finish(
            False, "qeth couldn't find the page asking for Tron"))

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
