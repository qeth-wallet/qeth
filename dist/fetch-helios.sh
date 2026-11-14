#!/usr/bin/env bash
# Print the path of a sha256-verified Helios binary for the "verify" builds,
# downloading the pinned official release on first use.
#
#   HELIOS="$(dist/fetch-helios.sh)"
#
# Cached per version under dist/.helios/<version>/ (gitignored, inside the repo
# so the AppImage container can mount it). The digest is GitHub's own for the
# release asset. Bumping Helios = change HELIOS_VERSION + HELIOS_SHA256 here (and
# the darwin pin in .github/workflows/macos.yml). The .deb/.rpm VMs take a binary
# path via QETH_BUNDLE_HELIOS; copy them this one.
set -euo pipefail

HELIOS_VERSION="0.12.0"
HELIOS_SHA256="dd2746b11323f8f4a4e4613891b275148f34963bf4912cab79a34316a02a94b4"
URL="https://github.com/a16z/helios/releases/download/${HELIOS_VERSION}/helios_linux_amd64.tar.gz"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$REPO/dist/.helios/$HELIOS_VERSION"
BIN="$DIR/helios"

if [ ! -x "$BIN" ]; then
    mkdir -p "$DIR"
    tmp="$(mktemp "$DIR/helios.tar.gz.XXXXXX")"
    trap 'rm -f "$tmp"' EXIT
    echo ">> fetching helios $HELIOS_VERSION" >&2
    curl -fsSL --retry 3 -o "$tmp" "$URL"
    echo "$HELIOS_SHA256  $tmp" | sha256sum -c --quiet - >&2 \
        || { echo "helios $HELIOS_VERSION: sha256 mismatch" >&2; exit 1; }
    tar -xzf "$tmp" -C "$DIR"
    [ -f "$BIN" ] || { echo "helios $HELIOS_VERSION: no helios in the tarball" >&2; exit 1; }
    chmod +x "$BIN"
fi
echo "$BIN"
