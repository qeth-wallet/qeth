#!/bin/sh
set -eu

qeth_repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$qeth_repo_root"

exec uv run --isolated --no-dev \
    --extra bundled --extra qr --group macos-build \
    pyinstaller --noconfirm --clean \
    --distpath=dist/macos/out --workpath=dist/macos/out/build \
    "$@" dist/macos/qeth.spec
