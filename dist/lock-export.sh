#!/usr/bin/env bash
# Export hash-pinned requirements from uv.lock for a self-contained build, so it
# ships exactly the versions the lock (and `uv audit`) vouch for — the lock's
# 1-week age cutoff and its hashes — instead of whatever PyPI serves on build day.
#
#   dist/lock-export.sh OUTDIR EXTRA...
#
# Writes into OUTDIR:
#   requirements.txt        qeth's runtime deps for those extras (qeth itself
#                           excluded), every line `==`-pinned with its hashes
#   build-requirements.txt  setuptools, pinned the same way (the `build-backend`
#                           group). The builder installs it OUTSIDE the bundle and
#                           builds with --no-build-isolation, so building qeth and
#                           the few sdist-only deps (ledgerblue, pydes,
#                           python-u2flib-host) never fetches an unpinned setuptools.
#
# The builder then runs, in its own interpreter:
#   pip install --require-hashes --no-deps --target TOOLS -r build-requirements.txt
#   PYTHONPATH=TOOLS pip install --require-hashes --no-build-isolation -r requirements.txt
#   PYTHONPATH=TOOLS pip install --no-deps --no-build-isolation --no-index <qeth src>
# In --require-hashes mode pip refuses any dependency missing from the file, so
# an incomplete export fails the build instead of pulling something unpinned.
#
# Used by the AppImage and Flatpak builds. The .rpm deliberately doesn't: it
# takes most of its stack from Fedora's own packages.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:?usage: dist/lock-export.sh OUTDIR [EXTRA...]}"; shift
extras=()
for e in "$@"; do extras+=(--extra "$e"); done

mkdir -p "$OUT"
cd "$REPO"
# --locked: refuse a lock that's out of sync with pyproject.toml rather than
# silently re-resolving (which would also pick up anything newer).
uv export --locked --quiet --no-dev --no-emit-project "${extras[@]}" \
    -o "$OUT/requirements.txt"
uv export --locked --quiet --only-group build-backend --no-emit-project \
    -o "$OUT/build-requirements.txt"
echo ">> pinned $(grep -c '^[A-Za-z0-9]' "$OUT/requirements.txt") packages from uv.lock -> $OUT"
