#!/bin/bash
# Build a native-themed qeth .deb for a distro that ships PySide6 itself (x86_64):
#   QETH_DEB_TARGET=debian13    (default) LMDE 7 "Gigi" / Debian 13 "Trixie" —
#                               PySide6 6.8, python3.13
#   QETH_DEB_TARGET=ubuntu2604  Ubuntu 26.04 LTS "Resolute Raccoon" (and the Mint
#                               release built on it) — PySide6 6.10, python3.14
# Run it ON the target: the vendored wheels are built for that distro's python3,
# and the package Depends on exactly that python3 minor version.
#
# Why this is FAR simpler than the Mint/Ubuntu build (build-deb.sh): Debian 13
# ships PySide6 6.8 natively — as the split python3-pyside6.* module packages —
# built for its stock python3 (3.13). So, exactly like the Fedora RPM, we just
#   Depends: on the system python3-pyside6.* modules
# and DON'T compile any Qt bindings. The bindings link the system Qt (via the
# distro's libqt6* deps), so the user's qt6ct/Kvantum theme applies natively —
# the same native-theming win as the Mint deb, without the 15-minute PySide6
# from-source compile.
#
# Debian does NOT package the eth stack (web3/eth-account/eth-abi/eth-keys/rlp/
# ledgereth), a new-enough hexbytes (>=1), or zxing-cpp, so — like the Mint deb —
# the FULL Python closure MINUS PySide6 is vendored under /usr/lib/qeth/vendor
# from PyPI wheels. Vendoring the whole closure (rather than Depending on
# Debian's python3-pydantic/-aiohttp/-eth-utils, some of which only exist in
# backports) keeps the package installable on any stock Debian 13 with just the
# main repo. The vendor dir shadows any system copy via PYTHONPATH order.
#
# Build prereqs (apt; Debian: main repo only, Ubuntu: main + universe, where
# its python3-pyside6.* live):
#   python3 python3-venv python3-pip
#   python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets \
#   python3-pyside6.qtnetwork python3-pyside6.qtmultimedia
#   dpkg-dev
# (The build only needs system PySide6 present so a post-build smoke import can
# verify the bindings resolve; the .deb itself Depends on them for the user.)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VERSION="$(sed -nE 's/^__version__ = "(.*)"/\1/p' "$REPO/qeth/__init__.py")"
VENV="$(mktemp -d)/venv"
OUT="${1:-$REPO/dist/deb}"
PY=python3
TARGET="${QETH_DEB_TARGET:-debian13}"
case "$TARGET" in
    debian13)   DISTRO='LMDE 7 / Debian 13 "Trixie"' ;;
    ubuntu2604) DISTRO='Ubuntu 26.04 LTS "Resolute Raccoon"' ;;
    *) echo "unknown QETH_DEB_TARGET: $TARGET (debian13 | ubuntu2604)" >&2; exit 1 ;;
esac
# The vendored closure has compiled wheels (pydantic-core, lru-dict, ...) for
# THIS interpreter's minor version, so pin python3 to it: X.Y <= python3 < X.Y+1.
PYVER="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
PYNEXT="$("$PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1] + 1}")')"

echo ">> qeth $VERSION  $TARGET build ($DISTRO, python $PYVER)  (venv: $VENV  out: $OUT)"

# 1. A plain venv under the SYSTEM python3 (3.13 / 3.14), then the full closure MINUS
#    PySide6 from PyPI. PySide6 is only in qeth's [bundled] extra, which we do
#    NOT request, so pip never pulls it — it comes from the system at runtime.
#    [simulate] = the pure-Python py-evm fork engine (event previews on RPCs
#    without eth_simulateV1, + Helios-verified previews). [qr] = the air-gapped
#    QR signer decode stack (cbor2 + zxing-cpp reader + Pillow).
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install -q --upgrade pip
"$VENV/bin/python" -m pip install --no-warn-script-location --no-compile "$REPO[simulate,qr,trezor]"
# The venv's real site-packages (lib/python3.13/..., not lib/python3/...) —
# ask the venv rather than constructing it from $PY.
SITE="$("$VENV/bin/python" -c 'import site; print(site.getsitepackages()[0])')"
# Sanity: the venv closure must NOT contain PySide6 (it comes from the system).
if [ -e "$SITE/PySide6" ]; then
    echo "!! PySide6 leaked into the vendored closure — it must come from the system" >&2
    exit 1
