"""Falkon connector regression gate for the shared provider.

provider.js is shared byte-for-byte with the browser extension and behaviour
is chosen by an optional ``window.__QETH_PROVIDER_CONFIG__`` the loader sets.
These text-level checks pin that the FALKON path keeps its original behaviour:
the config defaults reproduce the connector (poll + direct fallback, no push),
the plugin still substitutes the logo placeholder and never injects a config
(so the defaults are what runs), and the hard-won sub-frame inert behaviour is
intact. The webext mirror/behaviour lives in tests/test_webext.py.

(The Falkon plugin module imports the runtime ``Falkon`` module, so these read
the sources as text rather than importing them.)
"""

from pathlib import Path

FALKON = Path(__file__).resolve().parent.parent / "extensions" / "falkon" / "qeth_connector"
PROVIDER = (FALKON / "provider.js").read_text()
INIT = (FALKON / "__init__.py").read_text()


def test_config_defaults_are_the_falkon_connector():
    # No config object (Falkon sets none) → CFG is {}, and each flag must
    # default to the connector's behaviour.
    assert 'var CFG = window.__QETH_PROVIDER_CONFIG__ || {};' in PROVIDER
    assert 'var DIRECT_FALLBACK = CFG.directFallback !== false;' in PROVIDER  # default true
    assert 'var POLL = CFG.poll !== false;' in PROVIDER                       # default true
    assert 'var PUSH = CFG.push === true;' in PROVIDER                        # default false


def test_logo_placeholder_survives_for_substitution():
    # The loader substitutes this at load; if the config change had dropped
    # the placeholder the Falkon icon (EIP-6963) would break.
    assert '"__QETH_LOGO_DATA_URI__"' in PROVIDER
    assert 'CFG.logo || "__QETH_LOGO_DATA_URI__"' in PROVIDER


def test_falkon_plugin_substitutes_logo_and_injects_no_config():
    assert '"__QETH_LOGO_DATA_URI__"' in INIT      # still does the replace
    assert '.replace(' in INIT
    # Falkon must NOT set a provider config, or the defaults wouldn't apply.
    assert '__QETH_PROVIDER_CONFIG__' not in INIT


def test_subframe_inert_behaviour_intact():
    # The Safe-App fix: present-but-inert in a sub-frame. These lines must
    # survive the transport refactor unchanged.
    assert 'this.isMetaMask = !IN_SUBFRAME;' in PROVIDER
    assert 'this._authorized = !IN_SUBFRAME;' in PROVIDER
    # eth_accounts answered locally as [] while unauthorized — and so is
    # wallet_getPermissions, which asks the same "are we connected?" question
    # in EIP-2255 form (a library probing with it would otherwise read us as
    # authorized inside the frame and take us over the Safe connector).
    assert 'if (!this._authorized && (args.method === "eth_accounts"' in PROVIDER
    assert '|| args.method === "wallet_getPermissions")' in PROVIDER
    # An explicit connect lifts the gate — either spelling of it.
    assert 'if (args.method === "eth_requestAccounts"' in PROVIDER
    assert '|| args.method === "wallet_requestPermissions") self._authorized = true;' \
        in PROVIDER
    # EIP-6963 announce is top-frame only.
    assert 'if (!IN_SUBFRAME) {' in PROVIDER


def test_snapshot_before_request_comparison_preserved():
    # The poll/reconnect refresh must compare against the value captured
    # BEFORE the request (a real bug fix), not self.* after _absorb ran.
    assert 'var prevChain = this.chainId;' in PROVIDER
    assert 'var prevAccount = this.selectedAddress;' in PROVIDER


def _load_module(filename):
    # probe.py / bridge.py import no Falkon runtime, so they load standalone
    # (unlike __init__.py/settings.py). No __pycache__ left in the plugin dir.
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location(
        "qeth_falkon_" + filename.split(".")[0], FALKON / filename)
    mod = importlib.util.module_from_spec(spec)
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = prev
    return mod


