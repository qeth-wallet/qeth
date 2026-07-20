"""ApprovalsCache — persisted allowances + last block (round-trip, robustness)."""

import json
from decimal import Decimal

from qeth.plugins.approvals.cache import _SCHEMA_VERSION, ApprovalsCache
from qeth.plugins.approvals.discovery import ApprovalRow

A = "0x" + "a1" * 20
T1 = "0x" + "11" * 20
T2 = "0x" + "22" * 20
SP = "0x" + "ee" * 20
_MAX = (1 << 256) - 1


def test_roundtrip_rows_and_last_block(tmp_path):
    c = ApprovalsCache(tmp_path)
    rows = [
        ApprovalRow(token=T1, spender=SP, allowance=_MAX, symbol="ZRX",
                    decimals=18, spender_label="Uniswap: Router", price_usd=None),
        ApprovalRow(token=T2, spender=SP, allowance=5_000_000, symbol="USDC",
                    name="USD Coin", decimals=6, price_usd=Decimal("1.5")),
    ]
    c.save(1, A, rows, 12345)
    loaded = c.load(1, A)
    assert loaded is not None
    got, last_block = loaded
    assert last_block == 12345
    assert [r.spender for r in got] == [SP, SP]
    assert got[0].allowance == _MAX and got[0].price_usd is None
    assert got[0].spender_label == "Uniswap: Router"
    assert got[1].price_usd == Decimal("1.5") and got[1].decimals == 6


def test_token_balance_round_trips(tmp_path):
    c = ApprovalsCache(tmp_path)
    c.save(1, A, [ApprovalRow(token=T1, spender=SP, allowance=1,
                              token_balance=123_456_789)], 5)
    got, _ = c.load(1, A)
    assert got[0].token_balance == 123_456_789         # for the at-risk tag on re-open


def test_uint256_max_allowance_survives_json(tmp_path):
    c = ApprovalsCache(tmp_path)
    c.save(1, A, [ApprovalRow(token=T1, spender=SP, allowance=_MAX)], 0)
    got, _ = c.load(1, A)
    assert got[0].allowance == _MAX               # stored as string, not a JSON float


def test_missing_returns_none(tmp_path):
    assert ApprovalsCache(tmp_path).load(1, A) is None


def test_corrupt_file_returns_none(tmp_path):
    c = ApprovalsCache(tmp_path)
    p = c._path(1, A)
    p.parent.mkdir(parents=True)
    p.write_text("{ not valid json")
    assert c.load(1, A) is None


def test_address_is_case_insensitive(tmp_path):
    c = ApprovalsCache(tmp_path)
    c.save(1, A.upper(), [ApprovalRow(token=T1, spender=SP, allowance=1)], 7)
    loaded = c.load(1, A.lower())
    assert loaded is not None and loaded[1] == 7


def test_save_stamps_the_schema_version(tmp_path):
    c = ApprovalsCache(tmp_path)
    c.save(1, A, [ApprovalRow(token=T1, spender=SP, allowance=1)], 7)
    data = json.loads(c._path(1, A).read_text())
    assert data["v"] == _SCHEMA_VERSION


def test_v1_unversioned_cache_is_rejected_as_a_miss(tmp_path):
    # A pre-event-log cache (no "v", last_block = old tx-head meaning) must read
    # as a MISS so the plugin cold-scans from block 0 instead of trusting a
    # poisoned incremental cursor — the "no rescan on 0x7a" bug.
    c = ApprovalsCache(tmp_path)
    p = c._path(1, A)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "last_block": 25574518,       # a recent block, old semantics
        "rows": [{"token": T1, "spender": SP, "allowance": "1",
                  "symbol": "X", "decimals": 18}],
    }))
    assert c.load(1, A) is None       # rejected → cold scan


def test_wrong_version_cache_is_rejected(tmp_path):
    c = ApprovalsCache(tmp_path)
    p = c._path(1, A)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"v": _SCHEMA_VERSION + 99, "last_block": 1, "rows": []}))
    assert c.load(1, A) is None
