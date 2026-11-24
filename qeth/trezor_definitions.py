"""Trezor's signed definitions — network and token names, ERC-7730 descriptions
of contract calls — kept locally so signing never asks a server what is being
signed (docs/trezor-clear-signing.md).

While it signs, the firmware asks the host for what it needs to show the
transaction (``EthereumDefinitionRequest``: the chain, the called contract and
its function selector). Answering that by downloading the one file from
data.trezor.io — what qeth used to do — told SatoshiLabs the chain, contract
and function of every transaction. Trezor publishes every definition in one
archive instead; qeth downloads it on a schedule (an unchanged archive is one
request answered 304) and answers the device from the local copy.

* The archive is split by chain into ``BUCKETS`` xz-compressed tar files: xz
  shares the Merkle proofs between definitions, so one file per definition
  would take ~3× the disk, and one bucket loads in well under a second.
* Every definition is checked on the host against Trezor's production keys
  before it is sent — the firmware aborts the whole signature on a bad one.
* A definition missing locally is answered with none: the device shows the
  raw value, as before. There is no online fallback — that's the leak.

Qt-free; the refresh runs on a worker thread, lookups on any thread.
"""

from __future__ import annotations

import io
import json
import logging
import lzma
import tarfile
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .fsatomic import atomic_write_bytes, atomic_write_text

log = logging.getLogger(__name__)

ARCHIVE_URL = "https://data.trezor.io/firmware/definitions/definitions.tar.xz"
CACHE_DIR = Path.home() / ".qeth" / "trezor"
# How often the archive is revalidated. An unchanged archive costs one request.
REFRESH_INTERVAL_S = 24 * 3600
BUCKETS = 32
_HTTP_TIMEOUT_S = 120.0
# Safety stops against a broken download (the archive is ~14 MB, ~41 MB
# unpacked, its definitions well under 1 KB each).
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_DEFINITION_BYTES = 64 * 1024
_MAX_KEPT_BYTES = 512 * 1024 * 1024
# Buckets kept unpacked in memory (Ethereum mainnet's is ~10 MB).
_CACHED_BUCKETS = 2


def bucket_of(path: str) -> int | None:
    """The bucket of an archive path (``eth/chain-id/<id>/…``), or ``None``
    for what isn't kept (other coins, the slip44 networks)."""
    parts = path.split("/")
    if len(parts) < 4 or parts[0] != "eth" or parts[1] != "chain-id":
        return None
    if not parts[2].isdigit():
        return None
    return int(parts[2]) % BUCKETS


def verify_definition(blob: bytes) -> bool:
    """Trezor's production signature over the definition's Merkle root checks
    out — what the firmware checks before anything else."""
    from trezorlib import cosi, merkle_tree
    from trezorlib._ed25519 import SignatureMismatch
    from trezorlib.definitions import (
        DEFINITIONS_PUBLIC_KEYS, DEFINITIONS_SIGS_REQUIRED, Definition,
    )
    try:
        definition = Definition.parse(blob)
        payload = definition.payload.build()
    except Exception:   # noqa: BLE001 — construct's own errors: malformed bytes
        return False
    root = merkle_tree.evaluate_proof(payload, definition.proof)
    try:
        cosi.verify(definition.signature, root, DEFINITIONS_SIGS_REQUIRED,
                    DEFINITIONS_PUBLIC_KEYS, definition.sigmask)
    except (SignatureMismatch, ValueError):
        return False
    return True


