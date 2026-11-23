#!/usr/bin/env bash
# Smoke-run a qeth.exe on Windows (Git Bash — CI): launch it on a fresh
# profile and require the JSON-RPC answer on 127.0.0.1:1248, its main window,
# and a live process 15 s on; then stop it. `scripts/wine.sh smoke` is the
# Wine counterpart.
#
# Usage: dist/windows/smoke.sh PATH/TO/qeth.exe
set -euo pipefail
export MSYS2_ARG_CONV_EXCL='*'   # keep /F, /IM, /FI verbatim
exe=$1

# Path.home() reads USERPROFILE: a throwaway ~/.qeth, never the runner's.
profile=$(mktemp -d)
USERPROFILE=$(cygpath -w "$profile") "$exe" &

running() { tasklist /FI "IMAGENAME eq qeth.exe" | grep -qi qeth.exe; }
stop() {
    taskkill /F /IM qeth.exe > /dev/null 2>&1 || true
    for _ in $(seq 20); do running || return 0; sleep 0.5; done
}

ok=
for i in $(seq 1 45); do
    if reply=$(curl -fsS --max-time 2 -H 'Content-Type: application/json' \
         --data '{"jsonrpc":"2.0","id":1,"method":"web3_clientVersion","params":[]}' \
         http://127.0.0.1:1248 2> /dev/null) && [[ $reply == *'"qeth/v'* ]]; then
        ok=1; echo "RPC after ~$((i * 2)) s: $reply"; break
    fi
    sleep 2
done
[ -n "$ok" ] || { echo "::error::$exe never answered on 127.0.0.1:1248"; stop; exit 1; }

sleep 15
title=$(powershell -NoProfile -Command \
    "(Get-Process qeth -ErrorAction SilentlyContinue | Where-Object MainWindowTitle | Select-Object -First 1).MainWindowTitle")
echo "window: ${title:-<none>}"
running || { echo "::error::$exe exited after starting"; exit 1; }
stop
# Matched loosely: the em dash in "qeth — Ethereum wallet" may not survive
# PowerShell's console code page. Qt's own "no platform plugin" box (title
# "qeth") or PyInstaller's traceback box fail here.
case "$title" in
    *"Ethereum wallet"*) ;;
    *) echo "::error::$exe's main window isn't up (window: ${title:-<none>})"; exit 1 ;;
esac
echo "fresh profile's ~/.qeth:"; ls "$profile/.qeth" 2> /dev/null || echo "  (nothing written yet)"