class TestProbe:
    """The Qt-free status probe shared by the settings dialog and the toolbar
    button (extensions/falkon/qeth_connector/probe.py)."""

    def _probe(self):
        return _load_module("probe.py")

    def test_chain_names(self):
        p = self._probe()
        assert p.chain_name("0x1") == "Ethereum"
        assert p.chain_name("0xa") == "Optimism"
        assert p.chain_name("0x2105") == "Base"
        assert p.chain_name("0x63") == "Chain 99"     # unknown id → fallback
        assert p.chain_name(None) == "None"           # non-hex → str()

    def test_parse_connected(self):
        import json
        p = self._probe()
        st = p.parse_status(json.dumps([
            {"jsonrpc": "2.0", "id": 1, "result": "0x1"},
            {"jsonrpc": "2.0", "id": 2, "result": ["0xABC"]},
        ]))
        assert st.connected and st.chain == "0x1" and st.account == "0xABC"
        assert st.error is None

    def test_parse_connected_without_account(self):
        import json
        p = self._probe()
        st = p.parse_status(json.dumps([
            {"id": 1, "result": "0xa"}, {"id": 2, "result": []}]))
        assert st.connected and st.chain == "0xa" and st.account is None

    def test_parse_chain_error_is_disconnected(self):
        import json
        p = self._probe()
        st = p.parse_status(json.dumps([
            {"id": 1, "error": {"code": -1, "message": "boom"}},
            {"id": 2, "result": ["0xABC"]}]))
        assert not st.connected and st.error == "boom"

    def test_parse_single_object_and_garbage(self):
        import json
        p = self._probe()
        # A non-list (single envelope) response is still parsed.
        st = p.parse_status(json.dumps({"id": 1, "result": "0x1"}))
        assert st.connected and st.chain == "0x1"
        # Malformed JSON → error, not connected (a dead/absent server).
        bad = p.parse_status("{ not json")
        assert not bad.connected and bad.error

    def test_batch_body_shape(self):
        import json
        p = self._probe()
        body = json.loads(p.batch_body())
        assert [e["method"] for e in body] == ["eth_chainId", "eth_accounts",
                                               "tron_accounts"]
        assert [e["id"] for e in body] == [1, 2, 3]

    def test_parse_the_tron_account(self):
        import json
        p = self._probe()
        st = p.parse_status(json.dumps([
            {"id": 1, "result": "0x1"}, {"id": 2, "result": ["0xABC"]},
            {"id": 3, "result": ["TSzckeDYKoVyMhoh7jQ3kH9vLi5g5ZtfFL"]}]))
        assert st.connected and st.tron_account == "TSzckeDYKoVyMhoh7jQ3kH9vLi5g5ZtfFL"
        # A qeth without Tron errors on id 3: still connected, just no Tron line.
        old = p.parse_status(json.dumps([
            {"id": 1, "result": "0x1"}, {"id": 2, "result": ["0xABC"]},
            {"id": 3, "error": {"code": -32601, "message": "no such method"}}]))
        assert old.connected and old.error is None and old.tron_account is None


def test_bridge_forwards_only_http_origins():
    # Finding C from the Frame Companion review: a file:// page's
    # window.location.origin collapses to a shared "file://", so every local
    # file would share one per-origin slot in qeth. Only http(s) origins are
    # forwarded as the Origin header; everything else is origin-less.
    bridge = _load_module("bridge.py")
    assert bridge._dapp_origin("https://app.uniswap.org") == "https://app.uniswap.org"
    assert bridge._dapp_origin("http://localhost:3000") == "http://localhost:3000"
    for opaque in ("file://", "null", "", None, "chrome://x", "about:blank",
                   "data:text/html,x"):
        assert bridge._dapp_origin(opaque) == "", opaque


# --- TronWeb on demand (bridge.loadTronWeb) -------------------------------------

class _Url:
    def __init__(self, scheme, host, port=-1):
        self._s, self._h, self._p = scheme, host, port

    def scheme(self):
        return self._s

    def host(self):
        return self._h

    def port(self):
        return self._p


