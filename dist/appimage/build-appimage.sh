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
#    pulseaudio-libs provides libpulse.so.0, which libQt6Multimedia (the QR
#    signer's camera) hard-links (DT_NEEDED) — without it the multimedia module
#    won't even load. The client lib alone is enough; no audio server is needed
#    for camera capture.
dnf install -y -q \
    libxcb libxkbcommon libxkbcommon-x11 xcb-util-cursor xcb-util-image \
    xcb-util-keysyms xcb-util-renderutil xcb-util-wm libX11 libXext libXrender \
    libXrandr libXi libSM libICE fontconfig freetype mesa-libGL pulseaudio-libs \
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
# [simulate] = pure-Python py-evm fork engine (previews on simV1-less RPCs;
# Helios-verified previews when a helios binary is on the host).
# [qr] = the air-gapped QR signer decode stack (cbor2 + zxing-cpp + Pillow),
# from manylinux wheels — pairs with the QtMultimedia camera kept in 3b.
"$PY" -m pip install --no-cache-dir --prefix="$APPDIR/usr/python" \
    "${BUILD_SRC}[bundled,simulate,qr]"
# Fail loudly if Qt didn't actually land in the bundle, rather than shipping a
# tiny empty AppImage.
if ! ls -d "$APPDIR"/usr/python/lib/python*/site-packages/PySide6 >/dev/null 2>&1; then
    echo "FATAL: PySide6 not in the AppDir after install. site-packages holds:"
    ls "$APPDIR"/usr/python/lib/python*/site-packages/ 2>&1 | head -40
    exit 1
fi
echo "DIAG: AppDir after install = $(du -sh "$APPDIR" | cut -f1)"

# "verify" variant: bundle a Helios light client (path in QETH_BUNDLE_HELIOS)
# so previews are proof-verified out of the box; AppRun points
# QETH_HELIOS_BIN at it. NOTE: the official helios release needs glibc >= 2.39,
# which raises this AppImage's floor above the usual 2.34 — the verify AppImage
# therefore targets glibc >= 2.39 (Ubuntu 24.04+, Fedora 39+).
VARIANT=""
if [ -n "${QETH_BUNDLE_HELIOS:-}" ]; then
    [ -x "$QETH_BUNDLE_HELIOS" ] || { echo "QETH_BUNDLE_HELIOS not executable" >&2; exit 1; }
    install -Dm0755 "$QETH_BUNDLE_HELIOS" "$APPDIR/usr/bin/helios"
    VARIANT="-verify"
    echo "DIAG: bundled helios $("$QETH_BUNDLE_HELIOS" --version 2>/dev/null | head -1)"
fi

# 3b. Trim Qt modules qeth doesn't use. It's mostly a QtWidgets app, so the
#     heavyweight Addons — WebEngine, 3D, Charts, Pdf, Designer — are dead
#     weight and get removed.
#     EXCEPTION: QtMultimedia (the QR signer's live camera) is kept, and with it
#     the modules its FFmpeg backend plugin hard-links (DT_NEEDED, verified from
#     the wheel): Quick + the QtQml libs (Qml/QmlModels/QmlWorkerScript/QmlMeta)
#     + OpenGL. Those libQt6*.so must stay or libffmpegmediaplugin.so won't load
#     — even though qeth never touches QML (we only need the .so, not the qml/
#     module dir, which is still removed below). The bundled LGPL ffmpeg libav*
#     live in Qt/lib and aren't libQt6*, so they survive this loop untouched.
PS="$(echo "$APPDIR"/usr/python/lib/python*/site-packages/PySide6)"
rm -rf "$PS/Qt/qml" "$PS/Qt/resources" "$PS/Qt/translations" \
       "$PS"/Qt/libexec 2>/dev/null
