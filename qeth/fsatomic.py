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
from typing import Optional


def atomic_write_text(path: Path, text: str, *,
                      mode: Optional[int] = None) -> None:
    """Write ``text`` to ``path`` atomically (all-or-nothing).

    The data is fsynced before the rename and the directory entry after
    it, so the file is durable — not just consistent — once this returns.
    On any failure the half-written tmp file is removed and ``path`` is
    left exactly as it was.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
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