fi

# 2. Assemble the .deb tree.
STAGE="$(mktemp -d)/qeth"
VENDOR="$STAGE/usr/lib/qeth/vendor"
install -d "$VENDOR" "$STAGE/usr/bin" \
        "$STAGE/usr/share/applications" \
        "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/DEBIAN"
cp -a "$SITE/." "$VENDOR/"
# Drop venv/pip bookkeeping — keep qeth + the vendored runtime deps.
rm -rf "$VENDOR"/pip "$VENDOR"/pip-*.dist-info \
       "$VENDOR"/setuptools "$VENDOR"/setuptools-*.dist-info \
       "$VENDOR"/wheel "$VENDOR"/wheel-*.dist-info \
       "$VENDOR"/_distutils_hack "$VENDOR"/pkg_resources "$VENDOR"/*.pth
find "$VENDOR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

install -Dm0755 "$HERE/qeth-debian.launcher" "$STAGE/usr/bin/qeth"

# "verify" variant: bundle a Helios light client so verified previews work out
# of the box (the launcher points QETH_HELIOS_BIN at it). Pass a helios binary
# path in QETH_BUNDLE_HELIOS. Without it, the normal package is produced.
VARIANT=""
DESC_HELIOS=""
if [ -n "${QETH_BUNDLE_HELIOS:-}" ]; then
    [ -x "$QETH_BUNDLE_HELIOS" ] || { echo "QETH_BUNDLE_HELIOS not executable: $QETH_BUNDLE_HELIOS" >&2; exit 1; }
    install -Dm0755 "$QETH_BUNDLE_HELIOS" "$STAGE/usr/lib/qeth/helios"
    VARIANT="-verify"
    DESC_HELIOS=$'\n This "verify" build bundles a Helios light client, so transaction\n previews are proof-verified against Ethereum consensus out of the box.'
    echo ">> bundling helios: $("$QETH_BUNDLE_HELIOS" --version 2>/dev/null | head -1)"
fi

install -Dm0644 "$REPO/dist/flatpak/io.github.michwill.qeth.desktop" \
        "$STAGE/usr/share/applications/io.github.michwill.qeth.desktop"
install -Dm0644 "$REPO/qeth/assets/logos/qeth-icon-rounded.svg" \
        "$STAGE/usr/share/icons/hicolor/scalable/apps/io.github.michwill.qeth.svg"

INSTALLED_KB="$(du -sk "$STAGE/usr" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: qeth
Version: $VERSION
Architecture: amd64
Maintainer: Michael Egorov <michwill@yieldbasis.com>
Depends: python3 (>= $PYVER), python3 (<< $PYNEXT), python3-pyside6.qtcore, python3-pyside6.qtgui, python3-pyside6.qtwidgets, python3-pyside6.qtnetwork, python3-pyside6.qtmultimedia, gstreamer1.0-plugins-good, libusb-1.0-0
Installed-Size: $INSTALLED_KB
Section: utils
Priority: optional
Homepage: https://github.com/qeth-wallet/qeth
Description: Qt Ethereum wallet with Ledger support and a Frame-compatible JSON-RPC server
 qeth is a PySide6 Ethereum wallet for the Linux desktop with Ledger support and
 a Frame-compatible JSON-RPC server. This build targets $DISTRO:
 it Depends on the system PySide6 (native theming) and vendors the eth stack
 (web3, eth-*, ledgereth, ...) privately under /usr/lib/qeth/vendor.$DESC_HELIOS
EOF

mkdir -p "$OUT"
DEB="$OUT/qeth${VARIANT}_${VERSION}_${TARGET}_amd64.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$DEB"
rm -rf "$(dirname "$STAGE")" "$(dirname "$VENV")"
echo ">> built $DEB"
dpkg-deb -I "$DEB" | sed -n '1,16p'
