#!/usr/bin/env bash
# One-command release assembly for qeth.
#
# First gates on uv.lock (step 0) — the AppImage and Flatpak install exactly the
# locked versions, so the lock must be in sync, clean under `uv audit`, and not
# behind on anything that ships. Then builds the host-buildable artifacts
# (AppImage + Flatpak, each in a normal and a -verify variant), collects the .deb / .rpm you built in their target
# environments, checks the full 14-asset Linux set, and — only with --publish —
# tags v<version>, pushes the tag, and creates the GitHub release with them (plus
# whichever macOS / Windows CI downloads are present).
#
# Why the .deb/.rpm are external: they can't be built portably on the dev host
# (the Mint/Ubuntu-24.04 .deb needs Ubuntu/Mint's Qt 6.4 + deadsnakes py3.11; the
# Debian 13 and Ubuntu 26.04 .debs need that distro's native PySide6 and python3;
# each .rpm needs its Fedora release's rpmbuild and python3). Build them there —
# NORMAL and VERIFY — and drop all ten into dist/release/ (or point
# QETH_PKG_INDIR at them):
# (For the verify builds, copy the pinned helios from `dist/fetch-helios.sh` to
# the VM share and pass that path.)
#   deb:  (on Ubuntu/Mint)  ./dist/deb/build-deb.sh <out>
#         (verify)          QETH_BUNDLE_HELIOS=/path/helios ./dist/deb/build-deb.sh <out>
#   deb:  (on LMDE/Debian13)./dist/deb/build-deb-debian.sh <out>
#         (verify)          QETH_BUNDLE_HELIOS=/path/helios ./dist/deb/build-deb-debian.sh <out>
#   deb:  (on Ubuntu 26.04) QETH_DEB_TARGET=ubuntu2604 ./dist/deb/build-deb-debian.sh <out>
#         (verify)          ...plus QETH_BUNDLE_HELIOS=/path/helios
#   rpm:  (on Fedora 44 AND on Fedora 45 — each python3 minor needs its own)
#                           rpmbuild -bb dist/rpm/qeth.spec
#         (verify)          rpmbuild -bb --define "bundle_helios /path/helios" dist/rpm/qeth.spec
#                           then rename the verify output to qeth-verify-<v>-1.fc*.x86_64.rpm
#                           (the spec keeps Name: qeth for both — only the file is renamed)
#
# The macOS .dmg is built by CI (.github/workflows/macos.yml) on an Apple
# Silicon runner — it cannot be produced on this host. Download the `qeth-macos-dmg`
# artifact from the run for THIS commit and drop it in dist/release as
# qeth-<version>-macos-arm64.dmg:
#   gh run download <run-id> --name qeth-macos-dmg -D /tmp/m \
#     && mv /tmp/m/qeth-macos.dmg dist/release/qeth-$VERSION-macos-arm64.dmg
# It is ad-hoc signed, NOT notarized: the release notes must tell users to clear
# com.apple.quarantine. Optional — the release publishes without it (with a
# warning), since it depends on a CI run finishing.
#
# Windows likewise comes from CI (.github/workflows/windows.yml, `bundle` job):
# the portable zip and the Inno Setup installer, both unsigned (SmartScreen
# warns on first run). Same rule — optional, fetched from the run for THIS commit:
#   gh run download <run-id> --name qeth-windows-zip   -D dist/release
#   gh run download <run-id> --name qeth-windows-setup -D dist/release
# (qeth-<version>-windows-x64.zip / qeth-<version>-windows-x64-setup.exe)
#
# Browser extensions are not BUILT by this script — they publish to AMO / the
# Chrome Web Store separately. At release time regenerate + republish them per
# extensions/README.md ("Releasing / publishing"): `build.py submit` sends the
# Firefox zip to the AMO listing as a new listed version, and the chrome zip
# goes to the CWS dashboard. The committed chrome zip (extensions/chrome/) IS attached to the
# GitHub release afterwards (a plain `gh release upload`) for loading unpacked;
# Firefox has nothing to attach. Store versions trail the app while in review.
#
# Usage:
#   scripts/release.sh              # build host assets + collect + verify (no publish)
#   scripts/release.sh --publish    # ...then tag, push, and gh release create
# Needs docker access (the AppImage step): after a fresh `usermod -aG docker`,
# either log out/in or run it as `sg docker -c 'scripts/release.sh …'`.
#
# Env:
#   QETH_PKG_INDIR        where to find the pre-built .deb/.rpm  (default: dist/release)
#   QETH_BUNDLE_HELIOS    helios binary for the -verify variants (default: the pinned,
#                         sha256-checked release from dist/fetch-helios.sh)
#   QETH_RELEASE_NOTES    markdown notes file for --publish      (default: gh --generate-notes)
#   QETH_ALLOW_STALE_LOCK=1  release even though a shipped package is behind
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
VERSION="$(sed -nE 's/^__version__ = "(.*)"/\1/p' qeth/__init__.py)"
OUT="$REPO/dist/release"
INDIR="${QETH_PKG_INDIR:-$OUT}"
PUBLISH=0; [ "${1:-}" = "--publish" ] && PUBLISH=1
mkdir -p "$OUT"
echo ">> qeth $VERSION  (out: $OUT  publish: $PUBLISH)"

