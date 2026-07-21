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

FALKON = Path(__file__).resolve().parent.parent / "integrations" / "falkon" / "qeth_connector"
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
    # eth_accounts answered locally as [] while unauthorized.
    assert 'if (!this._authorized && args.method === "eth_accounts")' in PROVIDER
    # EIP-6963 announce is top-frame only.
    assert 'if (!IN_SUBFRAME) {' in PROVIDER


def test_snapshot_before_request_comparison_preserved():
    # The poll/reconnect refresh must compare against the value captured
    # BEFORE the request (a real bug fix), not self.* after _absorb ran.
    assert 'var prevChain = this.chainId;' in PROVIDER
    assert 'var prevAccount = this.selectedAddress;' in PROVIDER


def _load_bridge():
    # bridge.py imports only PySide6 (no Falkon runtime), so it's importable —
    # unlike __init__.py/settings.py, which is why the rest read source as text.
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("qeth_falkon_bridge",
                                                  FALKON / "bridge.py")
    mod = importlib.util.module_from_spec(spec)
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = prev
    return mod


def test_bridge_forwards_only_http_origins():
    # Finding C from the Frame Companion review: a file:// page's
    # window.location.origin collapses to a shared "file://", so every local
    # file would share one per-origin slot in qeth. Only http(s) origins are
    # forwarded as the Origin header; everything else is origin-less.
    bridge = _load_bridge()
    assert bridge._dapp_origin("https://app.uniswap.org") == "https://app.uniswap.org"
    assert bridge._dapp_origin("http://localhost:3000") == "http://localhost:3000"
    for opaque in ("file://", "null", "", None, "chrome://x", "about:blank",
                   "data:text/html,x"):
        assert bridge._dapp_origin(opaque) == "", opaque
