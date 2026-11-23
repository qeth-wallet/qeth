#!/usr/bin/env python3
"""Build (and optionally submit to AMO) the qeth browser extension.

Stdlib only — no node, no npm, matching the project's build story.

    python build.py                # → ../chrome/qeth-<version>-chrome.zip
                                   #   (committed Chrome distributable)
                                   #   + out/qeth-<version>-firefox.zip
    python build.py sync           # stamp the app version (qeth/__init__.py
                                   #   __version__) into manifest.json + the
                                   #   Falkon connector's metadata.desktop
    python build.py submit [--release-notes F] [--reviewer-notes F]
                                   # build, then submit the Firefox zip as a
                                   #   new LISTED version of the AMO listing
                                   #   (needs QETH_AMO_JWT_ISSUER / _SECRET;
                                   #   without them, just builds)

The committed Chrome package lives in ``extensions/chrome/``; Firefox commits
nothing, since AMO signs and distributes the listed version. The shared source
is here in ``extensions/webext/``. Intermediate zips go to ``out/``
(gitignored).

Two packages, one codebase: Chrome uses an MV3 service-worker background;
Firefox MV3 has no service-worker support and needs the event-page form
(``background.scripts``). Declaring both keys in one manifest makes Chrome log
``'background.scripts' requires manifest version of 2 or lower``, so the source
manifest is Chrome-clean (service_worker only — that's the unpacked dev target)
and the Firefox variant is generated here (``_manifest_for("firefox")``).

The extension version always equals the app version (``qeth/__init__.py``
``__version__``): ``build`` stamps it into every packaged manifest, and ``sync``
writes it back into the source files so an unpacked dev load shows it too
(tests/test_webext.py gates that they don't drift).

The AMO signing flow (JWT auth + submission API) is exercised only with real
credentials, so it is env-gated and untested offline; the zip build is covered
by tests/test_webext.py.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FALKON_DESKTOP = HERE.parent / "falkon" / "qeth_connector" / "metadata.desktop"
# The committed Chrome distributable lives in a sibling dir (the shared source
# is here in webext/). Firefox has none: its build is the AMO listing.
CHROME_DIR = HERE.parent / "chrome"

# Exactly what ships. Anything not listed (README, .gitignore, out/, build.py,
# the source SVG) stays out of the package.
PACKAGE_FILES = [
    "manifest.json",
    "config.js",
    "provider.js",
    "relay.js",
    "background.js",
    "popup.html",
    "popup.js",
    "icons/icon16.png", "icons/icon32.png", "icons/icon48.png", "icons/icon128.png",
    "icons/icon16-off.png", "icons/icon32-off.png",
    "icons/icon48-off.png", "icons/icon128-off.png",
    "tronweb/TronWeb.js", "tronweb/TronWeb.js.LICENSE.txt",
    "tronweb/LICENSE", "tronweb/SOURCE.txt",
]

# The vendored TronWeb (npm tronweb@6.5.1 dist/TronWeb.js, UNMODIFIED — store
# reviewers verify third-party code against its release by checksum). Pinned,
# so an accidental edit, or a bump that forgets the pin and SOURCE.txt, can't
# ship. The Falkon connector carries the same file.
TRONWEB_FILE = "tronweb/TronWeb.js"
TRONWEB_SHA256 = "f35b43d3e67bda1442f073a30d90a62871b5390ecd11808cd55ed201b9fb1e6f"

AMO_API = "https://addons.mozilla.org/api/v5"


def app_version() -> str:
    """The single source of truth — ``qeth/__init__.py`` ``__version__``, read
    statically (no import: qeth pulls in PySide6 and the eth stack)."""
    src = (REPO / "qeth" / "__init__.py").read_text()
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', src, re.M)
    if not m:
        raise SystemExit("could not read __version__ from qeth/__init__.py")
    return m.group(1)


def _manifest() -> dict:
    return json.loads((HERE / "manifest.json").read_text())


def _manifest_for(target: str) -> dict:
    """The manifest for a browser target, version-stamped from the app version.

    ``chrome`` is the source manifest as-is (MV3 service worker). ``firefox``
    swaps the background to the event-page form (``scripts``) — Firefox MV3 has
    no service-worker background — so neither store sees a key it warns on."""
    m = copy.deepcopy(_manifest())
    m["version"] = app_version()
    if target == "firefox":
        m["background"] = {"scripts": ["background.js"]}
    elif target != "chrome":
        raise SystemExit(f"unknown target {target!r} (chrome|firefox)")
    return m


def _verify_tree() -> None:
    """Fail loudly before packaging a broken tree."""
    # The shared provider must not have drifted from the Falkon copy.
    falkon = HERE.parent / "falkon" / "qeth_connector" / "provider.js"
    if falkon.exists() and (HERE / "provider.js").read_bytes() != falkon.read_bytes():
        raise SystemExit(
            "provider.js has drifted from the Falkon copy — re-mirror it "
            "(cp extensions/falkon/qeth_connector/provider.js "
            "extensions/webext/provider.js).")
    # Every packaged file exists.
    missing = [f for f in PACKAGE_FILES if not (HERE / f).is_file()]
    if missing:
        raise SystemExit("missing packaged files: " + ", ".join(missing))
    # TronWeb is the pinned upstream bundle, and Falkon ships the same one.
    tronweb = (HERE / TRONWEB_FILE).read_bytes()
    if hashlib.sha256(tronweb).hexdigest() != TRONWEB_SHA256:
        raise SystemExit(
            f"{TRONWEB_FILE} isn't the pinned upstream bundle — it must be the "
            "unmodified npm dist; update TRONWEB_SHA256 + tronweb/SOURCE.txt "
            "when bumping it.")
    falkon_tw = HERE.parent / "falkon" / "qeth_connector" / TRONWEB_FILE
    if falkon_tw.exists() and falkon_tw.read_bytes() != tronweb:
        raise SystemExit(f"the Falkon connector's {TRONWEB_FILE} has drifted "
                         "from extensions/webext's — re-mirror it.")


def _member_bytes(rel: str, target: str) -> bytes:
    """The bytes to pack for ``rel`` — the generated manifest for the target,
    the on-disk file for everything else."""
    if rel == "manifest.json":
        return (json.dumps(_manifest_for(target), indent=2) + "\n").encode()
    return (HERE / rel).read_bytes()


def _zip(zip_path: Path, target: str) -> Path:
    # Deterministic zip: fixed member order, fixed timestamp.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in PACKAGE_FILES:
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, _member_bytes(rel, target))
    print(f"built {zip_path}  ({zip_path.stat().st_size} bytes)")
    return zip_path


def build(out_dir: Path, target: str = "chrome") -> Path:
    """Package one browser's zip. Chrome is the default (the unpacked dev
    target); the version is always the app version, stamped into the manifest."""
    _verify_tree()
    version = app_version()
    out_dir.mkdir(parents=True, exist_ok=True)
    return _zip(out_dir / f"qeth-{version}-{target}.zip", target)


def _replace_dist(dest: Path, target: str) -> Path:
    """Refresh a committed per-browser distributable: drop any prior
    ``qeth-*`` package so only the current version stays tracked, then build."""
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.glob("qeth-*"):
        old.unlink()
    return build(dest, target)


def write_unpacked(dest_dir: Path, target: str) -> Path:
    """Materialise an unpacked extension dir for ``target`` (used by the Firefox
    browser test, whose source manifest is Chrome-only)."""
    _verify_tree()
    for rel in PACKAGE_FILES:
        dst = dest_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(_member_bytes(rel, target))
    return dest_dir


def sync_versions() -> str:
    """Write the app version into the source manifest.json and the Falkon
    connector's metadata.desktop so both extensions track it. Only rewrites a
    file when it's actually stale (keeps ``build`` side-effect-free in tests)."""
    version = app_version()
    mpath = HERE / "manifest.json"
    manifest = json.loads(mpath.read_text())
    if manifest.get("version") != version:
        manifest["version"] = version
        mpath.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"synced manifest.json → {version}")
    if FALKON_DESKTOP.exists():
        lines = FALKON_DESKTOP.read_text().splitlines()
        want = f"X-Falkon-Version={version}"
        for i, ln in enumerate(lines):
            if ln.startswith("X-Falkon-Version=") and ln != want:
                lines[i] = want
                FALKON_DESKTOP.write_text("\n".join(lines) + "\n")
                print(f"synced Falkon metadata.desktop → {version}")
                break
    return version


