#!/bin/sh
set -eu

qeth_repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$qeth_repo_root"

# QETH_BUNDLE_HELIOS=/path/to/helios builds the "verify" variant instead (the
# spec picks it up and renames the output to qeth-verify-macos.app). uv run
# forwards the environment, so it just needs exporting before this script.
#
# Extras match what the AppImage bundles ([bundled,simulate,qr]) so the .app is
# not a lesser build: `simulate` is the pure-Python py-evm fork engine behind
# the pre-broadcast event preview on RPCs without eth_simulateV1 — dropping it
# would silently cost Mac users that check. `frame` stays out, as it does
# there: it's only the Frame-export import path, and it drags in Rust-built
# cryptography.
exec uv run --isolated --no-dev \
    --extra bundled --extra simulate --extra qr --group macos-build \
    pyinstaller --noconfirm --clean \
    --distpath=dist/macos/out --workpath=dist/macos/out/build \
    "$@" dist/macos/qeth.spec
