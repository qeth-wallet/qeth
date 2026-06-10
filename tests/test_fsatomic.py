"""atomic_write_text: all-or-nothing file replacement.

``Path.write_text`` truncates before writing, so a crash mid-write tears the
file — fatal for config.json (the accounts list) or a keystore. The helper
must (a) produce the full content, (b) leave the ORIGINAL untouched when the
write fails, and (c) never leak tmp files.
"""
import os

import pytest

from qeth.fsatomic import atomic_write_text


def test_writes_content_and_creates_parents(tmp_path):
    p = tmp_path / "deep" / "nested" / "cache.json"
    atomic_write_text(p, '{"a": 1}')
    assert p.read_text() == '{"a": 1}'


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


def test_mode_is_applied(tmp_path):
    p = tmp_path / "keystore.json"
    atomic_write_text(p, "{}", mode=0o600)
    assert (p.stat().st_mode & 0o777) == 0o600


def test_default_is_owner_only(tmp_path):
    # mkstemp's 0600 default is deliberate: ~/.qeth holds private financial
    # data; a wallet shouldn't write group/world-readable files.
    p = tmp_path / "cache.json"
    atomic_write_text(p, "{}")
    assert (p.stat().st_mode & 0o077) == 0           # no group/world bits
