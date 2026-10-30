"""atomic_write_text: all-or-nothing file replacement.

``Path.write_text`` truncates before writing, so a crash mid-write tears the
file — fatal for config.json (the accounts list) or a keystore. The helper
must (a) produce the full content, (b) leave the ORIGINAL untouched when the
write fails, and (c) never leak tmp files.
"""
import os
import sys

import pytest

from qeth.fsatomic import atomic_write_bytes, atomic_write_text

# Permission bits are POSIX: on Windows privacy is the profile's ACL and chmod
# only toggles read-only (see qeth/fsatomic.py).
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


def test_writes_content_and_creates_parents(tmp_path):
    p = tmp_path / "deep" / "nested" / "cache.json"
    atomic_write_text(p, '{"a": 1}')
    assert p.read_text() == '{"a": 1}'


def test_bytes_round_trips(tmp_path):
    # The binary caches (icons, price/chain/tokenlist blobs) go through the
    # bytes variant — the \r\n must survive (no text-mode translation).
    p = tmp_path / "deep" / "icon.png"
    blob = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
    atomic_write_bytes(p, blob)
    assert p.read_bytes() == blob


@posix_only
def test_bytes_is_owner_only(tmp_path):
    # Like the text variant it's 0600 by default, so the whole ~/.qeth tree
    # is owner-only even at the file level (issue #3).
    p = tmp_path / "icon.png"
    atomic_write_bytes(p, b"\x89PNG")
    assert (p.stat().st_mode & 0o077) == 0           # no group/world bits


def test_bytes_replaces_atomically_no_tmp_leftover(tmp_path):
    p = tmp_path / "icon.png"
    p.write_bytes(b"OLD")
    atomic_write_bytes(p, b"NEW")
    assert p.read_bytes() == b"NEW"
    assert os.listdir(tmp_path) == ["icon.png"]      # no .tmp debris


def test_replaces_existing_atomically_no_tmp_leftover(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("OLD")
    atomic_write_text(p, "NEW")
    assert p.read_text() == "NEW"
    assert os.listdir(tmp_path) == ["config.json"]   # no .tmp debris


def test_failure_leaves_original_intact_and_cleans_tmp(tmp_path, monkeypatch):
    p = tmp_path / "config.json"
    p.write_text("PRECIOUS")

    def boom(src, dst):
        raise OSError("disk pulled")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(p, "HALF-WRI")
    assert p.read_text() == "PRECIOUS"               # original untouched
    assert os.listdir(tmp_path) == ["config.json"]   # tmp cleaned up


@posix_only
def test_mode_is_applied(tmp_path):
    p = tmp_path / "keystore.json"
    atomic_write_text(p, "{}", mode=0o600)
    assert (p.stat().st_mode & 0o777) == 0o600


@posix_only
def test_default_is_owner_only(tmp_path):
    # mkstemp's 0600 default is deliberate: ~/.qeth holds private financial
    # data; a wallet shouldn't write group/world-readable files.
    p = tmp_path / "cache.json"
    atomic_write_text(p, "{}")
    assert (p.stat().st_mode & 0o077) == 0           # no group/world bits
