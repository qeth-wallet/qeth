#!/usr/bin/env bash
# Assemble the qeth AppImage *inside a manylinux container* (glibc 2.34, generic
# x86-64). Nothing from the developer's host (Gentoo, -march=native, glibc 2.42)
# enters the bundle: the interpreter, every wheel, and the bundled system libs
# all come from the old, generic container. Run via build-in-container.sh or CI
# — NOT on the host directly (a host-built AppImage would need the host's glibc
# 2.42 and its CPU's instruction set, i.e. run almost nowhere — not even the VM).
#
# Env knobs (all optional):
#   SRC    repo checkout            (default /src)
#   OUT    where the .AppImage lands (default /out)
#   WORK   scratch build dir         (default /tmp/qeth-appimage)
#   PYVER  manylinux CPython tag     (default cp312-cp312)
#
# Lower the glibc floor to 2.28 (Debian 10 / RHEL 8 / Ubuntu 20.04+): run this
# in quay.io/pypa/manylinux_2_28_x86_64 AND pin PySide6<6.10 in the [bundled]
# extra — 6.10 moved PySide6's own wheel floor up to glibc 2.34.
set -euo pipefail

SRC="${SRC:-/src}"
OUT="${OUT:-/out}"
WORK="${WORK:-/tmp/qeth-appimage}"
PYVER="${PYVER:-cp312-cp312}"
ARCH="x86_64"
APPDIR="$WORK/AppDir"

rm -rf "$WORK"; mkdir -p "$APPDIR/usr/lib" "$OUT"

# 1. System libraries the Qt xcb platform plugin dlopens but the PySide6 wheel
#    does NOT bundle (libxcb-cursor is the usual culprit on Qt 6.5+). Pulled
#    from the container (old glibc, generic), never the host. NOTE: this list
#    is a starting point — the real test is launching on a clean target (the
#    VM) and chasing whatever the xcb plugin reports missing.
dnf install -y -q \
    libxcb libxkbcommon libxkbcommon-x11 xcb-util-cursor xcb-util-image \
    xcb-util-keysyms xcb-util-renderutil xcb-util-wm libX11 libXext libXrender \
    libXrandr libXi libSM libICE fontconfig freetype mesa-libGL \
    >/dev/null 2>&1 || echo "WARN: some packages unavailable — refine for the target"

# 2. A relocatable CPython from the container ($ORIGIN-relative RPATH, so it
#    runs from anywhere once PYTHONHOME points at it).
# /opt/python/cpXYZ is a SYMLINK into /opt/_internal — copy the *resolved* tree,
# or the AppDir gets a dangling link and ships empty.
mkdir -p "$APPDIR/usr/python"
cp -a "$(readlink -f "/opt/python/${PYVER}")/." "$APPDIR/usr/python/"
M="${PYVER#cp3}"; M="${M%%-*}"                 # cp312-cp312 -> 12
ln -sf "python3.${M}" "$APPDIR/usr/python/bin/python3"
PY="$APPDIR/usr/python/bin/python3"
# manylinux's CPython is built with a fixed /opt/python prefix, so pip would
# install back THERE, not into our copy. Point its home at the AppDir for the
# rest of the build so every install lands in the bundle's site-packages. (At
# runtime AppRun sets the same PYTHONHOME.)
export PYTHONHOME="$APPDIR/usr/python"

# 3. qeth + bundled PySide6 + the eth stack, installed FRESH from PyPI manylinux
#    wheels. Anything without a wheel compiles with the container's generic gcc
#    (old glibc, no -march=native) — still portable.
#    The source is copied to a WRITABLE dir first: pip's egg_info build step
#    writes into the tree, and /src is mounted read-only. The exclude list keeps
#    the host .venv/.git/build artifacts out — no host binary ever rides along,
#    which is the whole point of building in here.
BUILD_SRC="$WORK/src"
mkdir -p "$BUILD_SRC"
tar -C "$SRC" \
    --exclude=.git --exclude=.venv --exclude=build --exclude=dist \
    --exclude=.flatpak-builder --exclude=resume --exclude='__pycache__' \
    --exclude='.env' --exclude='.env.local' \
    -cf - . | tar -C "$BUILD_SRC" -xf -
"$PY" -m pip install --no-cache-dir --upgrade pip wheel >/dev/null
# --prefix pins the install into the AppDir copy. manylinux's CPython resolves
# its own prefix back to /opt/python, so neither the default scheme nor
# PYTHONHOME reliably redirects pip — --prefix is explicit and deterministic.
"$PY" -m pip install --no-cache-dir --prefix="$APPDIR/usr/python" \
    "${BUILD_SRC}[bundled]"
# Fail loudly if Qt didn't actually land in the bundle, rather than shipping a
# tiny empty AppImage.
if ! ls -d "$APPDIR"/usr/python/lib/python*/site-packages/PySide6 >/dev/null 2>&1; then
    echo "FATAL: PySide6 not in the AppDir after install. site-packages holds:"
    ls "$APPDIR"/usr/python/lib/python*/site-packages/ 2>&1 | head -40
    exit 1
