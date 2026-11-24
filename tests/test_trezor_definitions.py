"""The local copy of Trezor's signed definitions (qeth/trezor_definitions.py)
and the scheduler that keeps it fresh (plugins/wallets/trezor_mirror.py).

``tests/data/trezor_definitions_sample.tar.xz`` holds real, production-signed
definitions cut from Trezor's archive (data.trezor.io definitions.tar.xz,
Last-Modified 2026-11-12): Ethereum's and Base's networks, the CRV token, the
ERC-7730 descriptions of Lido's ``submit`` and Aave's ``supply`` — plus a
slip44 network and a Solana token, which the copy leaves out.
"""

from __future__ import annotations

import io
import lzma
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("trezorlib")

from qeth import trezor_definitions as td  # noqa: E402
from qeth.trezor_definitions import (  # noqa: E402
    BUCKETS, DefinitionMirror, bucket_of, verify_definition,
)

SAMPLE = (Path(__file__).parent / "data" / "trezor_definitions_sample.tar.xz").read_bytes()
CRV = "eth/chain-id/1/token-d533a949740bb3306d119cc777fa900ba034cd52.dat"
LIDO = ("eth/chain-id/1/display-format/"
        "ae7ab96520de3a18e5e111b5eaab095312d7fe84-a1903eab.dat")
BASE = "eth/chain-id/8453/network.dat"


