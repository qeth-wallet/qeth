#!/usr/bin/env bash
# Build BOTH AppImage variants (normal + "verify") locally by running
# build-appimage.sh inside the manylinux container. Needs podman or docker.
#
# This is the PRIMARY way the AppImage is built: the dev box has docker, so the
# release .AppImages are produced here (in a manylinux container) and verified
# locally before upload. The CI workflow (.github/workflows/appimage.yml) is kept
# but its automatic tag-trigger is DISABLED — the container is what guarantees
# the dev host's glibc/CPU tuning never touches the bundle, and docker provides
# it here just as well as a clean CI runner.
#
#   ./dist/appimage/build-in-container.sh                 # glibc 2.34, newest Qt
#   IMAGE=quay.io/pypa/manylinux_2_28_x86_64 ./...        # glibc 2.28 (PySide6<6.10)
#
# Everything that ends up in the bundle is pinned: the Python deps come from
# uv.lock with hashes (exported here, on the host — the container has no uv),
# Helios is a sha256-checked release (dist/fetch-helios.sh), and the build image
# is pinned by digest. Bump the image deliberately:
#   podman pull quay.io/pypa/manylinux_2_34_x86_64 \
#     && podman image inspect quay.io/pypa/manylinux_2_34_x86_64 --format '{{.RepoDigests}}'
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="${IMAGE:-quay.io/pypa/manylinux_2_34_x86_64@sha256:934419fa742ff922855f464203cc0ad5afe3317a8541712a00cf3dc7fb893fc8}"
OUT="$REPO/dist/appimage/out"
mkdir -p "$OUT"

# The container mounts the repo read-only at /src, so the pins land under it.
"$REPO/dist/lock-export.sh" "$REPO/dist/.pinned/appimage" bundled simulate qr trezor

ENGINE="$(command -v podman || command -v docker || true)"
[ -n "$ENGINE" ] || { echo "need podman or docker (or use the CI workflow)"; exit 1; }

# Run build-appimage.sh in the container. Extra args (e.g. the helios env for the
# verify variant) are passed through to `docker run` before the image name.
_build() {
    "$ENGINE" run --rm \
      -v "$REPO":/src:ro \
      -v "$OUT":/out \
      -e PYVER="${PYVER:-cp312-cp312}" \
      "$@" \
      "$IMAGE" bash /src/dist/appimage/build-appimage.sh
}

# 1. Normal AppImage.
echo ">> building the normal AppImage"
_build

# 2. Verify AppImage — bundles a Helios light client so simulation previews are
#    proof-verified against Ethereum consensus out of the box (AppRun points
#    QETH_HELIOS_BIN at it). Helios is only COPIED into the AppDir, so the build
#    container never runs it — but the official release needs glibc >= 2.39, so
#    THIS variant's floor is 2.39 regardless of $IMAGE (the normal one stays at
#    the container's floor). Helios is the pinned, sha256-checked release from
#    dist/fetch-helios.sh, cached under the repo so it's mountable at /src. (No
#    more falling back to whatever ~/.helios/bin/helios holds — a stale cached
#    copy once shipped 0.11.1 unnoticed.) Set QETH_APPIMAGE_NO_VERIFY=1 to skip
#    this variant.
if [ "${QETH_APPIMAGE_NO_VERIFY:-0}" != "1" ]; then
    HELIOS="$("$REPO/dist/fetch-helios.sh")"
    echo ">> bundling helios: $("$HELIOS" --version 2>/dev/null | head -1)"
    echo ">> building the verify AppImage"
    _build -e QETH_BUNDLE_HELIOS="/src/${HELIOS#"$REPO"/}"
fi

echo ">> built:"
ls -la "$OUT"/*.AppImage
