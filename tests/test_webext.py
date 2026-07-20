"""Static gates for the qeth browser extension (integrations/webext/).

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
WEBEXT = ROOT / "integrations" / "webext"
FALKON = ROOT / "integrations" / "falkon" / "qeth_connector"
MANIFEST = json.loads((WEBEXT / "manifest.json").read_text())


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

    def test_dual_background_for_both_browsers(self):
        # Chrome uses service_worker; Firefox MV3 uses scripts (event page).
        bg = MANIFEST["background"]
        assert bg["service_worker"] == "background.js"
        assert bg["scripts"] == ["background.js"]

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
