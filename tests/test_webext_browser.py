"""Opt-in Selenium harness: load the real MV3 extension (integrations/webext/)
into headless Chromium AND Firefox and exercise the IN-PAGE provider against a
stub ``RpcServer`` — the layer test_webext_protocol.py can't reach (it drives the
WS protocol with a fake background, no browser).

Marked ``browser`` and skipped by default (see pyproject ``addopts``). Selenium
lives in the opt-in ``webext`` dependency group, so run:

    uv sync --group webext
    uv run pytest -m browser -v

Non-negotiables (verified against the extension source):
  * background.js hardcodes ``ws://127.0.0.1:1248`` (manifest CSP too) → the stub
    server MUST bind 1248; if it's busy (a real qeth running) the module skips.
  * the test page is served over ``http://`` — host_permissions exclude file://
    and Chrome gates file access behind a manual per-extension toggle.
  * Selenium Manager is bypassed (explicit ``Service`` off the PATH driver) so no
    driver download reaches out over the network (the conftest guard would refuse
    it — these tests aren't ``network``-marked; the browser subprocess itself is
    invisible to that in-process guard).
"""

import shutil
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("selenium")  # opt-in `webext` group; skip the module if absent

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.support.ui import WebDriverWait

from qeth.rpc import RpcServer

pytestmark = pytest.mark.browser

EXT_DIR = Path(__file__).resolve().parents[1] / "integrations" / "webext"
ACCOUNT = "0x" + "11" * 20


# --- the tiny test dapp (served over http, in memory) ------------------------

_CAPTURE = """
window.__announced = [];
window.addEventListener("eip6963:announceProvider", function (e) {
  window.__announced.push({
    info: e.detail && e.detail.info,
    hasProvider: !!(e.detail && e.detail.provider),
  });
});
window.dispatchEvent(new Event("eip6963:requestProvider"));
"""

INDEX_HTML = (
    "<!doctype html><meta charset=utf-8><title>qeth-test-dapp</title>"
    "<script>" + _CAPTURE + "</script>"
    "<iframe src='/frame.html' style='width:80px;height:80px'></iframe>"
)
FRAME_HTML = (
    "<!doctype html><meta charset=utf-8><title>frame</title>"
    "<script>" + _CAPTURE + "</script>"
)


# --- stub server (mirror tests/test_webext_protocol.py:33-39) ----------------

def _store():
    chains = [SimpleNamespace(chain_id=1), SimpleNamespace(chain_id=10)]
    return SimpleNamespace(
        current_chain=lambda: chains[0],
        chains=chains,
        default_account=ACCOUNT,
    )


class _RpcHandle:
    """Start/stop wrapper so the reconnect test can restart the server (retrying
    past TIME_WAIT) without disturbing the module fixture or the other browser."""

    def __init__(self):
        self.server = None

    def start(self, tries=1, delay=0.5):
        err = None
        for i in range(tries):
            server = RpcServer(_store(), port=1248)
            server.start()
            err = server._error
            if err is None:
                self.server = server
                return None
            server.stop()                       # join the failed-to-bind thread
            if i + 1 < tries:
                time.sleep(delay)
        return err

    def stop(self):
        if self.server is not None:
            try:
                self.server.stop()
            finally:
                self.server = None


# --- fixtures ----------------------------------------------------------------

@pytest.fixture(scope="module")
def rpc():
    handle = _RpcHandle()
    err = handle.start()
    if err is not None:
        pytest.skip(f"cannot bind 127.0.0.1:1248 (a real qeth is likely "
                    f"running): {err}")
    yield handle
    handle.stop()


