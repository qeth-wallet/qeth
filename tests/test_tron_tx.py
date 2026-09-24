"""Tron transaction encoding against transactions a live node built
(``/wallet/createtransaction`` and ``/wallet/triggersmartcontract`` on
mainnet, 2026-09-24): our local encoding must reproduce the node's bytes and
txID exactly, or every signature would be over the wrong message."""

import pytest

from qeth.address import tron_to_hex
from qeth.tron.tx import (
    TransferContract, TriggerSmartContract, TronTx, decode_raw, read_fields,
    recover_signer, ref_block, signature_v27, signed_transaction, txid,
)

OWNER = tron_to_hex("TV6MuMXfmLbBqPZvBHdwFsDnQeVfnmiuSi").lower()
USDT = tron_to_hex("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t").lower()

TRANSFER_RAW = bytes.fromhex(
    "0a0265b62208ec0e44ee890909154098c0e1a48d345a67080112630a2d747970652e676f"
    "6f676c65617069732e636f6d2f70726f746f636f6c2e5472616e73666572436f6e747261"
    "637412320a1541d1c4bb7b2f39aba5707711719b2236b5b605af2e121541a614f803b6fd"
    "780986a42c78ec9c7f77e6ded13c1887ad4b70fff0dda48d34")
TRANSFER_TXID = "48385d0d06ea513ae453ad3018c4c2ea312589b4bb394627cd8ae7566f489685"
TRIGGER_RAW = bytes.fromhex(
    "0a0265b62208ec0e44ee890909154098c0e1a48d345aae01081f12a9010a31747970652e"
    "676f6f676c65617069732e636f6d2f70726f746f636f6c2e54726967676572536d617274"
    "436f6e747261637412740a1541d1c4bb7b2f39aba5707711719b2236b5b605af2e121541"
    "a614f803b6fd780986a42c78ec9c7f77e6ded13c2244a9059cbb00000000000000000000"
    "0000d1c4bb7b2f39aba5707711719b2236b5b605af2e0000000000000000000000000000"
    "0000000000000000000000000000000f42407089f2dda48d3490018087a70e")
TRIGGER_TXID = "0d71ab37cc07f5632292b37f90ce4371a2893358abd6ed113a72004278ef8b18"

REF_BYTES = bytes.fromhex("65b6")
REF_HASH = bytes.fromhex("ec0e44ee89090915")


def test_transfer_built_locally_matches_the_node():
    tx = TronTx(TransferContract(OWNER, USDT, 1234567), REF_BYTES, REF_HASH,
                expiration=1790273151000, timestamp=1790273091711)
    assert tx.raw_data() == TRANSFER_RAW
    assert tx.txid().hex() == TRANSFER_TXID


def test_trigger_built_locally_matches_the_node():
    data = bytes.fromhex("a9059cbb" + "00" * 12 + OWNER[2:]) + (10**6).to_bytes(32, "big")
    tx = TronTx(TriggerSmartContract(OWNER, USDT, data), REF_BYTES, REF_HASH,
                expiration=1790273151000, timestamp=1790273091849,
                fee_limit=30_000_000)
    assert tx.raw_data() == TRIGGER_RAW
    assert tx.txid().hex() == TRIGGER_TXID


@pytest.mark.parametrize("raw", [TRANSFER_RAW, TRIGGER_RAW])
def test_decode_round_trips(raw):
    tx = decode_raw(raw)
    assert tx.raw_data() == raw
    assert tx.owner == OWNER


def test_decode_refuses_what_it_cannot_show_faithfully():
    tx = decode_raw(TRANSFER_RAW)
    two = tx.raw_data() + read_fields_contract(tx)   # a second contract
    with pytest.raises(ValueError, match="exactly one contract"):
        decode_raw(two)
    with pytest.raises(ValueError, match="unsupported raw_data fields"):
        decode_raw(TRANSFER_RAW + bytes([0x4a, 0x00]))   # field 9: auths


def read_fields_contract(tx):
    """The raw bytes of the tx's contract field (field 11) — re-emitted."""
    for n, _, v in read_fields(tx.raw_data()):
        if n == 11:
            return bytes([0x5a]) + bytes([len(v)]) + v
    raise AssertionError


def test_zero_scalars_are_omitted():
    tx = TronTx(TransferContract(OWNER, USDT, 1), REF_BYTES, REF_HASH,
                expiration=1, timestamp=0)
    assert 14 not in {n for n, _, _ in read_fields(tx.raw_data())}
    assert 18 not in {n for n, _, _ in read_fields(tx.raw_data())}


def test_ref_block_takes_the_tapos_slices():
    bid = "00000000052865a657be05bc4f1ceb00bda7bbd12a2b02ab4a1221ada11df470"
    assert ref_block(86533542, bid) == (bytes.fromhex("65a6"),
                                        bytes.fromhex("57be05bc4f1ceb00"))
    with pytest.raises(ValueError):
        ref_block(1, "00")


def test_sign_recover_and_serialize():
    from eth_keys import keys
    priv = keys.PrivateKey(b"\x01" * 32)
    tid = txid(TRANSFER_RAW)
    sig = signature_v27(priv.sign_msg_hash(tid).to_bytes())
    assert sig[64] in (27, 28)
    assert recover_signer(tid, sig) == "0x" + priv.public_key.to_canonical_address().hex()
    signed = signed_transaction(TRANSFER_RAW, [sig])
    assert read_fields(signed) == [(1, 2, TRANSFER_RAW), (2, 2, sig)]
