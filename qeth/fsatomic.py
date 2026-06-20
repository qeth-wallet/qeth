"""Atomic file writes — tmp-in-same-dir + fsync + ``os.replace``.

``Path.write_text`` truncates the target before writing, so a crash or
power loss mid-write leaves a torn (often empty) file. For the caches
that's a stale annoyance; for ``config.json`` (the accounts list) or a
keystore it's data loss. ``os.replace`` is atomic on POSIX **when source
and target share a filesystem**, hence the tmp file lives next to the
target — never in /tmp.

Files come out 0600 (``mkstemp``'s default) unless ``mode`` is given —
deliberately tighter than the usual 0644: everything under ``~/.qeth``
is private financial data, and a wallet has no business writing
group/world-readable files.
"""

import os
import tempfile
from pathlib import Path


def _atomic_write(path: Path, data: bytes, mode: int | None) -> None:
    """Shared core: write ``data`` to ``path`` atomically + durably. The tmp
    file inherits ``mkstemp``'s 0600 unless ``mode`` overrides it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # Make the rename itself durable (the directory entry).
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, *,
                      mode: int | None = None) -> None:
    """Write ``text`` to ``path`` atomically (all-or-nothing).

    The data is fsynced before the rename and the directory entry after
    it, so the file is durable — not just consistent — once this returns.
    On any failure the half-written tmp file is removed and ``path`` is
    left exactly as it was.
    """
    _atomic_write(path, text.encode("utf-8"), mode)


def atomic_write_bytes(path: Path, data: bytes, *,
                       mode: int | None = None) -> None:
    """Bytes counterpart of :func:`atomic_write_text` — for the binary caches
    (icons, price/chain/token-list blobs). Same 0600-by-default privacy and
    all-or-nothing durability, so every file under ``~/.qeth`` is owner-only
    even if the directory mode is ever loosened (issue #3)."""
    _atomic_write(path, data, mode)
