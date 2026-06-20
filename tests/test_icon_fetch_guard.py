"""Defensive icon fetching (qeth.icons._safe_icon_fetch).

A token list's ``logoURI`` is third-party data, so the icon URL is untrusted:
the fetcher must reject non-http(s) schemes (no ``file://`` local reads),
refuse loopback/private hosts (no SSRF of the LAN or the wallet's own
127.0.0.1:1248 RPC), and cap the response body (no memory-DoS). Issue #5.
"""
import io

import pytest

from qeth.icons import _MAX_ICON_BYTES, _is_private_host, _safe_icon_fetch


class TestPrivateHost:
    @pytest.mark.parametrize("host", [
        "localhost", "127.0.0.1", "0.0.0.0", "::1",
        "10.0.0.5", "192.168.1.1", "172.16.3.4",      # RFC1918
        "169.254.1.1",                                # link-local
        "printer.local",                              # mDNS
        "",                                           # no host
    ])
    def test_private_hosts_are_blocked(self, host):
        assert _is_private_host(host)

    @pytest.mark.parametrize("host", [
        "raw.githubusercontent.com", "assets.coingecko.com",
        "8.8.8.8", "example.com",
    ])
    def test_public_hosts_are_allowed(self, host):
        assert not _is_private_host(host)


class TestSafeIconFetch:
    def test_rejects_file_scheme(self):
        with pytest.raises(ValueError, match="non-http"):
            _safe_icon_fetch("file:///etc/passwd")

    def test_rejects_ftp_scheme(self):
        with pytest.raises(ValueError, match="non-http"):
            _safe_icon_fetch("ftp://example.com/x.png")

    def test_rejects_loopback_rpc(self):
        # The poisoned-list-pokes-our-own-RPC case.
        with pytest.raises(ValueError, match="private/loopback"):
            _safe_icon_fetch("http://127.0.0.1:1248/")

    def test_allows_public_url_and_returns_body(self, monkeypatch):
        called = {}

        class _Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            called["url"] = req.full_url
            return _Resp(b"\x89PNG\r\n\x1a\n" + b"x" * 100)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        data = _safe_icon_fetch("https://raw.githubusercontent.com/x/logo.png")
        assert data.startswith(b"\x89PNG")
        assert called["url"].endswith("logo.png")

    def test_caps_an_oversize_body(self, monkeypatch):
        class _Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            # read(cap+1) returns cap+1 bytes → over the limit
            return _Resp(b"x" * (_MAX_ICON_BYTES + 1))

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(ValueError, match="exceeds"):
            _safe_icon_fetch("https://cdn.example.com/huge.png")
