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
#   scripts/wine.sh build [ARGS]   PyInstaller → dist/windows/out/qeth-windows
#   scripts/wine.sh installer      Inno Setup → dist/windows/out/qeth-<v>-setup.exe
#   scripts/wine.sh smoke          run the build the way CI does, isolated
#   scripts/wine.sh smoke installer   …or install, run, uninstall the setup.exe
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
# to fetch). `smoke` also needs Xvfb, xwininfo and unprivileged user
# namespaces (unshare -rn).
#
# Env: QETH_WINEPREFIX, QETH_WINE_PYTHON (default 3.14), QETH_WINE_DPI (setup
# writes it as the prefix's LogPixels, e.g. 192 on a 2x screen),
# QETH_SMOKE_LOG (where `smoke` writes the app's output), WINEDEBUG
# (default -all).
set -euo pipefail
self=$(realpath "$0")
cd "$(dirname "$0")/.."

prefix=${QETH_WINEPREFIX:-$HOME/.wine-qeth}
smoke_prefix=$prefix-smoke   # no Python in it: a clean Windows box
pyver=${QETH_WINE_PYTHON:-3.14}
venv='C:\qeth-venv'
uv_exe='C:\tools\uv\uv.exe'
# The same Inno Setup release windows.yml pins.
innosetup_version=6.7.3
innosetup_sha256=9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732
iscc='C:\tools\innosetup\ISCC.exe'

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

fetch_innosetup() (  # subshell, for the EXIT trap
    local tmp exe=innosetup-$innosetup_version.exe
    local marker=$prefix/drive_c/tools/innosetup/.qeth-version
    [ "$(cat "$marker" 2> /dev/null)" = "$innosetup_version" ] && exit 0
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    echo "== Inno Setup $innosetup_version"
    curl -fsSL -o "$tmp/$exe" \
        "https://github.com/jrsoftware/issrc/releases/download/is-${innosetup_version//./_}/$exe"
    echo "$innosetup_sha256  $tmp/$exe" | sha256sum -c --quiet
    wenv wine "$tmp/$exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART '/DIR=C:\tools\innosetup'
    echo "$innosetup_version" > "$marker"
)

qeth_version() {
    sed -n 's/^__version__ = "\(.*\)"/\1/p' qeth/__init__.py
}

sync() {
    echo "== uv sync → $venv (Python $pyver)"
    wenv UV_PROJECT_ENVIRONMENT="$venv" wine "$uv_exe" sync --locked --all-extras --python "$pyver"
}

# What every prefix needs to run Qt — the dev one and the smoke one.
init_prefix() {
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
}

setup() {
    init_prefix
    fetch_uv
    echo "== Python $pyver (Windows)"
    wenv wine "$uv_exe" python install "$pyver"
    sync
    fetch_innosetup
    wenv "$wine_dir/wineserver" -w
}

