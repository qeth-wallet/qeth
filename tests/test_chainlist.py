"""Hermetic tests for the eth_simulateV1 capability probe and the
RPC-picker's capability indication. Network is mocked — no live calls."""

import json
import urllib.error

from qeth.chainlist import probe_access_list, probe_simulate_v1


def _resp(obj):
    body = json.dumps(obj).encode()

    class R:
        def read(self): return body
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return R()


def _patch_urlopen(monkeypatch, fn):
    monkeypatch.setattr("qeth.chainlist.urllib.request.urlopen", fn)


class TestProbeSimulateV1:
    URL = "https://rpc.example/eth"

    def test_supported_returns_true(self, monkeypatch):
        _patch_urlopen(monkeypatch, lambda req, timeout=None: _resp(
            {"jsonrpc": "2.0", "id": 1,
             "result": [{"calls": [{"status": "0x1", "logs": []}]}]}))
        assert probe_simulate_v1(self.URL) is True

    def test_method_not_found_returns_false(self, monkeypatch):
        _patch_urlopen(monkeypatch, lambda req, timeout=None: _resp(
            {"jsonrpc": "2.0", "id": 1,
             "error": {"code": -32601, "message": "the method does not exist"}}))
        assert probe_simulate_v1(self.URL) is False

    def test_zksync_not_whitelisted_returns_false(self, monkeypatch):
        _patch_urlopen(monkeypatch, lambda req, timeout=None: _resp(
            {"error": {"code": -32601, "message": "rpc method is not whitelisted"}}))
        assert probe_simulate_v1(self.URL) is False

    def test_minus_32601_riding_on_http_400_returns_false(self, monkeypatch):
        # DRPC returns the -32601 envelope under an HTTP 400.
        def raise400(req, timeout=None):
            body = json.dumps(
                {"error": {"code": -32601, "message": "method not available"}}
            ).encode()
            raise urllib.error.HTTPError(self.URL, 400, "Bad Request", {},
                                         _Body(body))
        _patch_urlopen(monkeypatch, raise400)
        assert probe_simulate_v1(self.URL) is False

    def test_rate_limit_is_unknown_not_false(self, monkeypatch):
        # A 429 / -32005 must not be mistaken for 'unsupported'.
        _patch_urlopen(monkeypatch, lambda req, timeout=None: _resp(
            {"error": {"code": -32005, "message": "rate limit exceeded"}}))
        assert probe_simulate_v1(self.URL) is None

    def test_network_error_is_unknown(self, monkeypatch):
        def boom(req, timeout=None):
            raise OSError("connection refused")
        _patch_urlopen(monkeypatch, boom)
        assert probe_simulate_v1(self.URL) is None

    def test_non_http_url_is_unknown(self):
        assert probe_simulate_v1("wss://rpc.example") is None


class _Body:
    """Minimal file-like for HTTPError.read()."""
    def __init__(self, data): self._d = data
    def read(self): return self._d
    def close(self): pass


class TestProbeAccessList:
    URL = "https://rpc.example/eth"

    def test_supported_returns_true(self, monkeypatch):
        _patch_urlopen(monkeypatch, lambda req, timeout=None: _resp(
            {"jsonrpc": "2.0", "id": 1,
             "result": {"accessList": [], "gasUsed": "0x5208"}}))
        assert probe_access_list(self.URL) is True

    def test_method_not_found_returns_false(self, monkeypatch):
        _patch_urlopen(monkeypatch, lambda req, timeout=None: _resp(
            {"error": {"code": -32601, "message": "method not found"}}))
        assert probe_access_list(self.URL) is False

    def test_rate_limit_is_unknown_not_false(self, monkeypatch):
        _patch_urlopen(monkeypatch, lambda req, timeout=None: _resp(
            {"error": {"code": -32005, "message": "rate limit exceeded"}}))
        assert probe_access_list(self.URL) is None

    def test_probe_sends_no_fee_fields(self, monkeypatch):
        """The probe call must carry NO gasPrice/maxFeePerGas: an explicit
        0 is rejected post-London ("gasprice must be non-zero after london
        fork") and a priced probe trips balance validation on some nodes —
        both would misread as 'unsupported'. An explicit small gas cap is
        required for the same reason (nodes default the cap to the block
        limit and price THAT)."""
        captured = {}

        def capture(req, timeout=None):
            captured.update(json.loads(req.data))
            return _resp({"result": {"accessList": [], "gasUsed": "0x0"}})

        _patch_urlopen(monkeypatch, capture)
        assert probe_access_list(self.URL) is True
        call = captured["params"][0]
        assert "gasPrice" not in call
        assert "maxFeePerGas" not in call
        assert "value" not in call          # zero-value: no balance needed
        assert int(call["gas"], 16) <= 200_000   # small explicit cap


