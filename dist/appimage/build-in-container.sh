#!/usr/bin/env bash
# Build the AppImage locally by running build-appimage.sh inside the manylinux
# container. Needs podman or docker.
#
# This is the PRIMARY way the AppImage is built: the dev box has docker, so the
# release .AppImage is produced here (in a manylinux container) and verified
# locally before upload. The CI workflow (.github/workflows/appimage.yml) is kept
# but its automatic tag-trigger is DISABLED — the container is what guarantees
# the dev host's glibc/CPU tuning never touches the bundle, and docker provides
# it here just as well as a clean CI runner.
#
#   ./dist/appimage/build-in-container.sh                 # glibc 2.34, newest Qt
#   IMAGE=quay.io/pypa/manylinux_2_28_x86_64 ./...        # glibc 2.28 (pin PySide6<6.10)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="${IMAGE:-quay.io/pypa/manylinux_2_34_x86_64}"
OUT="$REPO/dist/appimage/out"
mkdir -p "$OUT"

ENGINE="$(command -v podman || command -v docker || true)"
[ -n "$ENGINE" ] || { echo "need podman or docker (or use the CI workflow)"; exit 1; }

exec "$ENGINE" run --rm \
  -v "$REPO":/src:ro \
  -v "$OUT":/out \
  -e PYVER="${PYVER:-cp312-cp312}" \
  "$IMAGE" bash /src/dist/appimage/build-appimage.sh