# NB: Quick, Qml, QmlModels, QmlWorkerScript, QmlMeta and Multimedia are
# deliberately ABSENT from this list — they're the FFmpeg camera plugin's
# dependency closure (see 3b). The remaining Quick*/Qml*/3D*/Multimedia* here
# (QuickWidgets, QmlCore, MultimediaWidgets, …) are NOT in that closure, so
# they stay stripped.
for mod in WebEngineCore WebEngineWidgets WebEngineQuick WebChannel WebChannelQuick \
           WebView Quick3D QuickWidgets QuickControls2 QuickControls2Impl \
           QuickTemplates2 QuickShapes QuickParticles QuickTest QuickLayouts \
           QuickDialogs2 QuickDialogs2QuickImpl QuickDialogs2Utils QuickEffects \
           QuickTimeline QuickVectorImage \
           QmlLocalStorage QmlCore QmlXmlListModel 3DCore 3DRender 3DInput \
           3DLogic 3DAnimation 3DExtras 3DQuick 3DQuickRender 3DQuickScene2D \
           Charts ChartsQml DataVisualization DataVisualizationQml Graphs \
           GraphsWidgets MultimediaWidgets MultimediaQuick SpatialAudio \
           Pdf PdfWidgets PdfQuick Designer DesignerComponents Help UiTools \
           Sql Test Bluetooth Nfc Positioning PositioningQuick Location Sensors \
           SensorsQuick SerialPort SerialBus RemoteObjects RemoteObjectsQml \
           Scxml ScxmlQml TextToSpeech WebSockets StateMachine StateMachineQml; do
    rm -f "$PS/Qt/lib/libQt6${mod}".so* "$PS/Qt${mod}.abi3.so" "$PS/Qt${mod}.pyi" 2>/dev/null
done
# Keep plugins/multimedia — it holds libffmpegmediaplugin.so, the camera backend.
rm -rf "$PS"/Qt/plugins/{qmltooling,webview,sqldrivers,designer,position,sensors,texttospeech,scenegraph} 2>/dev/null
# The camera plugin dlopens the Quick/Qml/OpenGL C++ libs kept above, but qeth
# imports none of them from Python — so drop their multi-MB shiboken BINDINGS +
# stubs (the .so libs stay). QtMultimedia's own binding is kept (qeth imports it;
# it needs only Core/Gui/Network, whose bindings also stay).
for b in Quick Qml QmlModels QmlWorkerScript QmlMeta OpenGL; do
    rm -f "$PS/Qt${b}.abi3.so" "$PS/Qt${b}.pyi" 2>/dev/null
done
echo "DIAG: AppDir after trim = $(du -sh "$APPDIR" | cut -f1)"

# 4. Bundle the external (non-wheel) shared-lib deps of Qt's libs + plugins.
#    PySide6 already ships its own libQt6*.so inside site-packages; we only need
#    the system libs those link that aren't in the wheel.
QTDIR="$(echo "$APPDIR"/usr/python/lib/python*/site-packages/PySide6/Qt)"
{ find "$QTDIR/lib" -name 'libQt6*.so*' 2>/dev/null
  find "$QTDIR/plugins/platforms" -name '*.so' 2>/dev/null
  find "$QTDIR/plugins/multimedia" -name '*.so' 2>/dev/null; } | while read -r so; do
    ldd "$so" 2>/dev/null || true
done | awk '/=> \// {print $3}' \
  | grep -vE 'PySide6/|/libQt6|/libpython|/ld-linux|/libc\.so|/libm\.so|/libdl|/libpthread|/librt|/libstdc\+\+|/libgcc_s' \
  | sort -u | xargs -r -I{} cp -Lu {} "$APPDIR/usr/lib/" 2>/dev/null || true

# 4a. Camera-stack sanity — the QR signer's live camera must ALWAYS work, so fail
#     the build here (not on the user's machine) if the FFmpeg backend plugin,
#     the bundled ffmpeg, or a Qt lib it hard-links went missing in the trim.
#     ldd resolves against the wheel's $ORIGIN/../../lib (libQt6*/libav*) plus
#     the container's system libs (incl. the pulseaudio-libs installed in step 1
#     — libQt6Multimedia hard-links libpulse), so a 'not found' here is a real
#     gap. libavcodec/libQt6Quick are checked because they're the two easiest
#     things to break: an over-eager libav strip and the QML closure (see 3b).
CAM="$QTDIR/plugins/multimedia/libffmpegmediaplugin.so"
for f in "$CAM" "$QTDIR"/lib/libQt6Multimedia.so.6 \
         "$QTDIR"/lib/libQt6Quick.so.6 "$QTDIR"/lib/libavcodec.so.*; do
    ls $f >/dev/null 2>&1 || { echo "FATAL: camera stack incomplete — missing $f"; exit 1; }
