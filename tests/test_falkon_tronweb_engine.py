"""Opt-in (``browser``): the Falkon connector's on-demand TronWeb in a REAL
QtWebEngine — ``QethBridge.loadTronWeb`` finds the asking frame by the token
its relay holds in the SafeJsWorld, and runs the bundled TronWeb in that
frame's main world, past a ``default-src 'none'`` CSP, leaving the others
alone. Runs the engine in a subprocess (QtWebEngine has to load before the
test process's QApplication exists).

    uv run pytest -m browser tests/test_falkon_tronweb_engine.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWebEngineWidgets")

pytestmark = pytest.mark.browser

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = r'''
import importlib.util, json, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import QApplication
from PySide6.QtWebEngineWidgets import QWebEngineView

PAGES = {"/": "<!doctype html><title>t</title><iframe src='/f.html'></iframe>",
         "/f.html": "<!doctype html><title>f</title><p>frame</p>"}
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = PAGES.get(self.path, "").encode()
        self.send_response(200); self.send_header("Content-Type", "text/html")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-src 'self'")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = "http://127.0.0.1:%d" % httpd.server_address[1]

app = QApplication(sys.argv)
spec = importlib.util.spec_from_file_location("fbridge", sys.argv[1])
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
view = QWebEngineView(); view.resize(400, 300); view.show()
bridge = mod.QethBridge()
out = {}
bridge.tronWebLoaded.connect(lambda cid, ok, err: out.update(loaded=[cid, ok, err]))

def finish():
    print("RESULT " + json.dumps(out)); app.quit()

def check(sub):
    def top(r):
        out["top"] = r; finish()
    def got(r):
        out["frame"] = r
        view.page().mainFrame().runJavaScript("typeof window.TronWeb", 0, top)
    sub.runJavaScript("typeof (window.TronWeb && window.TronWeb.TronWeb)", 0, got)

def ask(sub):
    bridge.loadTronWeb("cid-1", "tok-1", url)
    QTimer.singleShot(3000, lambda: check(sub))

def start():
    sub = view.page().mainFrame().children()[0]
    # What the relay does in its SafeJsWorld (ApplicationWorld = 1).
    sub.runJavaScript("window.__qethTronToken = 'tok-1'", 1, lambda _r: ask(sub))

view.loadFinished.connect(lambda ok: QTimer.singleShot(300, start))
view.load(QUrl(url + "/"))
QTimer.singleShot(20000, finish)
app.exec()
'''


def test_bridge_injects_tronweb_into_the_asking_frame_past_csp(tmp_path):
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen",
           "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox"}
    script = tmp_path / "engine.py"
    script.write_text(SCRIPT)
    bridge = ROOT / "extensions" / "falkon" / "qeth_connector" / "bridge.py"
    proc = subprocess.run([sys.executable, str(script), str(bridge)],
                          capture_output=True, text=True, timeout=90, env=env)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, proc.stdout[-2000:] + proc.stderr[-2000:]
    out = json.loads(lines[-1][len("RESULT "):])
    assert out == {"loaded": ["cid-1", True, ""], "frame": "function", "top": "undefined"}
