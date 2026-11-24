"""The local mirror of Ledger's signed token metadata (qeth/ledger_metadata.py)
and the scheduler that keeps it fresh (plugins/wallets/ledger_mirror.py).

Hermetic: Ledger's CAL service is a scripted fake. The two real descriptors
below are Ledger's prod-signed USDC / USDT (captured 2026-11-24), so host-side
signature verification runs against Ledger's actual key.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from qeth import ledger_metadata
from qeth.ledger_metadata import (
    REFRESH_INTERVAL_S, TokenMirror, erc20_call_kind, parse_descriptor,
    verify_legacy_signature,
)

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
USDC_DATA = bytes.fromhex(
    "55534443a0b86991c6218b36c1d19d4a2e9eb0ce3606eb480000000600000001")
USDC_SIG = bytes.fromhex(
    "3045022100b2e358726e4e6a6752cf344017c0e9d45b9a904120758d45f61b2804f9ad5299"
    "022015161ef28d8c4481bd9432c13562def9cce688bcfec896ef244c9a213f106cdd")
USDT_DATA = bytes.fromhex(
    "55534454dac17f958d2ee523a2206206994597c13d831ec70000000600000001")
USDT_SIG = bytes.fromhex(
    "3044022078c66ccea3e4dedb15a24ec3c783d7b582cd260daf62fd36afe9a8212a344aed"
    "0220160ba8c1c4b6a8aa6565bed20632a091aeeeb7bfdac67fc6589a6031acbf511c")


# --- descriptors ------------------------------------------------------------------


def test_real_ledger_signatures_verify():
    assert verify_legacy_signature(USDC_DATA, USDC_SIG)
    assert verify_legacy_signature(USDT_DATA, USDT_SIG)


def test_tampered_descriptor_fails_verification():
    forged = USDC_DATA[:-8] + (18).to_bytes(4, "big") + USDC_DATA[-4:]
    assert not verify_legacy_signature(forged, USDC_SIG)
    assert not verify_legacy_signature(USDC_DATA, USDT_SIG)
    assert not verify_legacy_signature(USDC_DATA, b"\x30\x02\x02\x00")


def test_parse_descriptor_fields():
    d = parse_descriptor(1, USDC.upper().replace("0X", "0x"), USDC_DATA, USDC_SIG)
    assert d is not None
    assert (d.ticker, d.decimals, d.address, d.chain_id) == ("USDC", 6, USDC, 1)
    assert d.apdu_payload() == b"\x04" + USDC_DATA + USDC_SIG
    assert d.verify()


@pytest.mark.parametrize("chain_id,address,data", [
    (10, USDC, USDC_DATA),                       # another chain's descriptor
    (1, USDT, USDC_DATA),                        # another token's descriptor
    (1, USDC, b"\xff" + USDC_DATA[4:]),           # non-ASCII ticker
    (1, USDC, USDC_DATA[4:]),                     # no ticker at all
])
def test_parse_descriptor_rejects(chain_id, address, data):
    assert parse_descriptor(chain_id, address, data, USDC_SIG) is None


@pytest.mark.parametrize("data,kind", [
    ("0xa9059cbb" + "00" * 64, "transfer"),
    ("0x095EA7B3" + "00" * 64, "approve"),
    ("0x23b872dd" + "00" * 96, None),            # transferFrom
    ("0x", None), (None, None),
])
def test_erc20_call_kind(data, kind):
    assert erc20_call_kind(data) == kind


# --- a scripted CAL ------------------------------------------------------------------


def _desc(chain_id: int, address: str, ticker: str = "TKN", decimals: int = 18):
    data = (ticker.encode() + bytes.fromhex(address[2:])
            + decimals.to_bytes(4, "big") + chain_id.to_bytes(4, "big"))
    return {"data": data.hex(), "descriptorType": "token",
            "signatures": {"prod": "3006020101020101", "test": "00"}}


class _Response:
    def __init__(self, status: int, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeCal:
    """Networks + paged tokens, ETag-revalidated like Ledger's CAL."""

    def __init__(self, etag: str = 'W/"c1"'):
        self.etag = etag
        self.requests: list[tuple[str, dict, dict]] = []
        self.networks = [
            {"id": "ethereum", "chain_id": 1, "family": "ethereum"},
            {"id": "base", "chain_id": 8453, "family": "ethereum"},
            {"id": "cardano", "chain_id": 1, "family": "cardano"},
            {"id": "acala", "family": "acala"},
        ]
        # network → list of pages (each a list of rows)
        self.pages = {
            "ethereum": [
                [{"contract_address": USDC, "descriptor": _desc(1, USDC, "USDC", 6)},
                 # test-signed only: never usable on a real device
                 {"contract_address": "0x" + "44" * 20, "descriptor": {
                     "data": _desc(1, "0x" + "44" * 20)["data"],
                     "signatures": {"test": "00"}}}],
                [{"contract_address": USDT, "descriptor": _desc(1, USDT, "USDT", 6)},
                 # a row whose bytes describe another chain
                 {"contract_address": "0x" + "55" * 20,
                  "descriptor": _desc(10, "0x" + "55" * 20)}],
            ],
            "base": [[{"contract_address": "0x" + "66" * 20,
                       "descriptor": _desc(8453, "0x" + "66" * 20, "BTKN")}]],
        }

    def get(self, url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        headers = dict(headers or {})
        self.requests.append((url.rsplit("/", 1)[-1], params, headers))
        assert "User-Agent" in headers
        if headers.get("If-None-Match") == self.etag:
            return _Response(304)
        if url.endswith("/networks"):
            return _Response(200, self.networks, {"ETag": self.etag})
        pages = self.pages[params["network"]]
        index = int(params.get("cursor", "0"))
        more = {"x-ledger-next": str(index + 1)} if index + 1 < len(pages) else {}
        return _Response(200, pages[index], {"ETag": self.etag, **more})


@pytest.fixture
def mirror(tmp_path):
    return TokenMirror(tmp_path)


def _files(mirror_root):
    return sorted(p.name for p in (mirror_root / "tokens").glob("*.json"))


def test_refresh_downloads_every_wanted_network(mirror, tmp_path):
    cal = FakeCal()
    assert mirror.refresh([1, 8453, 999], session=cal, now=1000.0) == 2
    assert _files(tmp_path) == ["1.json", "8453.json", "state.json"]
    tokens = json.loads((tmp_path / "tokens" / "1.json").read_text())["tokens"]
    # Only well-formed, prod-signed descriptors for THIS chain are kept.
    assert sorted(tokens) == [USDC, USDT]
    state = json.loads((tmp_path / "tokens" / "state.json").read_text())
    assert state["commit"] == 'W/"c1"' and state["chains"] == [1, 999, 8453]
    # The cursor walk followed x-ledger-next.
    token_reqs = [p for name, p, _ in cal.requests if name == "tokens"]
    assert [p.get("cursor") for p in token_reqs if p["network"] == "ethereum"] == [None, "1"]


def test_unchanged_dataset_costs_one_request(mirror):
    cal = FakeCal()
    mirror.refresh([1, 8453], session=cal, now=1000.0)
    cal.requests.clear()
    assert mirror.refresh([1, 8453], session=cal, now=2000.0) == 0
    assert [(name, h.get("If-None-Match")) for name, _, h in cal.requests] == [
        ("networks", 'W/"c1"')]
    assert mirror.last_checked() == 2000.0


def test_new_chain_fetches_only_that_network(mirror):
    cal = FakeCal()
    mirror.refresh([1], session=cal, now=1000.0)
    cal.requests.clear()
    assert mirror.refresh([1, 8453], session=cal, now=1100.0) == 1
    networks_req = cal.requests[0]
    assert networks_req[0] == "networks" and "If-None-Match" not in networks_req[2]
    assert {p["network"] for name, p, _ in cal.requests if name == "tokens"} == {"base"}


def test_new_dataset_commit_redownloads(mirror):
    cal = FakeCal()
    mirror.refresh([1], session=cal, now=1000.0)
    cal.etag = 'W/"c2"'
    cal.requests.clear()
    assert mirror.refresh([1], session=cal, now=1100.0) == 1


def test_failed_download_keeps_the_previous_mirror(mirror, tmp_path):
    cal = FakeCal()
    mirror.refresh([1], session=cal, now=1000.0)
    before = (tmp_path / "tokens" / "1.json").read_text()
    cal.etag = 'W/"c2"'

    def broken(url, params=None, headers=None, timeout=None):
        if url.endswith("/tokens"):
            return _Response(503)
        return FakeCal.get(cal, url, params, headers, timeout)
    with pytest.raises(RuntimeError):
        mirror.refresh([1], session=SimpleNamespace(get=broken), now=1100.0)
    assert (tmp_path / "tokens" / "1.json").read_text() == before
    # Not marked checked: the next scheduled check retries.
    assert mirror.last_checked() == 1000.0


def test_needs_refresh(mirror):
    assert mirror.needs_refresh([1], now=0.0)
    mirror.refresh([1], session=FakeCal(), now=1000.0)
    assert not mirror.needs_refresh([1], now=1000.0 + REFRESH_INTERVAL_S - 1)
    assert mirror.needs_refresh([1], now=1000.0 + REFRESH_INTERVAL_S)
    assert mirror.needs_refresh([1, 8453], now=1001.0)    # a chain was added


# --- lookup ------------------------------------------------------------------------


def _write_chain(root, chain_id, tokens):
    path = root / "tokens" / f"{chain_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": tokens}))
    return path