# --- AMO submission (Firefox, the listed channel) ------------------------
#
# qeth's Firefox build IS the AMO listing (the manifest's gecko id). AMO
# reviews a listed version before it signs it and pushes it to installed
# copies, which can take days, so ``submit`` uploads, creates the version
# and reports its status — it doesn't wait for the signature, and nothing is
# committed for Firefox.

def _jwt(issuer: str, secret: str) -> str:
    """A short-lived HS256 JWT for the AMO API (no PyJWT dependency)."""
    def seg(obj: dict) -> bytes:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=")
    now = int(time.time())
    head = seg({"alg": "HS256", "typ": "JWT"})
    body = seg({"iss": issuer, "jti": uuid.uuid4().hex, "iat": now, "exp": now + 60})
    signing_input = head + b"." + body
    sig = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")).decode()


class AmoError(SystemExit):
    """An AMO API call failed, with its HTTP status. Subclasses SystemExit so an
    unhandled one aborts with the message."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _api(method: str, path: str, token: str, *,
         data: bytes | None = None, headers: dict | None = None) -> dict:
    url = path if path.startswith("http") else f"{AMO_API}{path}"
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"JWT {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise AmoError(
            f"AMO {method} {url} → {e.code}: {e.read().decode(errors='replace')}",
            code=e.code)
    return json.loads(body) if body else {}


def _multipart(fields: dict[str, str], file_field: str,
               filename: str, file_bytes: bytes) -> tuple[bytes, str]:
    boundary = "----qeth" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode()
    out += f"--{boundary}\r\n".encode()
    out += (f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\n').encode()
    out += b"Content-Type: application/zip\r\n\r\n"
    out += file_bytes + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _poll(path: str, token: str, *, want: str, tries: int = 60, delay: int = 5) -> dict:
    for _ in range(tries):
        info = _api("GET", path, token)
        if info.get(want):
            return info
        if info.get("processed") and not info.get("valid", True):
            raise SystemExit(f"AMO validation failed: {json.dumps(info)[:500]}")
        time.sleep(delay)
    raise SystemExit(f"AMO polling timed out waiting for {want!r} at {path}")


def _version_file(version: dict) -> dict:
    """The file record on a version-detail response. The field is singular
    ``file`` in the current API and was a ``files`` list in older revisions —
    accept both so a server-side rename doesn't break signing."""
    f = version.get("file")
    if f is None and isinstance(version.get("files"), list) and version["files"]:
        f = version["files"][0]
    return f or {}