@pytest.fixture(scope="module")
def dapp_url():
    pages = {"/": INDEX_HTML, "/frame.html": FRAME_HTML}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server method name
            body = pages.get(self.path.split("?")[0])
            if body is None:
                self.send_error(404)
                return
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # noqa: N802 — silence request logging
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _chromium_bin():
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _make_chromium(profile_dir):
    opts = ChromeOptions()
    opts.binary_location = _chromium_bin()
    opts.add_argument("--headless=new")            # old headless ignores extensions
    opts.add_argument(f"--load-extension={EXT_DIR}")
    opts.add_argument(f"--disable-extensions-except={EXT_DIR}")
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-background-networking")
    opts.add_argument("--disable-component-update")
    # If a future Chromium disables the switch, add:
    #   --disable-features=DisableLoadExtensionCommandLineSwitch
    service = ChromeService(executable_path=shutil.which("chromedriver"))
    return webdriver.Chrome(options=opts, service=service)


def _make_firefox():
    opts = FirefoxOptions()
    opts.binary_location = shutil.which("firefox")
    opts.add_argument("-headless")
    # MV3 host permissions are user-grantable on Firefox; this pref (default true
    # on this build, per omni.ja) grants them so content scripts inject. The
    # readiness probe below turns a failure here into one clean skip.
    opts.set_preference("extensions.originControls.grantByDefault", True)
    opts.set_preference("dom.security.https_only_mode", False)  # keep the http origin
    opts.set_preference("datareporting.policy.dataSubmissionEnabled", False)
    opts.set_preference("app.update.disabledForTesting", True)
    opts.set_preference("browser.shell.checkDefaultBrowser", False)
    service = FirefoxService(executable_path=shutil.which("geckodriver"))
    drv = webdriver.Firefox(options=opts, service=service)
    drv.install_addon(str(EXT_DIR), temporary=True)   # selenium>=4.20 zips a dir
    return drv


@pytest.fixture(scope="module", params=["chromium", "firefox"])
def driver(request, rpc, dapp_url, tmp_path_factory):
    which = request.param
    if which == "chromium":
        if not shutil.which("chromedriver") or _chromium_bin() is None:
            pytest.skip("chromedriver / a chromium binary not on PATH")
        drv = _make_chromium(tmp_path_factory.mktemp("chrome-profile"))
    else:
        if not shutil.which("geckodriver") or not shutil.which("firefox"):
            pytest.skip("geckodriver / firefox not on PATH")
        drv = _make_firefox()
    try:
        # Readiness probe: provider.js always sets window.qeth once its content
        # script injects. Firefox failing this means the MV3 host grant didn't
        # take on this build → a clean per-param skip (Chromium is unaffected).
        drv.get(dapp_url + "/")
        try:
            _wait_js(drv, "window.qeth", timeout=15)
        except TimeoutException:
            if which == "firefox":
                pytest.skip("Firefox did not inject the webext content scripts "
                            "(origin-controls grant ineffective on this build) "
                            "— run the manual matrix for Firefox")
            raise
        yield drv
    finally:
        drv.quit()


@pytest.fixture
def page(driver, rpc, dapp_url):
    driver.switch_to.default_content()                 # defensive after an iframe test
    rpc.server._rpc_chain_id_by_origin.clear()          # full server-side per-origin reset
    driver.get(dapp_url + "/")                          # fresh provider instance + gates
    _wait_js(driver, "window.qeth", timeout=15)
    return driver


# --- driving helpers ---------------------------------------------------------

_REQUEST_JS = """
const cb = arguments[arguments.length - 1];
const method = arguments[0], params = arguments[1];
if (!window.ethereum) { cb({ok: null, err: {code: 0, message: "no window.ethereum"}}); return; }
window.ethereum.request({method: method, params: params || []})
  .then(r => cb({ok: (r === undefined ? null : r), err: null}))
  .catch(e => cb({ok: null, err: {code: (e && e.code) != null ? e.code : null,
                                  message: String((e && e.message) || e)}}));
"""


def _request(driver, method, params=None, timeout=15):
    """Call window.ethereum.request(...) and return {ok, err:{code,message}}."""
    driver.set_script_timeout(timeout)
    return driver.execute_async_script(_REQUEST_JS, method, params or [])


