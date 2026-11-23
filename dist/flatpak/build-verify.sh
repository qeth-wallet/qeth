#!/bin/bash
# Build the "verify" Flatpak variant: the normal bundle + a Helios light
# client at /app/bin/helios, so transaction previews are proof-verified
# out of the box. This is the ONLY way verified mode reaches the Flatpak —
# the sandbox can't reach a helios binary on the host.
#
# Helios is the pinned, sha256-checked release from dist/fetch-helios.sh unless
# QETH_BUNDLE_HELIOS names another binary explicitly. The script derives a
# verify manifest from the base one (single source of truth) by adding a helios
# module + the QETH_HELIOS_BIN env, exports the pinned Python deps from uv.lock,
# then builds + bundles to dist/flatpak/qeth-verify.flatpak.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="$HERE/io.github.michwill.qeth.yml"
HELIOS="${QETH_BUNDLE_HELIOS:-$("$HERE/../fetch-helios.sh")}"
[ -x "$HELIOS" ] || { echo "not executable: $HELIOS" >&2; exit 1; }
"$HERE/../lock-export.sh" "$HERE/../.pinned/flatpak" simulate qr trezor

STATE="${QETH_FLATPAK_STATE:-$HOME/.cache/qeth-flatpak}"
cp "$HELIOS" "$HERE/helios"        # the file: source the module reads (gitignored)

# The derived manifest MUST live in dist/flatpak/ (next to the base): its
# source paths (`../..` for the repo, `helios` for the binary) are resolved
# relative to the manifest's own directory.
MANIFEST="$HERE/.qeth-verify.yml"
trap 'rm -f "$HERE/helios" "$MANIFEST"' EXIT

# Derive the verify manifest: inject the env into finish-args and append a
# helios module. Pure text insertion — no YAML lib needed; the base format
# is stable.
awk '
  /^finish-args:/ {
    print
    print "  - --env=QETH_HELIOS_BIN=/app/bin/helios   # bundled Helios (verify variant)"
    next
  }
  { print }
' "$BASE" > "$MANIFEST"
cat >> "$MANIFEST" <<"YAML"
  - name: helios
    buildsystem: simple
    build-options:
      # Ship the sha256-checked release byte-for-byte: flatpak-builder would
      # otherwise strip it (debuginfo split), so it no longer matched the digest.
      no-debuginfo: true
      strip: false
    build-commands:
      - install -Dm0755 helios ${FLATPAK_DEST}/bin/helios
    sources:
      - type: file
        path: helios
YAML

echo ">> bundling helios: $("$HELIOS" --version 2>/dev/null | head -1)"
flatpak-builder --user --force-clean --repo "$STATE/repo-verify" \
    --state-dir "$STATE/state-verify" "$STATE/build-verify" "$MANIFEST"
OUT="${1:-$HERE/qeth-verify.flatpak}"
flatpak build-bundle "$STATE/repo-verify" "$OUT" io.github.michwill.qeth
echo ">> built $OUT"
ls -la "$OUT"