# 0. Lock pre-flight. "Behind" = `uv lock --upgrade` would bump a SHIPPED package
#    (one in the AppImage/Flatpak export) to a version the 1-week cutoff already
#    allows; dev-only tools (ruff, mypy, …) don't count. The fix is the usual
#    bump: uv lock --upgrade && uv sync --inexact && uv audit && uv run pytest,
#    then commit and re-run.
echo ">> [0/4] uv.lock pre-flight"
uv lock --locked --quiet \
  || { echo "!! uv.lock is out of sync with pyproject.toml — run uv lock"; exit 1; }
uv audit --locked --preview-features audit-command \
  || { echo "!! uv audit flagged the locked dependencies"; exit 1; }
shipped="$(uv export --locked --quiet --no-dev --no-emit-project --no-hashes \
             --extra bundled --extra simulate --extra qr --extra trezor \
           | sed -nE 's/^([A-Za-z0-9._-]+)==.*/\1/p' | tr 'A-Z_.' 'a-z--')"
# Capture first: a failed dry-run (e.g. PyPI unreachable) must stop the release,
# not read as "nothing to update".
upgrade="$(uv lock --upgrade --dry-run 2>&1)" \
  || { printf '%s\n' "$upgrade"; echo "!! couldn't check uv.lock for updates"; exit 1; }
behind=""
while read -r verb name rest; do
    case "$verb" in Update|Add|Remove) ;; *) continue ;; esac
    norm="$(echo "$name" | tr 'A-Z_.' 'a-z--')"
    if grep -qx -- "$norm" <<<"$shipped"; then
        behind+="   $verb $name $rest"$'\n'
    else
        echo "   (dev-only, not shipped) $verb $name $rest"
    fi
done <<<"$upgrade"
if [ -n "$behind" ]; then
    printf '%s' "$behind"
    if [ "${QETH_ALLOW_STALE_LOCK:-0}" = 1 ]; then
        echo "!! shipped packages are behind — continuing (QETH_ALLOW_STALE_LOCK=1)"
    else
        echo "!! uv.lock is behind on shipped packages: bump it (see above), commit, re-run"
        echo "   — or QETH_ALLOW_STALE_LOCK=1 to release as locked."
        exit 1
    fi
fi

# 1. AppImage — both variants (build-in-container.sh builds normal + verify).
echo ">> [1/4] AppImage (normal + verify) in a manylinux container"
./dist/appimage/build-in-container.sh
cp -f "dist/appimage/out/qeth-$VERSION-x86_64.AppImage" \
      "dist/appimage/out/qeth-verify-$VERSION-x86_64.AppImage" "$OUT/"