class _Frame:
    """A QWebEngineFrame stand-in: answers the token probe from its
    SafeJsWorld "global", records what ran in the main world."""

    def __init__(self, url, token=None, children=()):
        self._url, self.token, self._children = url, token, list(children)
        self.main_world: list[str] = []

    def url(self):
        return self._url

    def children(self):
        return self._children

    def runJavaScript(self, src, world, callback):  # noqa: N802 — Qt's name
        if world == 1:                                      # the relay's world
            callback(src.endswith('"%s"' % self.token) if self.token else False)
        else:
            self.main_world.append(src)
            callback(None)


class _Page:
    def __init__(self, main):
        self._main = main

    def mainFrame(self):  # noqa: N802
        return self._main


class _View:
    def __init__(self, page):
        self._page = page

    def page(self):
        return self._page


def _tron_bridge(tmp_path, pages):
    mod = _load_module("bridge.py")
    tw = tmp_path / "TronWeb.js"
    tw.write_text("/* tronweb */")
    b = mod.QethBridge(views=lambda: [_View(p) for p in pages], tronweb_path=str(tw))
    got = []
    b.tronWebLoaded.connect(lambda cid, ok, err: got.append((cid, ok, err)))
    return b, got


def test_bridge_loads_tronweb_into_the_frame_holding_the_token(qapp, tmp_path):
    origin = _Url("https", "dapp.example")
    asking = _Frame(origin, token="tok")
    bystander = _Frame(origin)                        # same origin, other frame
    other_site = _Frame(_Url("https", "evil.example"), token="tok")
    page = _Page(_Frame(origin, children=[bystander, asking]))
    b, got = _tron_bridge(tmp_path, [page, _Page(other_site)])
    b.loadTronWeb("cid", "tok", "https://dapp.example")
    assert got == [("cid", True, "")]
    assert asking.main_world == ["/* tronweb */"]
    assert bystander.main_world == [] and other_site.main_world == []


def test_bridge_reports_a_frame_it_cant_find(qapp, tmp_path):
    b, got = _tron_bridge(tmp_path, [_Page(_Frame(_Url("https", "dapp.example")))])
    b.loadTronWeb("cid", "tok", "https://dapp.example")
    assert got and got[0][:2] == ("cid", False)
    b.loadTronWeb("cid2", "tok", "https://nowhere.example")
    assert got[1][:2] == ("cid2", False)


def test_bridge_reports_a_missing_bundle(qapp, tmp_path):
    mod = _load_module("bridge.py")
    b = mod.QethBridge(views=lambda: [], tronweb_path=str(tmp_path / "nope.js"))
    got = []
    b.tronWebLoaded.connect(lambda cid, ok, err: got.append((cid, ok, err)))
    b.loadTronWeb("cid", "tok", "")
    assert got == [("cid", False, "TronWeb is missing from the qeth connector")]


def test_origin_of_matches_window_location_origin():
    mod = _load_module("bridge.py")
    assert mod._origin_of(_Url("https", "a.example")) == "https://a.example"
    assert mod._origin_of(_Url("http", "localhost", 3000)) == "http://localhost:3000"


def test_falkon_relay_asks_the_bridge_with_a_private_token():
    relay = (FALKON / "relay.js").read_text()
    assert 'd.kind === "tronweb"' in relay
    # The token lives in the SafeJsWorld, where no page script can set it.
    assert "window.__qethTronToken = cidGen();" in relay
    assert "bridge.loadTronWeb(cid, window.__qethTronToken" in relay
    assert "bridge.tronWebLoaded.connect" in relay


def test_falkon_ships_the_same_tronweb_as_the_extension():
    webext = FALKON.parent.parent / "webext" / "tronweb"
    for name in ("TronWeb.js", "TronWeb.js.LICENSE.txt", "LICENSE", "SOURCE.txt"):
        assert (FALKON / "tronweb" / name).read_bytes() == (webext / name).read_bytes(), name


