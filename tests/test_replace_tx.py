"""Speed-up / cancel a pending tx: fee decoding + replacement request."""

from __future__ import annotations

from eth_account import Account

from qeth.signing import (
    _bumped,
    build_replacement_request,
    decode_tx_fees,
)

ACCT = Account.from_key("0x" + "11" * 32)
ME = ACCT.address
TO = "0x" + "22" * 20


def _sign(tx) -> str:
    raw = ACCT.sign_transaction(tx).raw_transaction.hex()
    return raw if raw.startswith("0x") else "0x" + raw


def _eip1559(**over):
    tx = {"to": TO, "value": 1234, "data": "0x", "nonce": 9, "gas": 60000,
          "maxFeePerGas": 50_000_000_000, "maxPriorityFeePerGas": 2_000_000_000,
          "chainId": 1, "type": 2}
    tx.update(over)
    return _sign(tx)


# --- fee decoding --------------------------------------------------------

def test_decode_eip1559():
    f = decode_tx_fees(_eip1559(data="0x"))
    assert f.max_fee_per_gas == 50_000_000_000
    assert f.max_priority_fee_per_gas == 2_000_000_000
    assert f.gas == 60000 and f.nonce == 9 and f.gas_price is None


def test_decode_legacy():
    raw = _sign({"to": TO, "value": 5, "data": "0x", "nonce": 3, "gas": 21000,
                 "gasPrice": 40_000_000_000, "chainId": 1})
    f = decode_tx_fees(raw)
    assert f.gas_price == 40_000_000_000 and f.nonce == 3
    assert f.max_fee_per_gas is None and f.max_priority_fee_per_gas is None


# --- bump arithmetic -----------------------------------------------------

def test_bump_is_strictly_above_10pct():
    assert _bumped(None) is None
    assert _bumped(100) == 113                    # ceil(100 × 9/8)
    for v in (1, 7, 1000, 50_000_000_000):
        assert _bumped(v) > v * 11 // 10          # always clears geth's +10%


# --- replacement request -------------------------------------------------

def test_speedup_keeps_fields_and_nonce_and_bumps_fees():
    req, floor = build_replacement_request(
        from_addr=ME, to_addr=TO, value_wei=1234, data="0xabcdef",
        nonce=9, raw_signed=_eip1559(data="0xabcdef"), chain_id=1)
    # identical tx, same nonce
    assert req.nonce == 9 and req.to_addr == TO
    assert req.value_wei == 1234 and req.data == "0xabcdef" and req.gas == 60000
    # fees floored at old × 1.125
    assert req.max_fee_per_gas == _bumped(50_000_000_000)
    assert req.max_priority_fee_per_gas == _bumped(2_000_000_000)
    assert floor.max_fee_per_gas == _bumped(50_000_000_000)
    assert floor.max_priority_fee_per_gas == _bumped(2_000_000_000)


def test_cancel_is_zero_value_self_send_same_nonce():
    req, _ = build_replacement_request(
        from_addr=ME, to_addr=TO, value_wei=1234, data="0xabcdef",
        nonce=9, raw_signed=_eip1559(data="0xabcdef"), chain_id=1, cancel=True)
    assert req.to_addr == ME and req.value_wei == 0
    assert req.data == "0x" and req.gas == 21000
    assert req.nonce == 9                          # still replaces the same slot


def test_legacy_replacement_bumps_gas_price():
    raw = _sign({"to": TO, "value": 5, "data": "0x", "nonce": 3, "gas": 21000,
                 "gasPrice": 40_000_000_000, "chainId": 1})
    req, floor = build_replacement_request(
        from_addr=ME, to_addr=TO, value_wei=5, data="0x",
        nonce=3, raw_signed=raw, chain_id=1)
    assert req.gas_price == _bumped(40_000_000_000)
    assert req.max_fee_per_gas is None
    assert floor.gas_price == _bumped(40_000_000_000)