fi
echo "DIAG: AppDir after install = $(du -sh "$APPDIR" | cut -f1)"

# 3b. Trim Qt modules qeth doesn't use. It's a pure QtWidgets app (only
#     QtCore/QtGui/QtWidgets; QtSvg/QtNetwork/QtDBus stay as runtime deps), so
#     the heavyweight Addons — WebEngine, the whole QML/Quick stack, 3D, Charts,
#     Multimedia, Pdf, Designer — are dead weight that dominates the size. None
#     is a dependency of QtWidgets, so removing them is safe.
PS="$(echo "$APPDIR"/usr/python/lib/python*/site-packages/PySide6)"
rm -rf "$PS/Qt/qml" "$PS/Qt/resources" "$PS/Qt/translations" \
       "$PS"/Qt/libexec 2>/dev/null
for mod in WebEngineCore WebEngineWidgets WebEngineQuick WebChannel WebChannelQuick \
           WebView Quick Quick3D QuickWidgets QuickControls2 QuickControls2Impl \
           QuickTemplates2 QuickShapes QuickParticles QuickTest QuickLayouts \
           QuickDialogs2 QuickDialogs2QuickImpl QuickDialogs2Utils QuickEffects \
           QuickTimeline QuickVectorImage Qml QmlModels QmlWorkerScript \
           QmlLocalStorage QmlCore QmlXmlListModel QmlMeta 3DCore 3DRender 3DInput \
           3DLogic 3DAnimation 3DExtras 3DQuick 3DQuickRender 3DQuickScene2D \
           Charts ChartsQml DataVisualization DataVisualizationQml Graphs \
           GraphsWidgets Multimedia MultimediaWidgets MultimediaQuick SpatialAudio \
           Pdf PdfWidgets PdfQuick Designer DesignerComponents Help UiTools \
           Sql Test Bluetooth Nfc Positioning PositioningQuick Location Sensors \
           SensorsQuick SerialPort SerialBus RemoteObjects RemoteObjectsQml \
           Scxml ScxmlQml TextToSpeech WebSockets StateMachine StateMachineQml; do
    rm -f "$PS/Qt/lib/libQt6${mod}".so* "$PS/Qt${mod}.abi3.so" "$PS/Qt${mod}.pyi" 2>/dev/null
done
rm -rf "$PS"/Qt/plugins/{qmltooling,webview,multimedia,sqldrivers,designer,position,sensors,texttospeech,scenegraph} 2>/dev/null
echo "DIAG: AppDir after trim = $(du -sh "$APPDIR" | cut -f1)"

# 4. Bundle the external (non-wheel) shared-lib deps of Qt's libs + plugins.
#    PySide6 already ships its own libQt6*.so inside site-packages; we only need
#    the system libs those link that aren't in the wheel.
QTDIR="$(echo "$APPDIR"/usr/python/lib/python*/site-packages/PySide6/Qt)"
{ find "$QTDIR/lib" -name 'libQt6*.so*' 2>/dev/null
  find "$QTDIR/plugins/platforms" -name '*.so' 2>/dev/null; } | while read -r so; do
    ldd "$so" 2>/dev/null || true
done | awk '/=> \// {print $3}' \
  | grep -vE 'PySide6/|/libQt6|/libpython|/ld-linux|/libc\.so|/libm\.so|/libdl|/libpthread|/librt|/libstdc\+\+|/libgcc_s' \
  | sort -u | xargs -r -I{} cp -Lu {} "$APPDIR/usr/lib/" 2>/dev/null || true

# 4b. Strip debug symbols from every bundled .so — safe for shared objects
#     (--strip-unneeded preserves what dynamic linking needs) and saves tens of
#     MB across the Qt libs, python extensions and the bundled system libs.
find "$APPDIR" -type f -name '*.so*' -exec strip --strip-unneeded {} \; 2>/dev/null || true
echo "DIAG: AppDir after strip = $(du -sh "$APPDIR" | cut -f1)"

# 5. AppImage metadata at the AppDir root (AppImage conventions: AppRun + one
#    top-level .desktop + a matching icon).
install -Dm755 "$SRC/dist/appimage/AppRun"                          "$APPDIR/AppRun"
install -Dm644 "$SRC/dist/appimage/io.github.michwill.qeth.desktop" "$APPDIR/io.github.michwill.qeth.desktop"
install -Dm644 "$SRC/qeth/assets/logos/qeth-icon-rounded.svg"       "$APPDIR/io.github.michwill.qeth.svg"

# 6. Pack. --appimage-extract-and-run avoids needing FUSE inside the container.
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$BUILD_SRC/qeth/__init__.py")"
curl -sSL -o "$WORK/appimagetool" \
  "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
chmod +x "$WORK/appimagetool"
ARCH="$ARCH" "$WORK/appimagetool" --appimage-extract-and-run \
  "$APPDIR" "$OUT/qeth-${VERSION}-${ARCH}.AppImage"
echo "OK -> $OUT/qeth-${VERSION}-${ARCH}.AppImage  (needs glibc >= 2.34, generic x86-64)"
