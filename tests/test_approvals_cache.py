"""ApprovalsCache — persisted allowances + last block (round-trip, robustness)."""

from decimal import Decimal

from qeth.plugins.approvals.cache import ApprovalsCache
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
