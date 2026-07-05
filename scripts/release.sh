#!/usr/bin/env bash
# One-command release assembly for qeth.
#
# Builds the host-buildable artifacts (AppImage + Flatpak, each in a normal and
# a -verify variant), collects the .deb / .rpm you built in their target
# environments, checks the full 8-asset set, and — only with --publish — tags
# v<version>, pushes the tag, and creates the GitHub release with all 8 assets.
#
# Why the .deb/.rpm are external: they can't be built portably on the dev host
# (the .deb needs Ubuntu/Mint's Qt 6.4 + deadsnakes py3.11; the .rpm needs
# Fedora's rpmbuild). Build them there — NORMAL and VERIFY — and drop all four
# into dist/release/ (or point QETH_PKG_INDIR at them):
#   deb:  (on Ubuntu/Mint)  ./dist/deb/build-deb.sh <out>
#         (verify)          QETH_BUNDLE_HELIOS=/path/helios ./dist/deb/build-deb.sh <out>
#   rpm:  (on Fedora)       rpmbuild -bb dist/rpm/qeth.spec
#         (verify)          rpmbuild -bb --define "bundle_helios /path/helios" dist/rpm/qeth.spec
#                           then rename the verify output to qeth-verify-<v>-1.fc*.x86_64.rpm
#                           (the spec keeps Name: qeth for both — only the file is renamed)
#
# Usage:
#   scripts/release.sh              # build host assets + collect + verify (no publish)
#   scripts/release.sh --publish    # ...then tag, push, and gh release create
# Needs docker access (the AppImage step): after a fresh `usermod -aG docker`,
# either log out/in or run it as `sg docker -c 'scripts/release.sh …'`.
#
# Env:
#   QETH_PKG_INDIR        where to find the pre-built .deb/.rpm  (default: dist/release)
#   QETH_BUNDLE_HELIOS    helios binary for the -verify variants (default: ~/.helios/bin/helios)
#   QETH_RELEASE_NOTES    markdown notes file for --publish      (default: gh --generate-notes)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
VERSION="$(sed -nE 's/^__version__ = "(.*)"/\1/p' qeth/__init__.py)"
OUT="$REPO/dist/release"
INDIR="${QETH_PKG_INDIR:-$OUT}"
PUBLISH=0; [ "${1:-}" = "--publish" ] && PUBLISH=1
mkdir -p "$OUT"
echo ">> qeth $VERSION  (out: $OUT  publish: $PUBLISH)"

# 1. AppImage — both variants (build-in-container.sh builds normal + verify).
echo ">> [1/4] AppImage (normal + verify) in a manylinux container"
./dist/appimage/build-in-container.sh
cp -f "dist/appimage/out/qeth-$VERSION-x86_64.AppImage" \
      "dist/appimage/out/qeth-verify-$VERSION-x86_64.AppImage" "$OUT/"

# 2. Flatpak — both variants.
echo ">> [2/4] Flatpak (normal)"
flatpak-builder --user --force-clean --repo="$HOME/.cache/qeth-flatpak/repo" \
  --state-dir "$HOME/.cache/qeth-flatpak/state" \
  "$HOME/.cache/qeth-flatpak/build" dist/flatpak/io.github.michwill.qeth.yml
flatpak build-bundle "$HOME/.cache/qeth-flatpak/repo" \
  "$OUT/qeth-$VERSION.flatpak" io.github.michwill.qeth
echo ">> [3/4] Flatpak (verify)"
QETH_BUNDLE_HELIOS="${QETH_BUNDLE_HELIOS:-$HOME/.helios/bin/helios}" \
  bash dist/flatpak/build-verify.sh
cp -f dist/flatpak/qeth-verify.flatpak "$OUT/qeth-verify-$VERSION.flatpak"

# 3. Collect the externally-built .deb / .rpm.
echo ">> [4/4] collecting .deb / .rpm from $INDIR"
if [ "$INDIR" != "$OUT" ]; then
    for pat in "qeth_${VERSION}_amd64.deb" "qeth-verify_${VERSION}_amd64.deb" \
               "qeth-${VERSION}-1.fc"*".x86_64.rpm" "qeth-verify-${VERSION}-1.fc"*".x86_64.rpm"; do
        # shellcheck disable=SC2086
        for f in "$INDIR"/$pat; do [ -e "$f" ] && cp -f "$f" "$OUT/"; done
    done
fi

# 4. Verify the full 8-asset set.
missing=0
for pat in \
    "qeth-${VERSION}-1.fc"*".x86_64.rpm" "qeth-verify-${VERSION}-1.fc"*".x86_64.rpm" \
    "qeth_${VERSION}_amd64.deb"          "qeth-verify_${VERSION}_amd64.deb" \
    "qeth-${VERSION}-x86_64.AppImage"    "qeth-verify-${VERSION}-x86_64.AppImage" \
    "qeth-${VERSION}.flatpak"            "qeth-verify-${VERSION}.flatpak"; do
    # shellcheck disable=SC2086
    ls "$OUT"/$pat >/dev/null 2>&1 || { echo "  MISSING: $pat"; missing=1; }
done
if [ "$missing" = 1 ]; then
    echo "!! Not all 8 assets are present. Build the missing .deb/.rpm in their"
    echo "   target environments and drop them in $INDIR (or set QETH_PKG_INDIR)."
    exit 1
fi
ls -la "$OUT"/*.rpm "$OUT"/*.deb "$OUT"/*.AppImage "$OUT"/*.flatpak | awk '{printf "   %-46s %.0f MB\n", $NF, $5/1048576}'
echo ">> all 8 assets present"
[ "$PUBLISH" = 1 ] || { echo ">> done (dry run — pass --publish to tag + release)"; exit 0; }

# 5. Publish (opt-in): tag, push, gh release create.
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/master)" ] \
  || { echo "!! HEAD != origin/master — push master first"; exit 1; }
git rev-parse "v$VERSION" >/dev/null 2>&1 || git tag "v$VERSION"
git push origin "v$VERSION"
notes_arg=(--generate-notes)
[ -n "${QETH_RELEASE_NOTES:-}" ] && notes_arg=(--notes-file "$QETH_RELEASE_NOTES")
gh release create "v$VERSION" --title "qeth $VERSION" "${notes_arg[@]}" \
  "$OUT"/qeth-"$VERSION"-1.fc*.x86_64.rpm "$OUT"/qeth-verify-"$VERSION"-1.fc*.x86_64.rpm \
  "$OUT/qeth_${VERSION}_amd64.deb" "$OUT/qeth-verify_${VERSION}_amd64.deb" \
  "$OUT/qeth-$VERSION-x86_64.AppImage" "$OUT/qeth-verify-$VERSION-x86_64.AppImage" \
  "$OUT/qeth-$VERSION.flatpak" "$OUT/qeth-verify-$VERSION.flatpak"
gh release view "v$VERSION" --json url --jq '">> published: \(.url)"'