class TestPickerIndication:
    def test_format_row_tags_simv1(self):
        from qeth.chain_rpc_dialog import ChainRpcDialog
        f = ChainRpcDialog._format_row
        assert "⚡sim" in f("https://x", True, 42.0, True)
        assert "⚡sim" not in f("https://x", True, 42.0, False)   # no badge
        assert "⚡sim" not in f("https://x", True, 42.0, None)    # unknown
        # Columns stay aligned: the tag slot is fixed-width either way.
        a = f("https://x", True, 42.0, True)
        b = f("https://x", True, 42.0, False)
        assert a.index("https://x") == b.index("https://x")

    def test_simv1_endpoints_sort_first(self, qtbot, tmp_qeth, monkeypatch):
        from qeth.chain_rpc_dialog import (
            ChainRpcDialog, _ChainlistLoader, _UrlProbeWorker)
        from qeth.chains import DEFAULT_CHAINS
        from PySide6.QtCore import Qt
        # Stop the dialog's constructor from spawning the live-probe threads:
        # this test drives the sort/render path directly via _results +
        # _on_probing_done. The loader is DETACHED (parented to the
        # QApplication so the dialog can close instantly), so a real probe
        # run would hit the network for all 16 RPCs and — still in flight at
        # interpreter shutdown — hang the process joining it. No-op start().
        # The constructor also kicks a _UrlProbeWorker at the pre-filled
        # current RPC — same treatment.
        monkeypatch.setattr(_ChainlistLoader, "start", lambda self: None)
        monkeypatch.setattr(_UrlProbeWorker, "start", lambda self: None)
        dlg = ChainRpcDialog(DEFAULT_CHAINS[0])
        qtbot.addWidget(dlg)
        # A fast endpoint without simV1 and a slower one with it: the
        # simV1 one must rank first despite higher latency.
        dlg._results = {
            "https://fast-no-sim": (20.0, True, False),
            "https://slow-sim": (200.0, True, True),
            "https://dead": (None, False, None),
        }
        dlg._on_probing_done()
        order = [dlg.picker.item(i).data(Qt.UserRole)
                 for i in range(dlg.picker.count())]
        assert order == ["https://slow-sim", "https://fast-no-sim"]
        assert "⚡sim" in dlg.picker.item(0).text()


# --- probe_rpc against a real loopback server ---------------------------------

import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import qeth.chainlist as cl
from qeth.chainlist import ChainlistUnavailable, fetch_chains, probe_rpc


class _RpcHandler(BaseHTTPRequestHandler):
    """Answers eth_chainId per path; records each request's client port so a
    test can tell whether the second request reused the connection."""
    protocol_version = "HTTP/1.1"      # keep-alive

    def log_message(self, *a):          # quiet
        pass

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.ports.append((self.path, self.client_address[1]))
        path = self.path
        close = path == "/close"
        if path == "/html403":
            self._reply(403, b"<html>forbidden</html>", "text/html")
        elif path == "/rpcerr400":
            self._reply(400, json.dumps({"jsonrpc": "2.0", "id": 1, "error": {
                "code": -32000, "message": "invalid api key"}}).encode())
        else:
            cid = "0x89" if path == "/polygon" else "0x1"
            self._reply(200, json.dumps(
                {"jsonrpc": "2.0", "id": 1, "result": cid}).encode(),
                close=close)

    def _reply(self, code, body, ctype="application/json", close=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def rpc_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _RpcHandler)
    srv.ports = []
    t = threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True)
    t.start()
    yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