def _existing_version(guid: str, version: str, token: str) -> dict | None:
    """``version`` as AMO already holds it under ``guid``, on either channel.
    A version string belongs to whichever channel takes it first and can't be
    reused, so one that exists can't be submitted. Unlisted versions only
    appear with the explicit filter."""
    vers = _api("GET",
                f"/addons/addon/{guid}/versions/?filter=all_with_unlisted",
                token)
    for v in vers.get("results", []):
        if v.get("version") == version:
            return v
    return None


def submit(zip_path: Path, *, release_notes: Path | None = None,
           reviewer_notes: Path | None = None) -> dict | None:
    """Submit ``zip_path`` as a new LISTED version of the manifest's add-on.

    ``release_notes`` (shown on the listing) and ``reviewer_notes`` (AMO's
    "notes for reviewers": what changed, why a permission is needed) are
    plain-text files, written per version. The license carries over from the
    previous listed version; compatibility comes from the manifest."""
    issuer = os.environ.get("QETH_AMO_JWT_ISSUER")
    secret = os.environ.get("QETH_AMO_JWT_SECRET")
    if not issuer or not secret:
        print("AMO credentials not set (QETH_AMO_JWT_ISSUER / QETH_AMO_JWT_SECRET) "
              f"— not submitting. Upload {zip_path} in the AMO Developer Hub "
              "instead, as an \"On this site\" version.")
        return None

    guid = _manifest()["browser_specific_settings"]["gecko"]["id"]
    version = app_version()
    # Read the notes before uploading anything: a bad path must not cost an
    # upload (AMO throttles writes, and failed ones still count).
    fields: dict = {}
    if release_notes:
        fields["release_notes"] = {"en-US": release_notes.read_text().strip()}
    if reviewer_notes:
        fields["approval_notes"] = reviewer_notes.read_text().strip()

    # A fresh short-lived JWT per call (they expire in 60s and validation polls).
    def token():
        return _jwt(issuer, secret)

    held = _existing_version(guid, version, token())
    if held:
        raise SystemExit(
            f"AMO already has {guid} {version} (channel {held.get('channel')!r}, "
            f"file {_version_file(held).get('status')!r}) — a version string "
            "can't be submitted twice; bump __version__.")

    # 1. upload the package to the listed channel and wait for validation.
    body, ctype = _multipart({"channel": "listed"}, "upload",
                             zip_path.name, zip_path.read_bytes())
    up = _api("POST", "/addons/upload/", token(), data=body,
              headers={"Content-Type": ctype})
    print(f"AMO upload {up['uuid']} — validating…")
    _poll(f"/addons/upload/{up['uuid']}/", token(), want="valid")

    # 2. make it a version of the listing, which queues it for review.
    fields["upload"] = up["uuid"]
    ver = _api("POST", f"/addons/addon/{guid}/versions/", token(),
               data=json.dumps(fields).encode(),
               headers={"Content-Type": "application/json"})
    print(f"submitted {guid} {version} (listed) — file status "
          f"{_version_file(ver).get('status')!r}. AMO signs it and updates "
          "installed copies once review approves it; current_version at "
          f"{AMO_API}/addons/addon/{guid}/ says when.")
    return ver


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="build",
                    choices=["build", "sync", "submit"])
    ap.add_argument("--out", type=Path, default=HERE / "out")
    ap.add_argument("--release-notes", type=Path, metavar="FILE",
                    help="submit: this version's release notes (plain text, "
                         "shown on the listing)")
    ap.add_argument("--reviewer-notes", type=Path, metavar="FILE",
                    help="submit: notes for the AMO reviewer — what changed, "
                         "why a new permission is needed")
    args = ap.parse_args(argv)
    if args.command == "sync":
        sync_versions()
        return 0
    # Keep the source files' displayed version in step. The Chrome distributable
    # IS the packaged zip → commit it to extensions/chrome/. The Firefox zip is
    # what goes to the AMO listing; nothing is committed for it.
    sync_versions()
    _replace_dist(CHROME_DIR, "chrome")
    fx_zip = build(args.out, "firefox")
    if args.command == "submit":
        submit(fx_zip, release_notes=args.release_notes,
               reviewer_notes=args.reviewer_notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