class _DeadPage:
    """A page whose C++ side Qt already deleted — what a real Falkon's view
    list hands back for a replaced page / a closed tab awaiting deleteLater."""

    def mainFrame(self):  # noqa: N802
        raise RuntimeError(
            "libshiboken: Internal C++ object (PyFalkon.WebPage) already deleted.")


def test_bridge_skips_pages_qt_already_deleted(qapp, tmp_path):
    """The real-Falkon failure: one dead page made loadTronWeb raise — and an
    exception in a web-channel slot is swallowed, so the dapp spun forever."""
    asking = _Frame(_Url("https", "sun.io"), token="tok")
    b, got = _tron_bridge(tmp_path, [_DeadPage(), _Page(asking)])
    b.loadTronWeb("cid", "tok", "https://sun.io")
    assert got == [("cid", True, "")] and asking.main_world == ["/* tronweb */"]


class _SilentFrame(_Frame):
    def runJavaScript(self, src, world, callback):  # noqa: N802
        pass                                        # torn down: never calls back


def test_bridge_answers_even_when_a_frame_never_does(qtbot, tmp_path):
    mod = _load_module("bridge.py")
    mod._PROBE_TIMEOUT_MS = 50
    tw = tmp_path / "TronWeb.js"
    tw.write_text("x")
    page = _Page(_SilentFrame(_Url("https", "sun.io")))
    b = mod.QethBridge(views=lambda: [_View(page)], tronweb_path=str(tw))
    got = []
    b.tronWebLoaded.connect(lambda cid, ok, err: got.append((cid, ok)))
    b.loadTronWeb("cid", "tok", "https://sun.io")
    qtbot.waitUntil(lambda: got == [("cid", False)], timeout=2000)


def test_bridge_turns_any_error_into_an_answer(qapp, tmp_path):
    def boom():
        raise ValueError("surprise")
    mod = _load_module("bridge.py")
    tw = tmp_path / "TronWeb.js"
    tw.write_text("x")
    b = mod.QethBridge(views=boom, tronweb_path=str(tw))
    got = []
    b.tronWebLoaded.connect(lambda cid, ok, err: got.append((cid, ok, err)))
    b.loadTronWeb("cid", "tok", "")
    assert got == [("cid", False, "qeth couldn't load TronWeb: surprise")]


class _LateFrame(_Frame):
    """Answers the token probe LATER (as QtWebEngine does), and — PyFalkon's
    quirk — reads as deleted once its view's wrapper has been collected."""

    def __init__(self, url, token):
        super().__init__(url, token)
        self.pending: list = []
        self.view = None

    def runJavaScript(self, src, world, callback):  # noqa: N802
        if self.view() is None:
            raise RuntimeError("Internal C++ object (PyFalkon.WebPage) already deleted.")
        if world == 1:
            self.pending.append(lambda: callback(src.endswith('"%s"' % self.token)))
        else:
            self.main_world.append(src)
            callback(None)


def test_bridge_keeps_the_views_alive_until_it_answers(qapp, tmp_path):
    """The live-Falkon bug: holding only the PAGES let their views' wrappers be
    collected, which killed the page wrappers mid-load."""
    import gc
    import weakref
    mod = _load_module("bridge.py")
    tw = tmp_path / "TronWeb.js"
    tw.write_text("x")
    frame = _LateFrame(_Url("https", "sun.io"), token="tok")

    def views():
        view = _View(_Page(frame))
        frame.view = weakref.ref(view)
        return [view]
    b = mod.QethBridge(views=views, tronweb_path=str(tw))
    got = []
    b.tronWebLoaded.connect(lambda cid, ok, err: got.append((cid, ok, err)))
    b.loadTronWeb("cid", "tok", "https://sun.io")
    gc.collect()                      # nothing but the bridge holds the view now
    frame.pending.pop()()             # the probe's answer arrives
    assert got == [("cid", True, "")] and frame.main_world == ["x"]
    gc.collect()                      # …and it lets go once it has answered
    assert frame.view() is None