def _members(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:xz") as tf:
        return {m.name: tf.extractfile(m).read()   # type: ignore[union-attr]
                for m in tf if m.isfile()}


def _archive(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as out:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", etag: str | None = None):
        self.status_code = status
        self.body = body
        self.headers = {"ETag": etag} if etag else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i:i + size]


class FakeHttp:
    """data.trezor.io: the archive under an ETag, 304 when it's sent back."""

    def __init__(self, archive: bytes = SAMPLE, etag: str = '"v1"', status: int = 200):
        self.archive, self.etag, self.status = archive, etag, status
        self.requests: list[dict] = []

    def get(self, url, *, headers, timeout, stream):
        assert url == td.ARCHIVE_URL and stream
        self.requests.append(dict(headers))
        if self.status != 200:
            return FakeResponse(self.status)
        if headers.get("If-None-Match") == self.etag:
            return FakeResponse(304)
        return FakeResponse(200, self.archive, self.etag)


@pytest.fixture
def mirror(tmp_path) -> DefinitionMirror:
    return DefinitionMirror(tmp_path / "trezor")


# --- verification ----------------------------------------------------------------


def test_the_sample_is_trezors_production_signed_data():
    members = _members(SAMPLE)
    for name in (CRV, LIDO, BASE):
        assert verify_definition(members[name]), name


@pytest.mark.parametrize("damage", [
    lambda b: b[:-1] + bytes([b[-1] ^ 1]),          # the signature
    lambda b: b[:20] + bytes([b[20] ^ 1]) + b[21:],  # the payload
    lambda b: b[:10],
    lambda b: b"",
    lambda b: b"trzd1" + b"\x00" * 200,
])
def test_a_damaged_definition_doesnt_verify(damage):
    assert not verify_definition(damage(_members(SAMPLE)[CRV]))


@pytest.mark.parametrize(("path", "bucket"), [
    (CRV, 1), (BASE, 8453 % BUCKETS), ("eth/chain-id/32/network.dat", 0),
    ("eth/slip44/60/network.dat", None), ("solana/token/x.dat", None),
    ("eth/chain-id/-1/network.dat", None), ("eth/chain-id/1", None), ("", None),
])
def test_buckets(path, bucket):
    assert bucket_of(path) == bucket


# --- refresh ----------------------------------------------------------------------


def test_a_refresh_builds_the_copy_and_answers_from_it(mirror):
    assert mirror.needs_refresh()
    assert mirror.lookup(CRV) is None             # nothing local yet: no answer
    assert mirror.refresh(session=FakeHttp(), now=1000.0) is True
    members = _members(SAMPLE)
    for name in (CRV, LIDO, BASE):
        assert mirror.lookup(name) == members[name]
    assert mirror.lookup("eth/slip44/60/network.dat") is None
    assert mirror.lookup("eth/chain-id/1/token-" + "00" * 20 + ".dat") is None
    assert not mirror.needs_refresh(now=1000.0 + td.REFRESH_INTERVAL_S - 1)
    assert mirror.needs_refresh(now=1000.0 + td.REFRESH_INTERVAL_S)


def test_an_unchanged_archive_is_one_conditional_request(mirror):
    http = FakeHttp()
    mirror.refresh(session=http, now=1000.0)
    bucket = mirror._bucket_path(1)
    before = bucket.stat().st_mtime_ns
    assert mirror.refresh(session=http, now=2000.0) is False
    assert http.requests[0].get("If-None-Match") is None
    assert http.requests[1]["If-None-Match"] == '"v1"'
    assert "qeth" in http.requests[1]["User-Agent"]
    assert bucket.stat().st_mtime_ns == before
    assert mirror.last_checked() == 2000.0


def test_a_changed_archive_replaces_the_copy(mirror):
    mirror.refresh(session=FakeHttp(), now=1000.0)
    newer = _members(SAMPLE)
    del newer[CRV]
    mirror.refresh(session=FakeHttp(_archive(newer), etag='"v2"'), now=2000.0)
    assert mirror.lookup(CRV) is None
    assert mirror.lookup(LIDO) is not None


def test_a_missing_bucket_forces_a_full_download(mirror):
    http = FakeHttp()
    mirror.refresh(session=http, now=1000.0)
    mirror._bucket_path(5).unlink()
    assert mirror.needs_refresh(now=1000.0)
    mirror.refresh(session=http, now=1001.0)
    assert "If-None-Match" not in http.requests[1]
    assert mirror.lookup(BASE) is not None


@pytest.mark.parametrize("archive", [
    b"not an archive",
    lzma.compress(b"not a tar"),
    _archive({"solana/token/x.dat": b"x", "eth/slip44/60/network.dat": b"y"}),
])
def test_a_bad_archive_fails_and_keeps_the_old_copy(mirror, archive):
    mirror.refresh(session=FakeHttp(), now=1000.0)
    with pytest.raises(RuntimeError):
        mirror.refresh(session=FakeHttp(archive, etag='"v2"'), now=2000.0)
    assert mirror.lookup(CRV) is not None
    assert mirror.last_checked() == 1000.0
    assert not (mirror._dir / "archive.part").exists()


def test_an_oversized_download_is_refused(mirror, monkeypatch):
    monkeypatch.setattr(td, "_MAX_ARCHIVE_BYTES", len(SAMPLE) - 1)
    with pytest.raises(RuntimeError, match="too large"):
        mirror.refresh(session=FakeHttp(), now=1000.0)


def test_an_http_error_raises(mirror):
    with pytest.raises(OSError):
        mirror.refresh(session=FakeHttp(status=503), now=1000.0)
    assert mirror.last_checked() is None


def test_a_tampered_copy_answers_nothing(mirror):
    """A definition that doesn't verify is never sent — the firmware would
    abort the whole signature on it."""
    members = _members(SAMPLE)
    good = members[CRV]
    members[CRV] = good[:-1] + bytes([good[-1] ^ 1])
    mirror.refresh(session=FakeHttp(_archive(members)), now=1000.0)
    assert mirror.lookup(CRV) is None
    assert mirror.lookup(LIDO) is not None


def test_an_unreadable_bucket_answers_nothing(mirror):
    mirror.refresh(session=FakeHttp(), now=1000.0)
    mirror._bucket_path(1).write_bytes(b"garbage")
    assert mirror.lookup(CRV) is None


def test_the_copy_is_rebuilt_from_disk_when_it_changes(mirror, tmp_path):
    mirror.refresh(session=FakeHttp(), now=1000.0)
    assert mirror.lookup(CRV) is not None
    other = DefinitionMirror(tmp_path / "trezor")       # another process, say
    newer = _members(SAMPLE)
    del newer[CRV]
    other.refresh(session=FakeHttp(_archive(newer), etag='"v2"'), now=2000.0)
    assert mirror.lookup(CRV) is None


def test_the_process_wide_copy_follows_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(td, "CACHE_DIR", tmp_path / "a")
    first = td.default_definitions()
    assert td.default_definitions() is first
    monkeypatch.setattr(td, "CACHE_DIR", tmp_path / "b")
    assert td.default_definitions() is not first


def test_the_full_archive_layout_is_split_by_chain(mirror):
    """Every bucket is written, each holding only its own chains."""
    mirror.refresh(session=FakeHttp(), now=1000.0)
    assert all(mirror._bucket_path(b).exists() for b in range(BUCKETS))
    names = {b: set(_members(mirror._bucket_path(b).read_bytes()))
             for b in range(BUCKETS)}
    assert names[1] == {n for n in _members(SAMPLE) if n.startswith("eth/chain-id/1/")}
    assert names[8453 % BUCKETS] == {BASE}


# --- the scheduler ----------------------------------------------------------------


@pytest.fixture
def scheduler_env(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(td, "CACHE_DIR", tmp_path / "trezor")
    store = SimpleNamespace(accounts=[], chains=[])
    started: list = []
    return store, started


def _scheduler(store, started):
    from qeth.plugins.wallets.trezor_mirror import TrezorMirrorScheduler
    return TrezorMirrorScheduler(store, started.append)


def test_scheduler_idle_without_a_trezor_account(scheduler_env):
    store, started = scheduler_env
    store.accounts = [{"address": "0x" + "11" * 20, "source": "ledger"}]
    _scheduler(store, started).check()
    assert started == []


def test_scheduler_refreshes_a_missing_copy_one_at_a_time(scheduler_env):
    store, started = scheduler_env
    store.accounts = [{"address": "0x" + "11" * 20, "source": "trezor"}]
    sched = _scheduler(store, started)
    sched.check()
    assert len(started) == 1
    sched.check()
    assert len(started) == 1
    sched._on_finished()
    sched.check()
    assert len(started) == 2


def test_scheduler_leaves_a_fresh_copy_alone(scheduler_env):
    store, started = scheduler_env
    store.accounts = [{"address": "0x" + "11" * 20, "source": "trezor"}]
    td.default_definitions().refresh(session=FakeHttp())
    _scheduler(store, started).check()
    assert started == []


def test_scheduler_worker_reports_success_and_failure(scheduler_env, monkeypatch, qtbot):
    from qeth.plugins.wallets.trezor_mirror import TrezorMirrorWorker

    monkeypatch.setattr(DefinitionMirror, "refresh", lambda self: True)
    worker = TrezorMirrorWorker()
    with qtbot.waitSignal(worker.done, timeout=5000) as blocker:
        worker.start()
    worker.wait(5000)
    assert blocker.args == [True]

    def boom(self):
        raise RuntimeError("offline")
    monkeypatch.setattr(DefinitionMirror, "refresh", boom)
    worker = TrezorMirrorWorker()
    with qtbot.waitSignal(worker.failed, timeout=5000) as blocker:
        worker.start()
    worker.wait(5000)
    assert blocker.args == ["offline"]
