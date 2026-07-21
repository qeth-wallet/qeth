"""Static gates for the qeth browser extension (extensions/webext/).

No node/browser: these parse the manifest and scan the source files to pin the
cross-browser wiring, the shared-provider mirror, and world hygiene. The
extension is dependency-free vanilla JS, so structural checks are the
automated safety net (behaviour is covered by tests/test_webext_protocol.py
end-to-end against the real server, and a manual matrix in the README).
"""

import base64
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBEXT = ROOT / "extensions" / "webext"
FALKON = ROOT / "extensions" / "falkon" / "qeth_connector"
MANIFEST = json.loads((WEBEXT / "manifest.json").read_text())


def _load_build():
    """Import build.py by path without leaving a __pycache__/ in the extension
    dir (Chrome refuses to Load-unpacked a dir with a '_'-prefixed name)."""
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("wxbuild", WEBEXT / "build.py")
    mod = importlib.util.module_from_spec(spec)
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = prev
    return mod


BUILD = _load_build()


def _strip_js_comments(src: str) -> str:
    """Drop // line and /* */ block comments so scans see code, not prose
    (the header comment legitimately says 'the Falkon browser.')."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(^|[^:])//[^\n]*", lambda m: m.group(1), src)
    return src


def _cs(entry_world):
    """The content_scripts entry whose world matches (None = isolated)."""
    for cs in MANIFEST["content_scripts"]:
        if cs.get("world") == entry_world:
            return cs
    raise AssertionError(f"no content_scripts entry with world={entry_world!r}")


class TestManifest:
    def test_is_mv3(self):
        assert MANIFEST["manifest_version"] == 3

    def test_source_background_is_chrome_service_worker_only(self):
        # The source manifest is the Chromium unpacked-dev target, so it must
        # NOT carry the MV2 `background.scripts` key — Chrome logs
        # "'background.scripts' requires manifest version of 2 or lower" on it.
        assert MANIFEST["background"] == {"service_worker": "background.js"}

    def test_firefox_variant_uses_event_page_scripts(self):
        # Firefox MV3 has no service-worker background; build.py generates the
        # event-page form for the Firefox package (and browser test).
        fx = BUILD._manifest_for("firefox")
        assert fx["background"] == {"scripts": ["background.js"]}
        chrome = BUILD._manifest_for("chrome")
        assert chrome["background"] == {"service_worker": "background.js"}

    def test_version_tracks_the_app_version(self):
        # Both extensions carry the app version (qeth/__init__.py __version__).
        # `build.py sync` keeps the source files in step; this gate fails if
        # someone bumps __version__ without re-syncing.
        app = BUILD.app_version()
        assert MANIFEST["version"] == app
        desktop = (FALKON / "metadata.desktop").read_text()
        assert f"X-Falkon-Version={app}" in desktop

    def test_min_chrome_116_for_ws_keepalive(self):
        # <116 service workers aren't kept alive by WebSocket activity.
        assert MANIFEST["minimum_chrome_version"] == "116"

    def test_provider_injected_into_main_world_at_document_start(self):
        cs = _cs("MAIN")
        assert cs["js"] == ["config.js", "provider.js"]   # config BEFORE provider
        assert cs["run_at"] == "document_start"
        assert cs["all_frames"] is True

    def test_relay_is_isolated_world(self):
        cs = _cs(None)
        assert cs["js"] == ["relay.js"]
        assert cs["run_at"] == "document_start"
        assert cs["all_frames"] is True

    def test_csp_allows_loopback_ws(self):
        csp = MANIFEST["content_security_policy"]["extension_pages"]
        assert "connect-src" in csp
        assert "ws://127.0.0.1:1248" in csp

    def test_firefox_gecko_id_and_min_version(self):
        gecko = MANIFEST["browser_specific_settings"]["gecko"]
        assert gecko["id"]                       # stable, AMO binds to it
        assert gecko["strict_min_version"] == "128.0"   # world:MAIN support

    def test_minimal_permissions(self):
        assert MANIFEST["permissions"] == ["alarms"]
        assert set(MANIFEST["host_permissions"]) == {"http://*/*", "https://*/*"}

    def test_all_referenced_icons_exist(self):
        refs = set(MANIFEST["icons"].values())
        refs |= set(MANIFEST["action"]["default_icon"].values())
        for rel in refs:
            assert (WEBEXT / rel).is_file(), f"missing icon {rel}"


class TestProviderMirror:
    def test_provider_is_byte_identical_to_falkon(self):
        # The shared core must not drift. Any provider fix lands in the Falkon
        # copy and is mirrored here (build.py / this gate enforce equality).
        assert (WEBEXT / "provider.js").read_bytes() == \
            (FALKON / "provider.js").read_bytes()


class TestConfig:
    def _config_obj(self):
        src = (WEBEXT / "config.js").read_text()
        blob = src[src.index("{"):src.rindex("}") + 1]
        return json.loads(blob)

    def test_config_selects_push_transport(self):
        cfg = self._config_obj()
        assert cfg["poll"] is False
        assert cfg["directFallback"] is False
        assert cfg["push"] is True

    def test_config_logo_is_the_svg_data_uri(self):
        cfg = self._config_obj()
        svg = (WEBEXT / "icons" / "qeth-icon.svg").read_bytes()
        expected = "data:image/svg+xml;base64," + base64.b64encode(svg).decode()
        assert cfg["logo"] == expected


class TestWorldHygiene:
    def test_main_world_files_dont_touch_extension_apis(self):
        # provider.js / config.js run in the page's MAIN world — no chrome.*/
        # browser.* there. (relay.js/background.js, added later, are where the
        # extension APIs live.)
        for name in ("provider.js", "config.js"):
            src = _strip_js_comments((WEBEXT / name).read_text())
            assert "chrome." not in src, f"{name} references chrome.*"
            assert "browser." not in src, f"{name} references browser.*"

    def test_provider_uses_the_shared_envelope_tags(self):
        src = (WEBEXT / "provider.js").read_text()
        assert '"qeth-provider"' in src
        assert '"qeth-relay"' in src


class TestTransportFiles:
    def test_files_exist(self):
        assert (WEBEXT / "relay.js").is_file()
        assert (WEBEXT / "background.js").is_file()

    def test_relay_initiates_a_port_not_the_socket(self):
        src = (WEBEXT / "relay.js").read_text()
        assert "chrome.runtime.connect" in src        # per-frame Port
        assert "window.ethereum" not in src           # relay never sets it
        assert "WebSocket" not in src                 # the socket lives in bg
        assert '"qeth-provider"' in src and '"qeth-relay"' in src

    def test_background_holds_the_loopback_socket(self):
        src = (WEBEXT / "background.js").read_text()
        assert "ws://127.0.0.1:1248" in src
        assert "onConnect" in src                     # receives relay Ports
        assert "window.ethereum" not in _strip_js_comments(src)

    def test_background_demuxes_pushes_and_stamps_origin(self):
        src = (WEBEXT / "background.js").read_text()
        assert "eth_subscription" in src              # push demux
        assert "__frameOrigin" in src                 # per-frame origin stamp
        assert "sender" in src                        # from the unforgeable port

    def test_background_origin_is_from_sender_and_http_only(self):
        # Finding A + C from the Frame Companion review: the dapp origin must
        # come from the unforgeable port.sender (never a page-supplied field),
        # and only http(s) origins are trusted — a file:// page collapses to a
        # shared "file://" otherwise. The forwarded payload is a fresh object
        # (method/params/id + __frameOrigin), NOT a spread of the page payload.
        # Read raw (not comment-stripped): the http(s) regex literal contains
        # `//`, which the comment stripper would eat.
        raw = (WEBEXT / "background.js").read_text()
        assert "port.sender" in raw
        assert "originOf(port)" in raw
        assert "!/^https?:\\/\\//i.test(o)" in raw     # http(s)-only origin gate
        # No spread of the page payload into the upstream message (that's how
        # Frame leaked page-controlled __frameOrigin/__extensionConnecting).
        code = _strip_js_comments(raw)
        assert "...payload" not in code and "...msg" not in code
        assert "__extensionConnecting" not in code

    def test_background_keepalive_is_local_and_periodic(self):
        src = (WEBEXT / "background.js").read_text()
        assert "periodInMinutes" in src               # alarms keepalive
        # eth_chainId is locally answered; web3_clientVersion would be proxied
        # upstream and hit the chain RPC every tick (checked against code, not
        # the comment that explains the choice).
        assert "eth_chainId" in src
        assert "web3_clientVersion" not in _strip_js_comments(src)


class TestPopup:
    def test_popup_files_exist_and_are_referenced(self):
        assert (WEBEXT / "popup.html").is_file()
        assert (WEBEXT / "popup.js").is_file()
        assert MANIFEST["action"]["default_popup"] == "popup.html"

    def test_popup_has_no_inline_script(self):
        # MV3 CSP forbids inline scripts; every <script> must be external.
        html = (WEBEXT / "popup.html").read_text()
        for attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", html, re.S):
            assert "src=" in attrs, "inline <script> block in popup.html"
            assert body.strip() == "", "popup <script> has an inline body"

    def test_popup_drives_status_and_firefox_grant(self):
        js = (WEBEXT / "popup.js").read_text()
        assert '{ type: "status" }' in js or '"status"' in js
        assert "chrome.permissions.request" in js     # Firefox host-access grant
        assert "chrome.permissions.contains" in js

    def test_background_answers_the_status_query(self):
        src = (WEBEXT / "background.js").read_text()
        assert "onMessage" in src
        assert '"status"' in src


class TestBuild:
    def _build_mod(self):
        return BUILD

    def test_packages_exactly_the_shipping_files(self, tmp_path):
        import json as _json
        import zipfile
        mod = self._build_mod()
        zip_path = mod.build(tmp_path)                    # chrome (default)
        with zipfile.ZipFile(zip_path) as zf:
            members = set(zf.namelist())
            manifest = _json.loads(zf.read("manifest.json"))
        # Exactly the package set — no README/.gitignore/build.py/out/svg.
        assert members == set(mod.PACKAGE_FILES)
        assert "README.md" not in members
        assert "build.py" not in members
        # Version-stamped from the app version; Chrome background.
        assert zip_path.name == f"qeth-{mod.app_version()}-chrome.zip"
        assert manifest["version"] == mod.app_version()
        assert manifest["background"] == {"service_worker": "background.js"}

    def test_firefox_build_ships_the_event_page_manifest(self, tmp_path):
        import json as _json
        import zipfile
        mod = self._build_mod()
        zip_path = mod.build(tmp_path, "firefox")
        assert zip_path.name == f"qeth-{mod.app_version()}-firefox.zip"
        with zipfile.ZipFile(zip_path) as zf:
            manifest = _json.loads(zf.read("manifest.json"))
        assert manifest["background"] == {"scripts": ["background.js"]}

    def test_write_unpacked_materialises_the_target_manifest(self, tmp_path):
        import json as _json
        mod = self._build_mod()
        dest = mod.write_unpacked(tmp_path / "fx", "firefox")
        manifest = _json.loads((dest / "manifest.json").read_text())
        assert manifest["background"] == {"scripts": ["background.js"]}
        # Every shipping file landed.
        for rel in mod.PACKAGE_FILES:
            assert (dest / rel).is_file()

    def test_build_does_not_mutate_the_source_tree(self, tmp_path):
        # build()/write_unpacked() stamp the version into the PACKAGED manifest
        # only — the committed manifest.json must be untouched (sync does that).
        mod = self._build_mod()
        before = (WEBEXT / "manifest.json").read_bytes()
        mod.build(tmp_path)
        mod.write_unpacked(tmp_path / "u", "chrome")
        assert (WEBEXT / "manifest.json").read_bytes() == before

    def test_build_rejects_a_drifted_provider(self, tmp_path, monkeypatch):
        import pytest
        mod = self._build_mod()
        # Make only the webext provider read as different from the Falkon copy.
        orig = mod.Path.read_bytes

        def fake_read_bytes(self):
            if self.name == "provider.js" and "webext" in str(self):
                return b"// drifted"
            return orig(self)

        monkeypatch.setattr(mod.Path, "read_bytes", fake_read_bytes)
        with pytest.raises(SystemExit):
            mod.build(tmp_path)

    def test_jwt_is_valid_hs256(self):
        import base64
        import hashlib
        import hmac
        mod = self._build_mod()
        tok = mod._jwt("user:1:2", "s3cr3t")
        head_b64, body_b64, sig_b64 = tok.split(".")
        signing_input = (head_b64 + "." + body_b64).encode()
        want = base64.urlsafe_b64encode(
            hmac.new(b"s3cr3t", signing_input, hashlib.sha256).digest()
        ).rstrip(b"=").decode()
        assert want == sig_b64