def test_lookup_returns_verified_descriptors(mirror, tmp_path):
    _write_chain(tmp_path, 1, {
        USDC: f"{USDC_DATA.hex()}:{USDC_SIG.hex()}",
        USDT: f"{USDT_DATA.hex()}:{USDC_SIG.hex()}",     # wrong signature
    })
    d = mirror.lookup(1, USDC.upper().replace("0X", "0x"))
    assert d is not None and d.ticker == "USDC"
    assert mirror.lookup(1, USDT) is None
    assert mirror.lookup(1, "0x" + "77" * 20) is None
    assert mirror.lookup(10, USDC) is None
    assert mirror.lookup(1, None) is None


def test_lookup_sees_a_refreshed_file(mirror, tmp_path):
    import os
    path = _write_chain(tmp_path, 1, {})
    assert mirror.lookup(1, USDC) is None
    _write_chain(tmp_path, 1, {USDC: f"{USDC_DATA.hex()}:{USDC_SIG.hex()}"})
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
    assert mirror.lookup(1, USDC) is not None


def test_lookup_survives_a_corrupt_file(mirror, tmp_path):
    path = tmp_path / "tokens" / "1.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert mirror.lookup(1, USDC) is None


def test_default_mirror_follows_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_metadata, "CACHE_DIR", tmp_path / "a")
    first = ledger_metadata.default_mirror()
    assert ledger_metadata.default_mirror() is first
    monkeypatch.setattr(ledger_metadata, "CACHE_DIR", tmp_path / "b")
    assert ledger_metadata.default_mirror() is not first


