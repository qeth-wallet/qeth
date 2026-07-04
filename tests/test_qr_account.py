"""crypto-hdkey account-export parsing (qeth/qr/account.py), pinned to the
Blockchain Commons BCR-2020-007 hdkey test vector (which uses the current
40303/40304 tags)."""

import pytest
from cbor2 import CBORTag, dumps

from qeth.qr import account, ur

# BCR-2020-007 Test Vector 2 — a derived public key at m/44'/1'/1'/0/1.
VEC_UR = (
    "ur:hdkey/onaxhdclaojlvoechgferkdpqdiabdrflawshlhdmdcemtfnlrctghchbdolvwse"
    "dnvdztbgolaahdcxtottgostdkhfdahdlykkecbbweskrymwflvdylgerkloswtbrpfdbstic"
    "mwylklpahtantjsoyaoadamtantjooyadlecsdwykadykadykaewkadwkaycywlcscewfjnkp"
    "vllt"
)
VEC_KEYDATA = bytes.fromhex(
    "026fe2355745bb2db3630bbc80ef5d58951c963c841f54170ba6e5c12be7fc12a6")
VEC_CHAINCODE = bytes.fromhex(
    "ced155c72456255881793514edc5bd9447e7f74abb88c6d6b6480fd016ee8c85")


def test_parses_the_bc_hdkey_vector():
    key = account.parse_account_export(VEC_UR)
    assert key.pubkey == VEC_KEYDATA
    assert key.chain_code == VEC_CHAINCODE
    assert list(key.origin_path) == [44, True, 1, True, 1, True, 0, False, 1, False]
    assert key.parent_fingerprint == 0xE9181CF3


def _hdkey_ur(*, tag, keypath_tag, source_fp=0x12345678):
    """Hand-build a crypto-hdkey UR to exercise the legacy vs new tag numbers."""
    keypath = CBORTag(keypath_tag, {1: [44, True, 60, True, 0, True], 2: source_fp})
    body = {3: b"\x02" + b"\x11" * 32, 4: b"\x22" * 32, 6: keypath, 8: 0xABCDEF01}
    payload = dumps(CBORTag(tag, body) if tag else body, canonical=True)
    return ur.encode("crypto-hdkey", payload)


@pytest.mark.parametrize("tag,keypath_tag", [
    (None, 304),      # bare top-level map, legacy keypath tag (Keystone-style)
    (303, 304),       # legacy hdkey + keypath tags
    (40303, 40304),   # current IANA-range tags
])
def test_accepts_both_tag_generations(tag, keypath_tag):
    key = account.parse_account_export(_hdkey_ur(tag=tag, keypath_tag=keypath_tag))
    assert list(key.origin_path) == [44, True, 60, True, 0, True]
    assert key.source_fingerprint == 0x12345678
    assert key.master_fingerprint() == 0x12345678       # source fp wins


def test_master_fingerprint_falls_back_to_parent():
    key = account.parse_account_export(_hdkey_ur(tag=None, keypath_tag=304, source_fp=0))
    assert key.source_fingerprint == 0
    assert key.master_fingerprint() == 0xABCDEF01        # parent fp fallback


def test_rejects_non_hdkey_ur():
    with pytest.raises(ValueError, match="crypto-hdkey"):
        account.parse_account_export(ur.encode("eth-signature", b"\x00"))


def test_derives_addresses_below_the_exported_key():
    from qeth.qr import derive
    key = account.parse_account_export(_hdkey_ur(tag=None, keypath_tag=304))
    # The exported node is an account-level xpub; derive change/index locally.
    a0 = derive.derive_address(key.pubkey, key.chain_code, [0, 0])
    a1 = derive.derive_address(key.pubkey, key.chain_code, [0, 1])
    assert a0.startswith("0x") and len(a0) == 42 and a0 != a1