class DefinitionMirror:
    """The on-disk copy: ``<root>/definitions/eth-NN.tar.xz`` (the archive's
    ``eth/chain-id/<id>/…`` files, chain id mod ``BUCKETS``) and
    ``<root>/definitions/state.json`` (the archive's ETag, last check)."""

    def __init__(self, root: Path) -> None:
        self._dir = root / "definitions"
        self._lock = threading.Lock()
        self._buckets: OrderedDict[int, tuple[float, dict[str, bytes]]] = OrderedDict()

    def _bucket_path(self, bucket: int) -> Path:
        return self._dir / f"eth-{bucket:02d}.tar.xz"

    # --- lookup ------------------------------------------------------------

    def lookup(self, path: str) -> bytes | None:
        """The definition at ``path`` (``eth/chain-id/1/token-<addr>.dat``),
        if the copy has it and Trezor's signature verifies. Local only —
        never touches the network."""
        bucket = bucket_of(path)
        if bucket is None:
            return None
        blob = self._bucket(bucket).get(path)
        if blob is None:
            return None
        if not verify_definition(blob):
            log.warning("trezor definitions: bad signature on %s", path)
            return None
        return blob

    def _bucket(self, bucket: int) -> dict[str, bytes]:
        path = self._bucket_path(bucket)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        with self._lock:
            cached = self._buckets.get(bucket)
            if cached is not None and cached[0] == mtime:
                self._buckets.move_to_end(bucket)
                return cached[1]
        try:
            entries = _read_bucket(path)
        except (OSError, tarfile.TarError, lzma.LZMAError, EOFError) as e:
            log.warning("trezor definitions: unreadable %s: %s", path.name, e)
            entries = {}
        with self._lock:
            self._buckets[bucket] = (mtime, entries)
            self._buckets.move_to_end(bucket)
            while len(self._buckets) > _CACHED_BUCKETS:
                self._buckets.popitem(last=False)
        return entries

    # --- refresh -----------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        try:
            state = json.loads((self._dir / "state.json").read_text())
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def last_checked(self) -> float | None:
        checked = self._state().get("checked_at")
        return float(checked) if isinstance(checked, (int, float)) else None

    def _complete(self) -> bool:
        return all(self._bucket_path(b).exists() for b in range(BUCKETS))

    def needs_refresh(self, *, now: float | None = None) -> bool:
        """Stale (older than ``REFRESH_INTERVAL_S``), or not there at all."""
        checked = self.last_checked()
        now = time.time() if now is None else now
        return checked is None or now - checked >= REFRESH_INTERVAL_S \
            or not self._complete()

    def refresh(self, *, session: Any = None, now: float | None = None) -> bool:
        """Bring the copy up to date; True when a new archive was downloaded.
        One conditional request when nothing changed. Raises on a failure
        (the caller retries later); the old copy stays until a new one is
        complete."""
        import requests
        from . import USER_AGENT
        http = session or requests.Session()
        now = time.time() if now is None else now
        state = self._state()
        headers = {"User-Agent": USER_AGENT}
        etag = state.get("etag")
        if isinstance(etag, str) and self._complete():
            headers["If-None-Match"] = etag
        self._dir.mkdir(parents=True, exist_ok=True)
        part = self._dir / "archive.part"
        with http.get(ARCHIVE_URL, headers=headers, timeout=_HTTP_TIMEOUT_S,
                      stream=True) as r:
            if r.status_code == 304:
                state["checked_at"] = now
                self._write_state(state)
                return False
            r.raise_for_status()
            size = 0
            with part.open("wb") as f:
                for chunk in r.iter_content(1 << 16):
                    size += len(chunk)
                    if size > _MAX_ARCHIVE_BYTES:
                        raise RuntimeError("Trezor's definitions archive is too large")
                    f.write(chunk)
            etag = r.headers.get("ETag")
        try:
            count = self._split(part)
        finally:
            part.unlink(missing_ok=True)
        self._write_state({"etag": etag, "checked_at": now, "built_at": now,
                           "definitions": count})
        log.info("trezor definitions: %d Ethereum definitions in %d buckets",
                 count, BUCKETS)
        return True

    def _split(self, archive: Path) -> int:
        """The archive's Ethereum definitions into the buckets (each written
        atomically). Returns how many there are."""
        groups: dict[int, dict[str, bytes]] = {b: {} for b in range(BUCKETS)}
        kept = 0
        try:
            with tarfile.open(archive, "r|xz") as tf:
                for member in tf:
                    bucket = bucket_of(member.name)
                    if (bucket is None or not member.isfile()
                            or member.size > _MAX_DEFINITION_BYTES):
                        continue
                    f = tf.extractfile(member)
                    if f is None:
                        continue
                    groups[bucket][member.name] = f.read()
                    kept += member.size
                    if kept > _MAX_KEPT_BYTES:
                        raise RuntimeError("Trezor's definitions archive is too large")
        except (tarfile.TarError, lzma.LZMAError, EOFError) as e:
            raise RuntimeError(f"Trezor's definitions archive is unreadable: {e}") from e
        count = sum(len(files) for files in groups.values())
        if not count:
            raise RuntimeError("Trezor's definitions archive has no Ethereum definitions")
        for bucket, files in groups.items():
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:xz") as out:
                for name in sorted(files):
                    info = tarfile.TarInfo(name)
                    info.size = len(files[name])
                    out.addfile(info, io.BytesIO(files[name]))
            atomic_write_bytes(self._bucket_path(bucket), buf.getvalue())
        return count

    def _write_state(self, state: dict[str, Any]) -> None:
        atomic_write_text(self._dir / "state.json", json.dumps(state))


def _read_bucket(path: Path) -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    with tarfile.open(path, "r:xz") as tf:
        for member in tf:
            if not member.isfile() or member.size > _MAX_DEFINITION_BYTES:
                continue
            f = tf.extractfile(member)
            if f is not None:
                entries[member.name] = f.read()
    return entries


_MIRROR: DefinitionMirror | None = None
_MIRROR_ROOT: Path | None = None
_MIRROR_LOCK = threading.Lock()


def default_definitions() -> DefinitionMirror:
    """The process-wide copy under ``CACHE_DIR`` (re-read on each call, so a
    test redirecting it gets a fresh instance)."""
    global _MIRROR, _MIRROR_ROOT
    with _MIRROR_LOCK:
        if _MIRROR is None or _MIRROR_ROOT != CACHE_DIR:
            _MIRROR, _MIRROR_ROOT = DefinitionMirror(CACHE_DIR), CACHE_DIR
        return _MIRROR