# --- scheduler ----------------------------------------------------------------------


@pytest.fixture
def scheduler_env(qapp, tmp_path, monkeypatch):
    from qeth.chains import DEFAULT_CHAINS
    monkeypatch.setattr(ledger_metadata, "CACHE_DIR", tmp_path / "ledger")
    store = SimpleNamespace(accounts=[], chains=list(DEFAULT_CHAINS))
    started: list = []
    return store, started


def _scheduler(store, started):
    from qeth.plugins.wallets.ledger_mirror import LedgerMirrorScheduler
    return LedgerMirrorScheduler(store, started.append)


def test_scheduler_idle_without_a_ledger_account(scheduler_env):
    store, started = scheduler_env
    store.accounts = [{"address": "0x" + "11" * 20, "source": "hot"}]
    _scheduler(store, started).check()
    assert started == []


def test_scheduler_refreshes_a_stale_mirror_for_every_evm_chain(scheduler_env):
    store, started = scheduler_env
    store.accounts = [{"address": "0x" + "11" * 20, "source": "ledger"}]
    sched = _scheduler(store, started)
    sched.check()
    [worker] = started
    assert worker._chain_ids == sorted(c.chain_id for c in store.chains if c.is_evm)
    sched.check()                       # one at a time
    assert len(started) == 1
    sched._on_finished()
    sched.check()
    assert len(started) == 2


def test_scheduler_leaves_a_fresh_mirror_alone(scheduler_env):
    store, started = scheduler_env
    store.accounts = [{"address": "0x" + "11" * 20, "source": "ledger"}]
    sched = _scheduler(store, started)
    ledger_metadata.default_mirror().refresh(sched.chain_ids(), session=FakeCal())
    sched.check()
    assert started == []


def test_scheduler_worker_reports_failure(scheduler_env, monkeypatch, qtbot):
    from qeth.plugins.wallets.ledger_mirror import LedgerMirrorWorker

    def boom(self, chain_ids, **kw):
        raise RuntimeError("offline")
    monkeypatch.setattr(TokenMirror, "refresh", boom)
    worker = LedgerMirrorWorker([1])
    with qtbot.waitSignal(worker.failed, timeout=5000) as blocker:
        worker.start()
    worker.wait(5000)
    assert blocker.args == ["offline"]
