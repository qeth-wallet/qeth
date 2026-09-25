"""Opt-in: qeth's Tron provider end to end in real headless Chromium AND
Firefox — the extension, the bundled TronWeb loaded on demand into the page,
the real ``RpcServer`` (its ``tron_*`` methods and node proxy) in front of a
FAKE Tron node, and a stub signer standing in for the review dialog (it signs
with a test key, so TronWeb's own verifiers can check the results).

Marked ``browser`` and skipped by default, like tests/test_webext_browser.py:

    uv sync --group webext
    uv run pytest -m browser tests/test_webext_tron_browser.py -v

Unlike that module this one doesn't need port 1248: it materialises the
extension with its loopback port rewritten to a free one (background.js's
socket URL and the manifest CSP — the only two places it's spelled), so it
runs beside a live qeth.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("selenium")

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.support.ui import WebDriverWait

from qeth.address import tron_from_hex
from qeth.chains import DEFAULT_CHAINS, EVM, TRON, Chain
from qeth.rpc import RpcServer
from qeth.signing import (
    TronMessageSigningRequest, TronSigningRequest, TronTypedDataSigningRequest,
)
from qeth.tron.tx import signature_v27

pytestmark = pytest.mark.browser

EXT_DIR = Path(__file__).resolve().parents[1] / "extensions" / "webext"
_PRIV = bytes.fromhex("4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318")


def _account_hex() -> str:
    from eth_keys import keys
    return "0x" + keys.PrivateKey(_PRIV).public_key.to_canonical_address().hex()


ACCOUNT = _account_hex()
ACCOUNT_B58 = tron_from_hex(ACCOUNT)
OTHER = "0x" + "22" * 20
TO_B58 = tron_from_hex("0x" + "33" * 20)
TRON_CHAIN = next(c for c in DEFAULT_CHAINS if c.family == TRON)


def _load_build():
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("wxbuild", EXT_DIR / "build.py")
    mod = importlib.util.module_from_spec(spec)
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = prev
    return mod


BUILD = _load_build()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- a fake Tron node -----------------------------------------------------------

BLOCK = {"blockID": "0000000003b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091",
         "block_header": {"raw_data": {"number": 61981395, "timestamp": 1790000000000}}}


class FakeTronNode:
    """Answers the full-node paths TronWeb uses in these tests; records every
    request (path, body) and every broadcast."""

    def __init__(self):
        self.requests: list[tuple[str, dict]] = []
        self.broadcasts: list[dict] = []
        node = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, body: dict) -> None:
                path = self.path.split("?")[0]
                node.requests.append((path, body))
                if path in ("/wallet/getblock", "/wallet/getnowblock",
                            "/walletsolidity/getblock", "/walletsolidity/getnowblock"):
                    out = BLOCK
                elif path in ("/wallet/getaccount", "/walletsolidity/getaccount"):
                    out = {"address": body.get("address", ""), "balance": 12345678}
                elif path == "/wallet/broadcasttransaction":
                    node.broadcasts.append(body)
                    out = {"result": True, "txid": body.get("txID")}
                else:
                    out = {}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                self._answer(json.loads(raw) if raw else {})

            def do_GET(self):  # noqa: N802
                self._answer({})

            def log_message(self, *a):  # noqa: N802
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


# --- the stub signer (the review dialog's stand-in) -----------------------------

class StubBridge:
    """``SignerBridge``'s surface as RpcServer uses it: signs every request
    with the test key, as a confirmed review would."""

    def __init__(self):
        self.requests: list = []

    async def submit_async(self, req):
        from eth_keys import keys
        self.requests.append(req)
        if isinstance(req, TronSigningRequest):
            digest = req.tx.txid()
        elif isinstance(req, (TronMessageSigningRequest, TronTypedDataSigningRequest)):
            digest = req.digest()
        else:
            raise AssertionError(f"unexpected request {req!r}")
        sig = signature_v27(keys.PrivateKey(_PRIV).sign_msg_hash(digest).to_bytes())
        return sig.hex() if isinstance(req, TronSigningRequest) else "0x" + sig.hex()


# --- the test dapp ----------------------------------------------------------------

_CAPTURE = """
window.__tip6963 = [];
window.addEventListener("TIP6963:announceProvider", function (e) {
  window.__tip6963.push({ info: e.detail && e.detail.info, hasProvider: !!(e.detail && e.detail.provider) });
});
window.dispatchEvent(new Event("TIP6963:requestProvider"));
window.__tronwebAsks = 0;
window.addEventListener("message", function (e) {
  var d = e.data;
  if (d && d.source === "qeth-provider" && d.kind === "tronweb") window.__tronwebAsks++;
});
window.__accountsEvents = [];
if (window.tron) window.tron.on("accountsChanged", function (a) { window.__accountsEvents.push(a); });
"""

PAGES = {
    "/": "<!doctype html><title>tron</title><script>" + _CAPTURE + "</script>"
         "<iframe src='/frame.html' style='width:40px;height:40px'></iframe>",
    "/frame.html": "<!doctype html><title>frame</title><script>" + _CAPTURE + "</script>",
    "/own.html": "<!doctype html><title>own</title><script>window.TronWeb = {mine: 1};"
                 + _CAPTURE + "</script>",
    "/csp.html": "<!doctype html><title>csp</title><script>" + _CAPTURE + "</script>",
}
# A strict page: no eval, no network at all from the page itself.
CSP = "default-src 'none'; script-src 'unsafe-inline'"


@pytest.fixture(scope="module")
def dapp_url():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            body = PAGES.get(path)
            if body is None:
                self.send_error(404)
                return
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if path == "/csp.html":
                self.send_header("Content-Security-Policy", CSP)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # noqa: N802
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def backend():
    node = FakeTronNode()
    tron = dataclasses.replace(TRON_CHAIN, api_url=node.url, api_fallbacks=())
    eth = Chain("Ethereum", 1, "http://127.0.0.1:9/", "ETH", "")
    defaults = {EVM: "0x" + "11" * 20, TRON: ACCOUNT}
    selected = {"chain": eth}           # the network "selected in qeth's UI"
    store = SimpleNamespace(
        chains=[eth, tron], current_chain=lambda: selected["chain"],
        dapp_chain=lambda: eth,
        default_for=lambda fam: (defaults.get(fam), None),     # Store's (address, path)
    )
    store.default_account = defaults[EVM]
    bridge = StubBridge()
    port = _free_port()
    server = RpcServer(store, port=port, signer_bridge=bridge)
    server.start()
    if server._error is not None:
        pytest.skip(f"couldn't start the stub RpcServer: {server._error}")
    yield SimpleNamespace(server=server, node=node, bridge=bridge, port=port,
                          defaults=defaults, selected=selected, tron=tron, eth=eth)
    server.stop()
    node.close()


def _materialise(dest: Path, target: str, port: int) -> Path:
    """The extension for ``target`` with its wallet port rewritten."""
    BUILD.write_unpacked(dest, target)
    for rel in ("background.js", "manifest.json"):
        f = dest / rel
        text = f.read_text()
        assert "127.0.0.1:1248" in text
        f.write_text(text.replace("127.0.0.1:1248", f"127.0.0.1:{port}"))
    return dest


def _chromium_bin():
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if shutil.which(name):
            return shutil.which(name)
    return None


@pytest.fixture(scope="module", params=["chromium", "firefox"])
def driver(request, backend, dapp_url, tmp_path_factory):
    if request.param == "chromium":
        if not shutil.which("chromedriver") or _chromium_bin() is None:
            pytest.skip("chromedriver / chromium not on PATH")
        ext = _materialise(tmp_path_factory.mktemp("cr-ext"), "chrome", backend.port)
        opts = ChromeOptions()
        opts.binary_location = _chromium_bin()
        for arg in ("--headless=new", f"--load-extension={ext}",
                    f"--disable-extensions-except={ext}",
                    f"--user-data-dir={tmp_path_factory.mktemp('cr-profile')}",
                    "--no-first-run", "--no-default-browser-check",
                    "--disable-background-networking", "--disable-component-update"):
            opts.add_argument(arg)
        drv = webdriver.Chrome(options=opts, service=ChromeService(
            executable_path=shutil.which("chromedriver")))
        # An unpacked extension's id: its path's sha256, as letters a–p.
        drv.qeth_popup = "chrome-extension://{}/popup.html".format("".join(
            chr(ord("a") + int(c, 16))
            for c in hashlib.sha256(str(ext).encode()).hexdigest()[:32]))
    else:
        if not shutil.which("geckodriver") or not shutil.which("firefox"):
            pytest.skip("geckodriver / firefox not on PATH")
        ext = _materialise(tmp_path_factory.mktemp("fx-ext"), "firefox", backend.port)
        opts = FirefoxOptions()
        opts.binary_location = shutil.which("firefox")
        opts.add_argument("-headless")
        opts.set_preference("extensions.originControls.grantByDefault", True)
        opts.set_preference("dom.security.https_only_mode", False)
        opts.set_preference("datareporting.policy.dataSubmissionEnabled", False)
        opts.set_preference("app.update.disabledForTesting", True)
        drv = webdriver.Firefox(options=opts, service=FirefoxService(
            executable_path=shutil.which("geckodriver")))
        drv.install_addon(str(ext), temporary=True)
    try:
        drv.get(dapp_url + "/")
        try:
            _wait_js(drv, "window.qeth && window.tron", timeout=15)
        except TimeoutException:
            if request.param == "firefox":
                pytest.skip("Firefox didn't inject the content scripts on this build")
            raise
        yield drv
    finally:
        drv.quit()


@pytest.fixture
def page(driver, backend, dapp_url):
    driver.switch_to.default_content()
    backend.defaults[TRON] = ACCOUNT
    backend.bridge.requests.clear()
    driver.get(dapp_url + "/")
    _wait_js(driver, "window.qeth && window.tron", timeout=15)
    return driver


def _wait_js(driver, expr, timeout=10):
    WebDriverWait(driver, timeout, poll_frequency=0.1).until(
        lambda d: d.execute_script("return !!(" + expr + ")"))


_ASYNC = """
const cb = arguments[arguments.length - 1];
(async () => { %s })().then(
  v => cb({ok: v === undefined ? null : v}),
  e => cb({err: {code: (e && e.code != null) ? e.code : null, message: String((e && e.message) || e)}}));