done
if ldd "$CAM" 2>/dev/null | grep -q 'not found'; then
    echo "FATAL: libffmpegmediaplugin.so has unresolved deps:"; ldd "$CAM" | grep 'not found'
    exit 1
fi
echo "DIAG: camera stack OK ($(basename "$(ls "$QTDIR"/lib/libavcodec.so.* | head -1)") bundled)"

# 4b. Strip debug symbols from bundled .so — EXCEPT the bundled ffmpeg libs.
#     Stripping libav*/libsw* with the container's (old binutils) strip misaligns
#     their PT_LOAD segments (offset%align != vaddr%align), so a NEWER-glibc host
#     rejects the dlopen with "ELF load command address/offset not page-aligned"
#     — which silently disables the camera backend. It slips past the 4a gate:
#     that runs pre-strip, and the build container's own glibc 2.28 loads the
#     misaligned lib fine (the strictness is newer). The ffmpeg libs ship
#     release-stripped in the wheel, so skipping them costs no size.
find "$APPDIR" -type f -name '*.so*' \
    ! -name 'libav*' ! -name 'libsw*' \
    -exec strip --strip-unneeded {} \; 2>/dev/null || true
echo "DIAG: AppDir after strip = $(du -sh "$APPDIR" | cut -f1)"

# 4c. Regression guard for 4b: verify strip left the ffmpeg libs' LOAD segments
#     page-aligned. A hit here is exactly the corruption that kills the camera on
#     a newer-glibc host — fail the build now instead of shipping a dead scanner.
bad_align="$(find "$QTDIR/lib" -type f \( -name 'libav*' -o -name 'libsw*' \) | while read -r f; do
    readelf -lW "$f" 2>/dev/null | awk -v F="$f" '/LOAD/{a=strtonum($NF); if(a>0 && (strtonum($2)%a)!=(strtonum($3)%a)){print F; exit}}'
done)"
if [ -n "$bad_align" ]; then
    echo "FATAL: ffmpeg lib(s) have misaligned LOAD segments (strip corruption — camera would fail on newer glibc):"
    echo "$bad_align"
    exit 1
fi
echo "DIAG: ffmpeg libs page-aligned after strip OK"

# 5. AppImage metadata at the AppDir root (AppImage conventions: AppRun + one
#    top-level .desktop + a matching icon).
install -Dm755 "$SRC/dist/appimage/AppRun"                          "$APPDIR/AppRun"
install -Dm644 "$SRC/dist/appimage/io.github.michwill.qeth.desktop" "$APPDIR/io.github.michwill.qeth.desktop"
install -Dm644 "$SRC/qeth/assets/logos/qeth-icon-rounded.svg"       "$APPDIR/io.github.michwill.qeth.svg"

# 6. Pack. --appimage-extract-and-run avoids needing FUSE inside the container.
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$BUILD_SRC/qeth/__init__.py")"
# -f so a transient 5xx (GitHub's "continuous" release CDN 504s intermittently)
# fails the curl instead of saving the HTML error page as the "binary"; retry a
# few times so a hiccup doesn't sink the whole release build.
curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o "$WORK/appimagetool" \
  "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
chmod +x "$WORK/appimagetool"
ARCH="$ARCH" "$WORK/appimagetool" --appimage-extract-and-run \
  "$APPDIR" "$OUT/qeth${VARIANT}-${VERSION}-${ARCH}.AppImage"
_floor="2.34"; [ -n "$VARIANT" ] && _floor="2.39 (bundled helios)"
echo "OK -> $OUT/qeth${VARIANT}-${VERSION}-${ARCH}.AppImage  (needs glibc >= $_floor, generic x86-64)"
