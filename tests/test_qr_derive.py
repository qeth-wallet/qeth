"""BIP32 CKDpub (qeth/qr/derive.py). Pinned against private derivation — the
defining property is that the public child of a parent PUBLIC key equals the
public key of the private child. If CKDpub is wrong, that equality breaks."""

import hashlib
import hmac

import pytest
from eth_keys import keys
from eth_keys.backends.native.ecdsa import N

from qeth.qr import derive


def _priv_pub(priv: bytes) -> bytes:
    return keys.PrivateKey(priv).public_key.to_compressed_bytes()


def _ckd_priv_soft(parent_priv: bytes, chain_code: bytes, index: int):
    """Reference non-hardened CKDpriv (the oracle): child_priv = (IL +
    parent_priv) mod n, HMAC keyed by the parent PUBLIC key."""
    data = _priv_pub(parent_priv) + index.to_bytes(4, "big")
    digest = hmac.new(chain_code, data, hashlib.sha512).digest()
    il = int.from_bytes(digest[:32], "big")
    child = (il + int.from_bytes(parent_priv, "big")) % N
    return child.to_bytes(32, "big"), digest[32:]


PARENT_PRIV = hashlib.sha256(b"qeth-derive-parent").digest()
CHAIN_CODE = hashlib.sha256(b"qeth-derive-chaincode").digest()
PARENT_PUB = _priv_pub(PARENT_PRIV)


@pytest.mark.parametrize("index", [0, 1, 2, 5, 100, (1 << 31) - 1])
def test_ckd_pub_equals_public_key_of_ckd_priv(index):
    child_priv, cc_priv = _ckd_priv_soft(PARENT_PRIV, CHAIN_CODE, index)
    child_pub, cc_pub = derive.ckd_pub(PARENT_PUB, CHAIN_CODE, index)
    assert child_pub == _priv_pub(child_priv)      # points agree
    assert cc_pub == cc_priv                        # chain codes agree


def test_hardened_public_derivation_is_rejected():
    with pytest.raises(ValueError, match="hardened"):
        derive.ckd_pub(PARENT_PUB, CHAIN_CODE, 1 << 31)


def test_multilevel_derivation_matches_private_chain():
    # Walk m/0/5 publicly, and privately, and compare the leaf address.
    path = [0, 5]
    priv, cc = PARENT_PRIV, CHAIN_CODE
    for i in path:
        priv, cc = _ckd_priv_soft(priv, cc, i)
    private_addr = keys.PrivateKey(priv).public_key.to_checksum_address()
    public_addr = derive.derive_address(PARENT_PUB, CHAIN_CODE, path)
    assert public_addr == private_addr


def test_pubkey_to_address_matches_eth_keys():
    addr = derive.pubkey_to_address(PARENT_PUB)
    assert addr == keys.PrivateKey(PARENT_PRIV).public_key.to_checksum_address()
    assert addr.startswith("0x") and len(addr) == 42


def test_compress_decompress_roundtrip():
    x, y = derive._decompress(PARENT_PUB)
    assert derive._compress((x, y)) == PARENT_PUB
    with pytest.raises(ValueError):
        derive._decompress(b"\x04" + b"\x00" * 32)   # not a compressed point