# Launch $1 (a qeth.exe) and require what CI does: the RPC server answers, the
# main window comes up, the process is still there 15 s on. Then stop it.
run_exe() {
    local exe=$1 i reply rpc= windows alive= ok=1 pid
    # Wine keeps the PID: $! IS the Windows process. Track exactly it — never
    # match by name (`pkill -f qeth.exe` would hit any process whose command
    # line merely mentions it).
    wenv wine "$exe" >> "${QETH_SMOKE_LOG:-/dev/null}" 2>&1 &
    pid=$!
    for i in $(seq 1 60); do
        if reply=$(curl -fsS --max-time 2 -H 'Content-Type: application/json' \
             --data '{"jsonrpc":"2.0","id":1,"method":"web3_clientVersion","params":[]}' \
             http://127.0.0.1:1248 2> /dev/null) && [[ $reply == *'"qeth/v'* ]]; then
            rpc=1; echo "RPC answered after ~$((i * 2)) s: $reply"; break
        fi
        sleep 2
    done
    sleep 15
    windows=$(xwininfo -root -tree 2> /dev/null | grep -oE '"[^"]+"' | sort -u || true)
    kill -0 "$pid" 2> /dev/null && alive=1
    # Through Wine's own taskkill: it terminates via the wineserver, which
    # releases the process's open files before returning — after a bare Unix
    # kill the uninstaller can still find the bundle in use. Scoped to this
    # prefix's Wine session, not the host.
    wenv wine taskkill /F /IM qeth.exe > /dev/null 2>&1 || true
    for i in $(seq 20); do kill -0 "$pid" 2> /dev/null || break; sleep 0.5; done
    [ -n "$rpc" ] || { echo "FAIL: no answer on 127.0.0.1:1248"; ok=; }
    [ -n "$alive" ] || { echo "FAIL: qeth.exe exited"; ok=; }
    [[ $windows == *'"qeth — Ethereum wallet"'* ]] \
        || { echo "FAIL: no main window (windows: ${windows//$'\n'/ })"; ok=; }
    [[ $windows == *'Unhandled exception'* ]] && { echo "FAIL: an unhandled exception box"; ok=; }
    [ -n "$ok" ]
}

# Run the build the way CI does, on the smoke prefix (no Python: a clean
# Windows box), inside a private network namespace — its own loopback, so
# port 1248 is free even beside a running qeth, and nothing reaches the
# outside — with a hidden Xvfb display. Runs as the namespace's root
# (`unshare -r`), which only maps to you. `installer` mode installs the
# setup.exe silently, checks the Start-menu entry and uninstall registration,
# runs the INSTALLED qeth.exe, then uninstalls and checks it's gone.
smoke() {
    local mode=${1:-bundle} exe=dist/windows/out/qeth-windows/qeth.exe setup_exe= i ok=1
    if [ "$mode" = installer ]; then
        setup_exe=$(ls dist/windows/out/qeth-*-setup.exe 2> /dev/null | tail -1)
        [ -n "$setup_exe" ] || { echo "no setup.exe — run: scripts/wine.sh installer" >&2; exit 1; }
        exe=
    else
        [ -f "$exe" ] || { echo "no $exe — run: scripts/wine.sh build" >&2; exit 1; }
    fi
    [ -e /tmp/.X11-unix/X99 ] && { echo "display :99 is taken" >&2; exit 1; }
    prefix=$smoke_prefix
    ip link set lo up
    Xvfb :99 -screen 0 1600x1000x24 -nolisten tcp > /dev/null 2>&1 &
    local xvfb=$!
    for i in $(seq 50); do [ -S /tmp/.X11-unix/X99 ] && break; sleep 0.1; done
    export DISPLAY=:99
    init_prefix > /dev/null

    if [ -n "$setup_exe" ]; then
        local appid
        appid=$(sed -n 's/^AppId={{\(.*\)}$/\1/p' dist/windows/qeth.iss)
        echo "== $setup_exe, silently, on a clean prefix, isolated"
        wenv wine "$setup_exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER
        exe=$(ls "$prefix"/drive_c/users/*/AppData/Local/Programs/qeth/qeth.exe 2> /dev/null | head -1)
        [ -n "$exe" ] || { echo "FAIL: nothing installed in %LOCALAPPDATA%\\Programs\\qeth"; ok=; }
        ls "$prefix"/drive_c/users/*/AppData/Roaming/Microsoft/Windows/'Start Menu'/Programs/qeth.lnk \
            > /dev/null 2>&1 || { echo "FAIL: no Start-menu entry"; ok=; }
        wenv wine reg query "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{$appid}_is1" \
            /v DisplayVersion > /dev/null 2>&1 || { echo "FAIL: not registered for uninstall"; ok=; }
    else
        echo "== $exe on a clean prefix, isolated"
    fi
    [ -n "$exe" ] && { run_exe "$exe" || ok=; }
    if [ -n "$setup_exe" ] && [ -n "$exe" ]; then
        # The uninstaller re-launches itself from %TEMP% and returns at once;
        # that copy removes unins000.exe and the folder last — wait for it.
        local app_dir
        app_dir=$(dirname "$exe")
        wenv wine "$app_dir/unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
        for i in $(seq 60); do [ -e "$app_dir" ] || break; sleep 1; done
        [ ! -e "$app_dir" ] || { echo "FAIL: uninstalling left $(ls "$app_dir" | tr '\n' ' ')behind"; ok=; }
    fi
    wenv "$wine_dir/wineserver" -k || true
    kill "$xvfb"
    [ -n "$ok" ] && echo "OK: $mode — RPC up, main window shown, still running${setup_exe:+; installed + uninstalled cleanly}"
    [ -n "$ok" ]
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
    # dist/windows/build.sh's command (keep the two in step), through the
    # Windows uv — an isolated env, so the dev venv above is untouched.
    build)  wenv wine "$uv_exe" run --isolated --no-dev \
                --extra bundled --extra simulate --extra qr --extra trezor \
                --group windows-build \
                pyinstaller --noconfirm --clean \
                --distpath=dist/windows/out --workpath=dist/windows/out/build \
                "$@" dist/windows/qeth.spec ;;
    installer)
        [ -d dist/windows/out/qeth-windows ] || { echo "run: scripts/wine.sh build" >&2; exit 1; }
        wenv wine "$iscc" /Qp "/DAppVersion=$(qeth_version)" 'dist\windows\qeth.iss' ;;
    smoke)  exec unshare -rn "$self" _smoke "$@" ;;   # → smoke(), in its namespace
    _smoke) smoke "$@" ;;
    *) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2 ;;
esac
