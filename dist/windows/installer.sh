#!/bin/sh
set -eu

qeth_repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$qeth_repo_root"

# Compile qeth.iss into dist/windows/out/qeth-<version>-setup.exe from the
# bundle build.sh produced. ISCC is Inno Setup's compiler (default: on PATH).
# Locally, `scripts/wine.sh installer` runs the same thing under Wine.
[ -d dist/windows/out/qeth-windows ] \
    || { echo "no dist/windows/out/qeth-windows — run dist/windows/build.sh first" >&2; exit 1; }
version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' qeth/__init__.py)

# Git Bash rewrites arguments that look like POSIX paths — /DAppVersion=… among
# them; keep Windows-style switches verbatim.
export MSYS2_ARG_CONV_EXCL='*'
exec "${ISCC:-iscc}" /Qp "/DAppVersion=$version" dist/windows/qeth.iss
