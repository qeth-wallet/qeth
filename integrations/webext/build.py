#!/usr/bin/env python3
"""Build (and optionally AMO-sign) the qeth browser extension.

Stdlib only — no node, no npm, matching the project's build story.

    python build.py                # → out/qeth-webext-<version>.zip
    python build.py sign           # build, then sign the Firefox .xpi via AMO
                                   #   (unlisted / self-distribution) if the
                                   #   QETH_AMO_JWT_ISSUER / _SECRET env vars
                                   #   are set; otherwise just builds.

One zip serves both stores: Chrome Web Store takes it as-is, and a Firefox
`.xpi` is just a zip — AMO signs that same package. Chrome is installed
unpacked during development and needs no signing.

The AMO signing flow (JWT auth + submission API) is exercised only with real
credentials, so it is env-gated and untested offline; the zip build is covered
by tests/test_webext.py.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

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
]

AMO_API = "https://addons.mozilla.org/api/v5"


def _manifest() -> dict:
    return json.loads((HERE / "manifest.json").read_text())


def _verify_tree() -> None:
    """Fail loudly before packaging a broken tree."""
    # The shared provider must not have drifted from the Falkon copy.
    falkon = HERE.parent / "falkon" / "qeth_connector" / "provider.js"
    if falkon.exists() and (HERE / "provider.js").read_bytes() != falkon.read_bytes():
        raise SystemExit(
            "provider.js has drifted from the Falkon copy — re-mirror it "
            "(cp integrations/falkon/qeth_connector/provider.js "
            "integrations/webext/provider.js).")
    # Every packaged file exists.
    missing = [f for f in PACKAGE_FILES if not (HERE / f).is_file()]
    if missing:
        raise SystemExit("missing packaged files: " + ", ".join(missing))


def build(out_dir: Path) -> Path:
    _verify_tree()
    version = _manifest()["version"]
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"qeth-webext-{version}.zip"
    # Deterministic zip: fixed member order, fixed timestamp.
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in PACKAGE_FILES:
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, (HERE / rel).read_bytes())
    print(f"built {zip_path}  ({zip_path.stat().st_size} bytes)")
    return zip_path


# --- AMO signing (Firefox .xpi, unlisted / self-distribution) -----------

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
        raise SystemExit(f"AMO {method} {url} → {e.code}: {e.read().decode(errors='replace')}")
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


def sign(zip_path: Path, out_dir: Path) -> Path | None:
    issuer = os.environ.get("QETH_AMO_JWT_ISSUER")
    secret = os.environ.get("QETH_AMO_JWT_SECRET")
    if not issuer or not secret:
        print("AMO credentials not set (QETH_AMO_JWT_ISSUER / QETH_AMO_JWT_SECRET) "
              "— skipping signing. The zip loads as a temporary add-on; set them "
              "to produce a permanently installable signed .xpi.")
        return None

    guid = _manifest()["browser_specific_settings"]["gecko"]["id"]
    token = _jwt(issuer, secret)

    # 1. upload the package (unlisted channel).
    body, ctype = _multipart({"channel": "unlisted"}, "upload",
                             zip_path.name, zip_path.read_bytes())
    up = _api("POST", "/addons/upload/", token, data=body, headers={"Content-Type": ctype})
    up_uuid = up["uuid"]
    print(f"AMO upload {up_uuid} — validating…")
    _poll(f"/addons/upload/{up_uuid}/", token, want="valid")

    # 2. create a version (creates the add-on on first run, keyed by the guid).
    token = _jwt(issuer, secret)
    payload = json.dumps({"upload": up_uuid}).encode()
    try:
        ver = _api("POST", f"/addons/addon/{guid}/versions/", token,
                   data=payload, headers={"Content-Type": "application/json"})
    except SystemExit:
        token = _jwt(issuer, secret)
        payload = json.dumps({"version": {"upload": up_uuid}}).encode()
        ver = _api("POST", "/addons/addon/", token,
                   data=payload, headers={"Content-Type": "application/json"})

    # 3. wait for the signed file and download it.
    ver_url = ver.get("url") or f"{AMO_API}/addons/addon/{guid}/versions/{ver['id']}/"
    token = _jwt(issuer, secret)
    info = _poll(ver_url, token, want="file")
    file_info = info["file"]
    if not file_info.get("url"):
        raise SystemExit("signed file has no download url yet: " + json.dumps(file_info)[:300])

    token = _jwt(issuer, secret)
    req = urllib.request.Request(file_info["url"])
    req.add_header("Authorization", f"JWT {token}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        xpi_bytes = resp.read()
    xpi_path = out_dir / f"qeth-webext-{_manifest()['version']}.xpi"
    xpi_path.write_bytes(xpi_bytes)
    print(f"signed {xpi_path}  ({xpi_path.stat().st_size} bytes)")
    return xpi_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", nargs="?", default="build", choices=["build", "sign"])
    ap.add_argument("--out", type=Path, default=HERE / "out")
    args = ap.parse_args(argv)
    zip_path = build(args.out)
    if args.command == "sign":
        sign(zip_path, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