class TestProbeRpc:
    def test_matching_chain_is_ok_and_timed_on_a_reused_connection(self, rpc_server):
        srv, base = rpc_server
        ok, latency, reason = probe_rpc(base + "/eth", 1)
        assert (ok, reason) == (True, None)
        assert latency is not None and latency >= 0
        # Two requests, ONE connection: the reported figure is the warm call.
        assert len(srv.ports) == 2
        assert srv.ports[0][1] == srv.ports[1][1]

    def test_a_server_that_closes_still_probes_ok(self, rpc_server):
        srv, base = rpc_server
        ok, latency, _ = probe_rpc(base + "/close", 1)
        assert ok and latency is not None
        assert srv.ports[0][1] != srv.ports[1][1]     # reconnected

    def test_wrong_chain(self, rpc_server):
        _, base = rpc_server
        ok, latency, reason = probe_rpc(base + "/polygon", 1)
        assert not ok and reason == "chain mismatch (137)"
        assert latency is not None

    def test_auth_wall_reports_the_http_status(self, rpc_server):
        _, base = rpc_server
        assert probe_rpc(base + "/html403", 1)[::2] == (False, "HTTP 403")

    def test_json_rpc_error_on_a_4xx_reports_the_node_message(self, rpc_server):
        _, base = rpc_server
        ok, _, reason = probe_rpc(base + "/rpcerr400", 1)
        assert not ok and reason == "rpc error: invalid api key"

    def test_refused_connection(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                              # nothing listens there now
        assert probe_rpc(f"http://127.0.0.1:{port}/", 1) == (
            False, None, "connection refused")

    def test_untrusted_certificate_says_so(self, tmp_path):
        """A TLS trust failure must read as one, not as a generic 'not
        reachable' — the case a Mac report couldn't distinguish."""
        cert, key = _self_signed(tmp_path)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _RpcHandler)
        srv.ports = []
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        t = threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True)
        t.start()
        try:
            ok, latency, reason = probe_rpc(
                f"https://127.0.0.1:{srv.server_address[1]}/", 1)
        finally:
            srv.shutdown()
            srv.server_close()
        assert (ok, latency) == (False, None)
        assert reason is not None and reason.startswith("TLS: ")
        assert "self-signed" in reason or "self signed" in reason


def _self_signed(tmp_path):
    x509 = pytest.importorskip("cryptography.x509")
    import datetime
    import ipaddress
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.UTC)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
         .public_key(k.public_key()).serial_number(1)
         .not_valid_before(now - datetime.timedelta(days=1))
         .not_valid_after(now + datetime.timedelta(days=1))
         .add_extension(x509.SubjectAlternativeName(
             [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
         .sign(k, hashes.SHA256()))
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(k.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    return str(cert), str(key)


class TestFetchChains:
    _REGISTRY = json.dumps([{"chainId": 1, "name": "Ethereum Mainnet",
                             "rpc": ["https://rpc.example"]}]).encode()

    def test_download_failure_without_cache_raises_a_short_reason(
            self, monkeypatch, tmp_path):
        def timeout(req, timeout=None):
            raise urllib.error.URLError(TimeoutError("timed out"))
        _patch_urlopen(monkeypatch, timeout)
        with pytest.raises(ChainlistUnavailable, match="^timed out$"):
            fetch_chains(cache_dir=tmp_path)

    def test_download_failure_falls_back_to_the_cache(self, monkeypatch, tmp_path):
        (tmp_path / "chains.json").write_bytes(self._REGISTRY)
        def boom(req, timeout=None):
            raise urllib.error.URLError(OSError("down"))
        _patch_urlopen(monkeypatch, boom)
        [entry] = fetch_chains(cache_dir=tmp_path, force=True)
        assert entry.chain_id == 1

    def test_a_download_is_used_even_if_it_cannot_be_cached(
            self, monkeypatch, tmp_path):
        _patch_urlopen(monkeypatch, lambda req, timeout=None: _Raw(self._REGISTRY))
        def no_write(path, data, **kw):
            raise OSError("read-only file system")
        monkeypatch.setattr(cl, "atomic_write_bytes", no_write)
        [entry] = fetch_chains(cache_dir=tmp_path)
        assert entry.rpcs[0].url == "https://rpc.example"


class _Raw:
    def __init__(self, data): self._d = data
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False
