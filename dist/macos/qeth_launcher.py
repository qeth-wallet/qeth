"""Entry point for the packaged macOS application."""

import os
import sys
from pathlib import Path


def _bundled_helios() -> Path | None:
    """The Helios binary shipped inside a "verify" bundle, if this is one.

    Only the verify variant carries it (built with ``QETH_BUNDLE_HELIOS``).
    PyInstaller unpacks ``binaries`` next to ``sys._MEIPASS`` — Contents/
    Frameworks for a macOS onedir app, with Contents/Resources symlinked to it
    — so both are checked."""
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(Path(sys.executable).resolve().parent)
    for root in roots:
        for candidate in (root / "helios", root.parent / "Resources" / "helios"):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def _use_bundled_ca_certs() -> None:
    """Point OpenSSL at certifi's CA bundle, as the AppImage's AppRun does.

    The app is built with Homebrew's Python, so PyInstaller ships Homebrew's
    OpenSSL, whose default CA file is baked in at build time:
    /opt/homebrew/etc/openssl@3/cert.pem. That exists on the CI runner and on
    Macs with Homebrew's openssl@3, and nowhere else — and OpenSSL can't read
    the Keychain. Without this, everything verified against the default store
    failed with "unable to get local issuer certificate": urllib (token
    discovery, prices, icons, history, the chainlist), aiohttp, websockets.
    requests was unaffected (it uses certifi itself), which is why RPC calls
    worked. Must run before anything creates an SSL context. An explicit
    SSL_CERT_FILE from the environment wins."""
    if "SSL_CERT_FILE" in os.environ:
        return
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()


def main() -> int:
    _use_bundled_ca_certs()
    # Point qeth's resolver at the bundled binary, the way the .deb/.rpm
    # launchers do. Set only when it's actually present, so a normal bundle
    # still falls through to a helios on PATH or ~/.helios/bin. An explicit
    # QETH_HELIOS_BIN from the environment always wins.
    if "QETH_HELIOS_BIN" not in os.environ:
        helios = _bundled_helios()
        if helios is not None:
            os.environ["QETH_HELIOS_BIN"] = str(helios)
    from qeth.__main__ import main as qeth_main
    return qeth_main()


if __name__ == "__main__":
    raise SystemExit(main())
