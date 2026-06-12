#!/bin/bash
# Build the "verify" Flatpak variant: the normal bundle + a Helios light
# client at /app/bin/helios, so transaction previews are proof-verified
# out of the box. This is the ONLY way verified mode reaches the Flatpak —
# the sandbox can't reach a helios binary on the host.
#
# Pass a helios binary path in QETH_BUNDLE_HELIOS (e.g. heliosup's
# ~/.helios/bin/helios). The script derives a verify manifest from the base
# one (single source of truth) by adding a helios module + the
# QETH_HELIOS_BIN env, then builds + bundles to dist/flatpak/qeth-verify.flatpak.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE="$HERE/io.github.michwill.qeth.yml"
HELIOS="${QETH_BUNDLE_HELIOS:?set QETH_BUNDLE_HELIOS to a helios binary path}"
[ -x "$HELIOS" ] || { echo "not executable: $HELIOS" >&2; exit 1; }

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
