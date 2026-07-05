#!/bin/bash
# Build PySide6 6.4.2 (Core/Gui/Widgets/Network/Multimedia, abi3) from source
# against the SYSTEM Qt 6.4 for python3.11, into the venv given as $1. ~15 min.
# See ./README.md. (Multimedia is the QR signer's live camera; it needs
# qt6-multimedia-dev at build time, and PySide's QtMultimedia module
# build-depends on the QtNetwork binding — so Network is in the subset too.)
#
# Ubuntu 24.04 / Mint 22 ship no PySide6 that fits their Qt6.4 + py3.12 combo,
# so we compile 6.4 for the deadsnakes python3.11. The bindings are abi3 (stable
# ABI) and link the system Qt — the native-theming win.
set -euo pipefail
VENV="${1:?usage: build-pyside.sh <venv-dir>}"
SRC="${PYSIDE_SRC:-/tmp/pyside-setup}"
PY=python3.11

# 1. A clean venv with the CONTEMPORARY setuptools/wheel that pyside-setup
#    v6.4.2 (2022-era build scripts) needs — Ubuntu 24.04's setuptools is too
#    new for them (it restructured the distutils plumbing v6.4.2 assumes).
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "setuptools==65.5.1" "wheel==0.37.1" packaging

# 2. pyside-setup v6.4.2 source (the github mirror 404s; use code.qt.io).
[ -d "$SRC/.git" ] || git clone -q --branch v6.4.2 --depth 1 \
    https://code.qt.io/pyside/pyside-setup.git "$SRC"

# 3. Compile shiboken6 + the Core/Gui/Widgets bindings against system Qt
#    (libclang-14 drives shiboken's C++ parser), then install into the venv.
#    NB: qt6-declarative-private-dev must be installed or the libpysideqml
#    support lib fails to configure (Qt::QmlPrivate -> missing include path),
#    aborting the whole build — even though qeth never touches QML.
cd "$SRC"
rm -rf build
# Multimedia carries the QR signer's live camera (QCamera → QVideoSink); it links
# the system libQt6Multimedia (added to the .deb Depends) and needs
# qt6-multimedia-dev present here so shiboken can parse its headers. PySide's
# QtMultimedia module build-depends on (and imports) the QtNetwork binding, so
# Network is in the subset — even though qeth uses neither Network nor
# MultimediaWidgets directly.
COMMON="--qmake=/usr/bin/qmake6 --module-subset=Core,Gui,Widgets,Network,Multimedia --skip-docs --ignore-git"
# shellcheck disable=SC2086
LLVM_INSTALL_DIR=/usr/lib/llvm-14 "$VENV/bin/$PY" setup.py build $COMMON --parallel="$(nproc)"
# shellcheck disable=SC2086
LLVM_INSTALL_DIR=/usr/lib/llvm-14 "$VENV/bin/$PY" setup.py install --reuse-build $COMMON

# 4. Drop the bundled Qt copy. The bindings RPATH $ORIGIN/Qt/lib, so removing it
#    falls them through to the SYSTEM Qt 6.4 (verified: QtCore -> the distro's
#    /usr/lib/.../libQt6Core.so.6). The launcher then points QT_PLUGIN_PATH at
#    the system Qt plugins so the platform + platformtheme (qt6ct) plugins load.
rm -rf "$VENV/lib/$PY/site-packages/PySide6/Qt"

"$VENV/bin/$PY" -c "import PySide6, shiboken6; print('PySide6', PySide6.__version__, 'built (system Qt)')"