"""


def _run(driver, body: str, timeout: int = 30):
    driver.set_script_timeout(timeout)
    return driver.execute_async_script(_ASYNC % body)


def _js(driver, expr):
    return driver.execute_script("return (" + expr + ")")


# --- tests ---------------------------------------------------------------------------

def test_stub_is_there_and_announced_without_loading_tronweb(page):
    assert _js(page, "window.tron.isTronLink && window.tron.isQeth") is True
    assert _js(page, "!!window.tronLink && window.tronLink.ready === false") is True
    ann = _js(page, "window.__tip6963")
    assert ann and ann[0]["info"]["rdns"] == "org.qeth" and ann[0]["hasProvider"]
    # An untouched page never loads TronWeb.
    assert _js(page, "window.__tronwebAsks") == 0
    assert _js(page, "typeof window.TronWeb") == "undefined"


def test_connect_loads_tronweb_and_the_account(page):
    before = set(_js(page, "Object.getOwnPropertyNames(window)"))
    r = _run(page, "return await window.tron.request({method: 'eth_requestAccounts'});")
    assert r == {"ok": [ACCOUNT_B58]}
    # Loading the library leaves behind only what TronWeb needs at run time:
    # its generated protobuf classes resolve these two globals on every call
    # (TronLink's injected TronWeb leaves the same). Pinned, so a TronWeb bump
    # that leaks more is noticed.
    assert set(_js(page, "Object.getOwnPropertyNames(window)")) - before \
        == {"TronWebProto", "proto"}
    assert _js(page, "window.tronWeb.defaultAddress.base58") == ACCOUNT_B58
    assert _js(page, "window.tronWeb.ready && window.tronLink.ready") is True
    assert _js(page, "window.tron.tronWeb === window.tronWeb") is True
    assert _js(page, "window.__tronwebAsks") == 1
    # The library global is put back as the page had it (absent).
    assert _js(page, "typeof window.TronWeb") == "undefined"
    assert _js(page, "window.__accountsEvents") == [[ACCOUNT_B58]]
    # TronLink's legacy request answers with a {code} object.
    r = _run(page, "return await window.tronLink.request({method: 'tron_requestAccounts'});")
    assert r["ok"]["code"] == 200


def test_node_calls_go_through_qeth(page, backend):
    _run(page, "await window.tron.request({method: 'eth_requestAccounts'});")
    before = len(backend.node.requests)
    r = _run(page, "return await window.tronWeb.trx.getBalance(window.tronWeb.defaultAddress.base58);")
    assert r == {"ok": 12345678}
    # TronWeb reads balances off the solidity API — reached through qeth.
    assert "/walletsolidity/getaccount" in [p for p, _ in backend.node.requests[before:]]


def test_send_trx_is_reviewed_signed_and_broadcast(page, backend):
    _run(page, "await window.tron.request({method: 'eth_requestAccounts'});")
    r = _run(page, f"return await window.tronWeb.trx.sendTransaction('{TO_B58}', 1500000);")
    assert r["ok"]["result"] is True, r
    [req] = [q for q in backend.bridge.requests if isinstance(q, TronSigningRequest)]
    assert req.tx.contract.amount == 1500000
    assert req.tx.contract.to.lower() == "0x" + "33" * 20
    assert req.origin and req.origin.startswith("http://127.0.0.1:")
    # The node got it signed by the account…
    [sent] = backend.node.broadcasts[-1:]
    from qeth.tron.tx import recover_signer
    sig = bytes.fromhex(sent["signature"][0])
    assert recover_signer(bytes.fromhex(sent["txID"]), sig).lower() == ACCOUNT


def test_messages_verify_with_tronwebs_own_verifiers(page):
    _run(page, "await window.tron.request({method: 'eth_requestAccounts'});")
    r = _run(page, """
      const tw = window.tronWeb;
      const v2 = await tw.trx.signMessageV2('hello qeth ✓');
      const v2ok = await tw.trx.verifyMessageV2('hello qeth ✓', v2);
      const hex = '0x' + 'ab'.repeat(32);
      const v1 = await tw.trx.sign(hex);
      const v1ok = await tw.trx.verifyMessage(hex, v1);
      return [v2ok, v1ok];""")
    assert r == {"ok": [ACCOUNT_B58, True]}
    # The Ethereum-header form would be an Ethereum signature: refused.
    r = _run(page, "return await window.tronWeb.trx.sign('0x' + 'ab'.repeat(32), undefined, false);")
    assert r["err"]["code"] == 4200


def test_typed_data_verifies_and_foreign_chains_are_refused(page):
    _run(page, "await window.tron.request({method: 'eth_requestAccounts'});")
    typed = """
      const domain = {name: 'Mail', version: '1', chainId: '0x2b6653dc',
                      verifyingContract: 'TUe6BwpA7sVTDKaJQoia7FWZpC9sK8WM2t'};
      const types = {Mail: [{name: 'to', type: 'address'}, {name: 'id', type: 'trcToken'},
                            {name: 'note', type: 'string'}]};
      const value = {to: '%s', id: '1002000', note: 'hi'};
    """ % TO_B58
    r = _run(page, typed + """
      const tw = window.tronWeb;
      const sig = await tw.trx._signTypedData(domain, types, value);
      return tw.utils.typedData.verifyTypedData(domain, types, value, sig);""")
    assert r["ok"].lower() == ACCOUNT
    r = _run(page, typed + """
      return await window.tronWeb.trx._signTypedData({...domain, chainId: 1}, types, value);""")
    assert r["err"]["code"] == -32602 and "chain 1" in r["err"]["message"]


def test_strict_csp_page(page, dapp_url):
    page.get(dapp_url + "/csp.html")
    _wait_js(page, "window.tron", timeout=15)
    r = _run(page, """
      await window.tron.request({method: 'eth_requestAccounts'});
      return await window.tronWeb.trx.getBalance(window.tronWeb.defaultAddress.base58);""")
    assert r == {"ok": 12345678}


def test_the_pages_own_TronWeb_global_survives(page, dapp_url):  # noqa: N802
    page.get(dapp_url + "/own.html")
    _wait_js(page, "window.tron", timeout=15)
    # The page owns window.TronWeb — and has no window.tronWeb, so ours installs.
    _run(page, "await window.tron.request({method: 'eth_requestAccounts'});")
    assert _js(page, "window.TronWeb && window.TronWeb.mine") == 1
    assert _js(page, "window.tronWeb.defaultAddress.base58") == ACCOUNT_B58


def test_subframe_is_inert_until_connect(page):
    page.switch_to.frame(0)
    try:
        _wait_js(page, "window.tron", timeout=15)
        assert _js(page, "window.tron.isTronLink") is False
        assert _js(page, "window.__tip6963.length") == 0
        assert _run(page, "return await window.tron.request({method: 'eth_accounts'});") == {"ok": []}
        assert _js(page, "window.__tronwebAsks") == 0
        r = _run(page, "return await window.tron.request({method: 'eth_requestAccounts'});")
        assert r == {"ok": [ACCOUNT_B58]}
        assert _js(page, "window.tronWeb.defaultAddress.base58") == ACCOUNT_B58
    finally:
        page.switch_to.default_content()


def test_account_change_reaches_the_page(page, backend):
    _run(page, "await window.tron.request({method: 'eth_requestAccounts'});")
    backend.defaults[TRON] = OTHER
    backend.server.broadcast_tron_accounts_changed([tron_from_hex(OTHER)])
    _wait_js(page, f"window.tronWeb.defaultAddress.base58 === '{tron_from_hex(OTHER)}'",
             timeout=10)
    assert _js(page, "window.__accountsEvents")[-1] == [tron_from_hex(OTHER)]


def test_the_popup_shows_the_selected_network_and_the_site(page, backend, dapp_url):
    """The status popup (Chromium — Firefox's moz-extension id is random): the
    network selected in qeth and ITS account, and what the site is presented."""
    popup = getattr(page, "qeth_popup", None)
    if popup is None:
        pytest.skip("the popup page is reached by id on Chromium only")
    _run(page, "await window.tron.request({method: 'eth_requestAccounts'});")   # a Tron site
    backend.selected["chain"] = backend.tron                                     # qeth on Tron
    try:
        page.get(popup)
        _wait_js(page, "document.getElementById('detail')", timeout=10)
        r = _run(page, """
          const res = await new Promise(ok => chrome.runtime.sendMessage(
            {type: "status", origin: "%s"}, ok));
          showConnected(res);
          return document.getElementById("detail").innerText;""" % dapp_url)
    finally:
        backend.selected["chain"] = backend.eth
    text = r["ok"]
    assert "Network: Tron" in text and ACCOUNT_B58 in text
    assert "0x" + "11" * 20 not in text.lower()          # not the EVM account too
    host = dapp_url.split("//")[1]
    assert f"This site ({host}): Tron · {ACCOUNT_B58[:6]}…{ACCOUNT_B58[-4:]}" in text