# 2. Flatpak — both variants.
echo ">> [2/4] Flatpak (normal)"
./dist/lock-export.sh dist/.pinned/flatpak simulate qr trezor
flatpak-builder --user --force-clean --repo="$HOME/.cache/qeth-flatpak/repo" \
  --state-dir "$HOME/.cache/qeth-flatpak/state" \
  "$HOME/.cache/qeth-flatpak/build" dist/flatpak/io.github.michwill.qeth.yml
flatpak build-bundle "$HOME/.cache/qeth-flatpak/repo" \
  "$OUT/qeth-$VERSION.flatpak" io.github.michwill.qeth
echo ">> [3/4] Flatpak (verify)"
bash dist/flatpak/build-verify.sh
cp -f dist/flatpak/qeth-verify.flatpak "$OUT/qeth-verify-$VERSION.flatpak"

# The 14 Linux assets: 7 formats, each in a normal and a -verify variant.
FEDORA_RELEASES=(44 45)
DEB_TARGETS=("" "_debian13" "_ubuntu2604")   # Mint 22/Ubuntu 24.04, Debian 13, Ubuntu 26.04
LINUX=()
for var in "" "-verify"; do
    for fc in "${FEDORA_RELEASES[@]}"; do LINUX+=("qeth$var-$VERSION-1.fc$fc.x86_64.rpm"); done
    for t in "${DEB_TARGETS[@]}"; do LINUX+=("qeth${var}_${VERSION}${t}_amd64.deb"); done
    LINUX+=("qeth$var-$VERSION-x86_64.AppImage" "qeth$var-$VERSION.flatpak")
done
# Built by CI (macOS + Windows runners) — optional, see the header.
CI_ASSETS=("qeth-$VERSION-macos-arm64.dmg" "qeth-verify-$VERSION-macos-arm64.dmg"
           "qeth-$VERSION-windows-x64.zip" "qeth-$VERSION-windows-x64-setup.exe")

# 3. Collect the externally-built .deb / .rpm (and any CI downloads).
echo ">> [4/4] collecting .deb / .rpm from $INDIR"
if [ "$INDIR" != "$OUT" ]; then
    for f in "${LINUX[@]}" "${CI_ASSETS[@]}"; do
        [ -e "$INDIR/$f" ] && cp -f "$INDIR/$f" "$OUT/"
    done
fi

# 4. Verify the full Linux set.
missing=0
for f in "${LINUX[@]}"; do
    [ -e "$OUT/$f" ] || { echo "  MISSING: $f"; missing=1; }
done
if [ "$missing" = 1 ]; then
    echo "!! Not all ${#LINUX[@]} Linux assets are present. Build the missing .deb/.rpm in"
    echo "   their target environments and drop them in $INDIR (or set QETH_PKG_INDIR)."
    exit 1
fi
size() { ls -la "$@" | awk '{printf "   %-50s %.0f MB\n", $NF, $5/1048576}'; }
size "${LINUX[@]/#/$OUT/}"
echo ">> all ${#LINUX[@]} Linux assets present"

# macOS .dmg / Windows downloads — built by CI, so they may legitimately not be
# ready. Publish without them rather than blocking the whole release on a runner.
ci=()
for f in "${CI_ASSETS[@]}"; do
    if [ -e "$OUT/$f" ]; then
        size "$OUT/$f"
        ci+=("$OUT/$f")
    else
        echo "!! NOTE: no $f — publishing WITHOUT it."
        echo "   Fetch it from the macOS / Windows CI run for this commit (see header)."
    fi
done
echo ">> CI downloads: ${#ci[@]} of ${#CI_ASSETS[@]} included"
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
  "${LINUX[@]/#/$OUT/}" ${ci[0]+"${ci[@]}"}
gh release view "v$VERSION" --json url --jq '">> published: \(.url)"'