def _js(driver, expr):
    return driver.execute_script("return (" + expr + ")")


def _wait_js(driver, expr, timeout=10):
    WebDriverWait(driver, timeout, poll_frequency=0.1).until(
        lambda d: d.execute_script("return !!(" + expr + ")"))


# --- tests (each runs for both browser params) -------------------------------

def test_eip6963_announce(page):
    _wait_js(page, "window.__announced.length >= 1")
    info = _js(page, "window.__announced[0].info")
    assert info["name"] == "qeth"
    assert info["rdns"] == "org.qeth"
    assert info["icon"].startswith("data:image/svg+xml;base64,")
    uuid.UUID(info["uuid"])                              # a valid uuid
    assert _js(page, "window.__announced[0].hasProvider") is True
    # re-announces on every eip6963:requestProvider
    page.execute_script("window.dispatchEvent(new Event('eip6963:requestProvider'))")
    _wait_js(page, "window.__announced.length >= 2")


def test_window_ethereum_top_frame(page):
    state = _js(page, "({present: !!window.ethereum, "
                      "isMM: !!(window.ethereum && window.ethereum.isMetaMask === true), "
                      "qeth: !!window.qeth})")
    assert state == {"present": True, "isMM": True, "qeth": True}


def test_rpc_round_trip(page):
    assert _request(page, "eth_chainId")["ok"] == "0x1"
    for m in ("eth_accounts", "eth_requestAccounts"):
        r = _request(page, m)
        assert r["err"] is None
        assert [a.lower() for a in r["ok"]] == [ACCOUNT.lower()]


def test_switch_chain(page):
    page.execute_script("window.__events = []; "
                        "window.ethereum.on('chainChanged', v => window.__events.push(v));")
    r = _request(page, "wallet_switchEthereumChain", [{"chainId": "0xa"}])
    assert r["err"] is None and r["ok"] is None
    _wait_js(page, "window.__events.includes('0xa')")   # push pipeline end-to-end
    assert _request(page, "eth_chainId")["ok"] == "0xa"


def test_switch_chain_unknown(page):
    r = _request(page, "wallet_switchEthereumChain", [{"chainId": "0x539"}])  # 1337
    assert r["err"]["code"] == 4902


def test_personal_sign_unavailable(page):
    r = _request(page, "personal_sign", ["0xdeadbeef", ACCOUNT])
    assert r["err"]["code"] == -32601
    assert "no signer" in r["err"]["message"].lower()


def test_iframe_inert(page):
    page.switch_to.frame(0)
    try:
        _wait_js(page, "window.qeth", timeout=15)
        assert _js(page, "window.__announced.length") == 0        # announce is top-frame only
        assert _js(page, "!!(window.ethereum && window.ethereum.isMetaMask)") is False
        assert _request(page, "eth_accounts")["ok"] == []          # local sub-frame gate
        r = _request(page, "eth_requestAccounts")                  # lifts the gate
        assert [a.lower() for a in r["ok"]] == [ACCOUNT.lower()]
        assert [a.lower() for a in _request(page, "eth_accounts")["ok"]] == [ACCOUNT.lower()]
    finally:
        page.switch_to.default_content()


def test_disconnect_reconnect(page, rpc):
    # Defined last: within a browser param this runs last, and it leaves the
    # shared module-scoped server healthy for the next param.
    rpc.stop()
    r = _request(page, "eth_chainId", timeout=15)
    assert r["err"] and r["err"]["code"] == 4900        # 2s outbox grace → reject
    err = rpc.start(tries=12, delay=0.5)                # ride out TIME_WAIT
    assert err is None, f"could not rebind 1248: {err}"
    deadline = time.time() + 15                          # 5s reconnect cadence + slack
    r = None
    while time.time() < deadline:
        r = _request(page, "eth_chainId", timeout=15)
        if r["ok"] == "0x1":
            break
        time.sleep(0.5)
    assert r is not None and r["ok"] == "0x1", f"did not reconnect: {r}"
