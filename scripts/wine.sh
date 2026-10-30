#!/usr/bin/env bash
# Run qeth's Windows build of Python under Wine — the local loop for Windows
# work (GitHub's Windows runner is the final check; real hardware wallets,
# camera and tray belong in a Windows VM).
#
#   scripts/wine.sh setup          create/refresh the prefix + Windows venv
#   scripts/wine.sh sync           re-sync the venv after uv.lock changes
#   scripts/wine.sh pytest [ARGS]  run the test suite on Windows Python
#   scripts/wine.sh qeth [ARGS]    launch the app
#   scripts/wine.sh python [ARGS]  the venv's python.exe
#
# Everything lives in its own prefix (QETH_WINEPREFIX, default ~/.wine-qeth),
# so Path.home() — and thus ~/.qeth — is C:\users\<you> inside it, never the
# real config. The venv is C:\qeth-venv, NOT the repo's .venv: Windows uv run
# in the repo would otherwise replace the Linux venv. It syncs from the same
# uv.lock (universal — it carries the win_amd64 wheels) with --locked, so it
# never rewrites the lock.
#
# Needs: wine (>= 11, 64-bit), the mingw-w64 cross gcc (for the ICU stub),
# curl, fontconfig's fc-list, and a host uv (its version picks the Windows uv
# to fetch).
#
# Env: QETH_WINEPREFIX, QETH_WINE_PYTHON (default 3.14), QETH_WINE_DPI (setup
# writes it as the prefix's LogPixels, e.g. 192 on a 2x screen), WINEDEBUG
# (default -all).
set -euo pipefail
cd "$(dirname "$0")/.."

prefix=${QETH_WINEPREFIX:-$HOME/.wine-qeth}
pyver=${QETH_WINE_PYTHON:-3.14}
venv='C:\qeth-venv'
uv_exe='C:\tools\uv\uv.exe'

# Gentoo's eselect-wine puts wine on PATH via env.d, which a shell started
# before the install doesn't have yet.
wine_bin=$(command -v wine || echo /etc/eselect/wine/bin/wine)
wine_dir=$(dirname "$wine_bin")

# A clean environment: Windows uv/python would otherwise inherit host
# VIRTUAL_ENV / UV_* / SSL_* pointing at Linux paths. (Wine itself already
# drops QT_*, so the host's QT_SCALE_FACTOR / platform theme never reach the
# Windows Qt.) DISPLAY is passed even for offscreen tests: the first process in
# a prefix starts its desktop, and one started without a display leaves every
# later GUI process in that session with no driver. PYTHONIOENCODING only
# makes the terminal output readable (Windows Python would write cp1252);
# PYTHONUTF8 would also flip open()'s default and hide real encoding bugs.
wenv() {
    env -i HOME="$HOME" USER="$USER" LANG="${LANG:-C.UTF-8}" PYTHONIOENCODING=utf-8 \
        DISPLAY="${DISPLAY:-}" XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}" \
        XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" \
        PATH="$wine_dir:/usr/bin:/bin" WINEPREFIX="$prefix" \
        WINEDEBUG="${WINEDEBUG:--all}" WINEPATH="$venv\\Scripts" "$@"
}

mingw_gcc() {
    command -v x86_64-w64-mingw32-gcc \
        || ls /usr/lib/mingw64-toolchain/bin/x86_64-w64-mingw32-gcc 2>/dev/null \
        || { echo "x86_64-w64-mingw32-gcc not found (Gentoo: dev-util/mingw64-toolchain)" >&2; exit 1; }
}

fetch_uv() (  # subshell, so the EXIT trap cleans up the download
    local version tmp
    version=$(uv --version | awk '{print $2}')
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    local base="https://github.com/astral-sh/uv/releases/download/$version"
    local zip=uv-x86_64-pc-windows-msvc.zip
    echo "== uv $version (Windows)"
    curl -fsSL -o "$tmp/$zip" "$base/$zip"
    curl -fsSL -o "$tmp/$zip.sha256" "$base/$zip.sha256"
    (cd "$tmp" && sha256sum -c --quiet "$zip.sha256")
    mkdir -p "$prefix/drive_c/tools/uv"
    python3 -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
        "$tmp/$zip" "$prefix/drive_c/tools/uv"
)

sync() {
    echo "== uv sync → $venv (Python $pyver)"
    wenv UV_PROJECT_ENVIRONMENT="$venv" wine "$uv_exe" sync --locked --all-extras --python "$pyver"
}

setup() {
    if [ ! -d "$prefix" ]; then
        echo "== new prefix $prefix"
        # Skip the .NET (mono) and IE (gecko) runtimes — Python needs neither.
        wenv WINEDLLOVERRIDES="mscoree,mshtml=" wine wineboot -i
    fi
    if [ -n "${QETH_WINE_DPI:-}" ]; then
        wenv wine reg add 'HKCU\Control Panel\Desktop' /v LogPixels /t REG_DWORD /d "$QETH_WINE_DPI" /f
    fi

    # Qt6Core links Windows' system ICU, which Wine lacks — see the stub.
    echo "== icuuc.dll stub"
    "$(mingw_gcc)" -shared -O2 -s -Wall -Wextra -o "$prefix/drive_c/windows/system32/icuuc.dll" \
        scripts/wine/icuuc_stub.c

    # Qt's windows platform plugin reads the monitor's ICC profile and then
    # calls mscms!WcsCreateIccProfile, which Wine leaves unimplemented — the
    # process aborts and Qt sees no screen. With the profile file absent Qt
    # never gets that far (it just assumes sRGB).
    local icm
    for icm in "$prefix"/drive_c/windows/system32/spool/drivers/color/*.icm; do
        [ -e "$icm" ] && mv "$icm" "$icm.disabled"
    done

    # The test suite's offscreen Qt reads fonts from %WINDIR%\Fonts only (see
    # tests/conftest.py), which Wine leaves empty — it serves its fonts from
    # outside the prefix. Link in the host's copies of the families a Windows
    # install has (whichever exist), plus DejaVu as the fallback.
    echo "== fonts → C:\\windows\\Fonts"
    local fam file fonts="$prefix/drive_c/windows/Fonts"
    for fam in "Arial" "Courier New" "Times New Roman" "Tahoma" "Verdana" "Georgia" \
               "Trebuchet MS" "Comic Sans MS" "DejaVu Sans" "DejaVu Sans Mono" "DejaVu Serif"; do
        fc-list --format '%{file}\n' "$fam" | while read -r file; do
            ln -sf "$file" "$fonts/$(basename "$file")"
        done
    done

    fetch_uv
    echo "== Python $pyver (Windows)"
    wenv wine "$uv_exe" python install "$pyver"
    sync
    wenv "$wine_dir/wineserver" -w
}

cmd=${1:-}
[ $# -gt 0 ] && shift
case "$cmd" in
    setup)  setup ;;
    sync)   sync ;;
    # The pytest cache lives in the prefix: the repo's .pytest_cache is the
    # Linux runs' (--lf would mix the two).
    pytest) wenv wine "$venv\\Scripts\\python.exe" -m pytest -o cache_dir='C:/qeth-pytest-cache' "$@" ;;
    qeth)   wenv wine "$venv\\Scripts\\python.exe" -m qeth "$@" ;;
    python) wenv wine "$venv\\Scripts\\python.exe" "$@" ;;
    *) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
