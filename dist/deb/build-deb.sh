#!/bin/bash
# Build a native-themed qeth .deb for Mint 22 / Ubuntu 24.04 (x86_64).
#
# Why this is more involved than the Fedora RPM: Fedora ships python3-pyside6 and
# most of the eth stack, so the RPM just Depends on them. Debian/Ubuntu 24.04
# ship NEITHER a Python-3.11 PySide6 (the stock 6.4 wheel is Requires-Python
# <3.12; py3.12 support starts at PySide6 6.6, which needs Qt 6.6) NOR a current
# eth stack. So we:
#   * run under the deadsnakes python3.11  (ppa:deadsnakes/ppa)
#   * BUILD PySide6 6.4 from source against the system Qt 6.4  (build-pyside.sh)
#   * vendor PySide6 + the eth stack privately under /usr/lib/qeth/vendor
# The bindings link the SYSTEM Qt 6.4 (Depends: libqt6*), so the user's
# qt6ct/Kvantum theme applies — the native-theming win over flatpak/AppImage.
#
# Build prereqs (apt): python3.11 python3.11-venv python3.11-dev (deadsnakes),
#   qt6-base-dev qt6-base-private-dev qt6-declarative-private-dev,
#   libclang-14-dev clang-14 cmake ninja-build, dpkg-dev.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
VERSION="$(sed -nE 's/^__version__ = "(.*)"/\1/p' "$REPO/qeth/__init__.py")"
VENV="${QETH_PYSIDE_VENV:-/tmp/qeth-pyside-venv}"
OUT="${1:-$REPO/dist/deb}"
PY=python3.11

echo ">> qeth $VERSION  (venv: $VENV  out: $OUT)"

# 1. PySide6 6.4 from source into the venv — skipped if already built (the abi3
#    bindings are Python-version-portable + reusable). The slow step (~15 min).
if ! "$VENV/bin/$PY" -c "import PySide6, shiboken6" 2>/dev/null; then
    "$HERE/build-pyside.sh" "$VENV"
fi

# 2. qeth + the eth stack into the same venv (vendored). pip's build isolation
#    keeps the venv's build-pinned setuptools out of the dep installs.
#    [simulate] = the pure-Python py-evm fork engine: event previews on RPCs
#    without eth_simulateV1, and Helios-verified previews when the user has
#    a helios binary installed.
"$VENV/bin/$PY" -m pip install --no-warn-script-location --no-compile "$REPO[simulate]"

# 3. Assemble the .deb tree.
STAGE="$(mktemp -d)/qeth"
VENDOR="$STAGE/usr/lib/qeth/vendor"
install -d "$VENDOR" "$STAGE/usr/bin" \
        "$STAGE/usr/share/applications" \
        "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/DEBIAN"
cp -a "$VENV"/lib/"$PY"/site-packages/. "$VENDOR/"
# Drop venv/pip bookkeeping — keep PySide6 + shiboken6 + qeth + the runtime deps.
rm -rf "$VENDOR"/pip "$VENDOR"/pip-*.dist-info \
       "$VENDOR"/setuptools "$VENDOR"/setuptools-*.dist-info \
       "$VENDOR"/wheel "$VENDOR"/wheel-*.dist-info \
       "$VENDOR"/_distutils_hack "$VENDOR"/pkg_resources "$VENDOR"/*.pth
find "$VENDOR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

install -Dm0755 "$HERE/qeth.launcher" "$STAGE/usr/bin/qeth"
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
Depends: python3.11, libqt6widgets6, libqt6gui6, libqt6core6, libqt6svg6, libqt6network6, libqt6dbus6
Installed-Size: $INSTALLED_KB
Section: utils
Priority: optional
Homepage: https://github.com/michwill/qeth
Description: Qt Ethereum wallet with Ledger support and a Frame-compatible JSON-RPC server
 qeth is a PySide6 Ethereum wallet for the Linux desktop with Ledger support and
 a Frame-compatible JSON-RPC server. This build runs under deadsnakes python3.11
 with a from-source PySide6 6.4 against the system Qt 6.4 (native theming),
 vendoring PySide6 + the eth stack (web3, eth-*, ledgereth, ...) privately.
EOF

mkdir -p "$OUT"
DEB="$OUT/qeth_${VERSION}_amd64.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$DEB"
rm -rf "$(dirname "$STAGE")"
echo ">> built $DEB"
dpkg-deb -I "$DEB" | sed -n '1,14p'
