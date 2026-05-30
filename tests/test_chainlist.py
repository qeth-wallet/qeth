"""Hermetic tests for the eth_simulateV1 capability probe and the
RPC-picker's capability indication. Network is mocked — no live calls."""

import json
import urllib.error

from qeth.chainlist import probe_simulate_v1


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

    def test_simv1_endpoints_sort_first(self, qtbot, tmp_qeth):
        from qeth.chain_rpc_dialog import ChainRpcDialog
        from qeth.chains import DEFAULT_CHAINS
        from PySide6.QtCore import Qt
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
