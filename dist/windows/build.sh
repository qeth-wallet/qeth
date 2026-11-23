#!/bin/sh
set -eu

qeth_repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$qeth_repo_root"

# Run on Windows (Git Bash in CI). Locally, `scripts/wine.sh build` runs the
# same thing under Wine.
#
# Extras match the macOS .app / AppImage ([bundled,simulate,qr]) plus trezor,
# so the Windows build isn't a lesser one. `frame` stays out, as it does
# there: it's only the Frame-export import path, and it drags in Rust-built
# cryptography.
exec uv run --isolated --no-dev \
    --extra bundled --extra simulate --extra qr --extra trezor --group windows-build \
    pyinstaller --noconfirm --clean \
    --distpath=dist/windows/out --workpath=dist/windows/out/build \
    "$@" dist/windows/qeth.spec
