"""EIP-4527 eth-sign-request / eth-signature codec. The sign-request test
structurally pins the on-the-wire CBOR (keys, tags, plain-int data-type, keypath
components) to Keystone's reference, so a wire-format regression is caught even
without a live-device vector."""

import uuid

import pytest
from cbor2 import CBORTag, dumps, loads

from qeth.qr import eth, ur
from qeth.qr.multipart import decode_parts

RID = bytes.fromhex("00112233445566778899aabbccddeeff")
ADDR = bytes.fromhex("11" * 20)


def test_eth_sign_request_wire_structure():
    parts, rid = eth.encode_eth_sign_request(
        sign_data=b"\xde\xad\xbe\xef",
        data_type=eth.DataType.TYPED_TRANSACTION,
        chain_id=1,
        path="m/44'/60'/0'/0/0",
        source_fingerprint=0x12345678,
        address=ADDR,
        origin="qeth",
        request_id=RID,
    )
    assert rid == RID
    ur_type, payload = decode_parts(parts)
    assert ur_type == "eth-sign-request"
    body = loads(payload)
    # request-id: cbor2 decodes UUID tag 37 into a uuid.UUID — its existence
    # (and matching bytes) proves we emitted tag 37 over the 16 request-id bytes.
    assert isinstance(body[1], uuid.UUID) and body[1].bytes == RID
    assert body[2] == b"\xde\xad\xbe\xef"          # sign-data
    assert body[3] == 4                            # data-type: plain int, typed-tx
    assert body[4] == 1                            # chain-id
    keypath = body[5]                              # crypto-keypath, tag 304
    assert isinstance(keypath, CBORTag) and keypath.tag == 304
    # cbor2 may hand back the nested array as a tuple; the wire is a CBOR array.
    assert list(keypath.value[1]) == [44, True, 60, True, 0, True, 0, False, 0, False]
    assert keypath.value[2] == 0x12345678          # source-fingerprint (xfp)
    assert body[6] == ADDR
    assert body[7] == "qeth"


def test_optional_fields_are_omitted():
    parts, _ = eth.encode_eth_sign_request(
        sign_data=b"\x01", data_type=eth.DataType.PERSONAL_MESSAGE,
        chain_id=1, path="m/44'/60'/0'/0/0", source_fingerprint=1,
        request_id=RID)
    body = loads(decode_parts(parts)[1])
    assert 6 not in body and 7 not in body         # no address / origin


def test_derivation_path_parsing():
    assert eth.parse_derivation_path("m/44'/60'/0'/0/0") == [
        44, True, 60, True, 0, True, 0, False, 0, False]
    assert eth.parse_derivation_path("44h/60h/0h") == [44, True, 60, True, 0, True]


def test_parse_eth_signature_echoes_request_id():
    sig = b"\x11" * 65
    body = {1: CBORTag(37, RID), 2: sig, 3: "keystone"}
    ur_string = ur.encode("eth-signature", dumps(body, canonical=True))
    parsed = eth.parse_eth_signature(ur_string)
    assert parsed.request_id == RID
    assert parsed.signature == sig
    assert len(parsed.signature) == 65


def test_parse_eth_signature_rejects_wrong_ur_type():
    wrong = ur.encode("eth-sign-request", b"\x00")
    with pytest.raises(ValueError, match="eth-signature"):
        eth.parse_eth_signature(wrong)


def test_request_id_defaults_to_a_fresh_uuid():
    parts, rid = eth.encode_eth_sign_request(
        sign_data=b"\x00", data_type=eth.DataType.TRANSACTION, chain_id=1,
        path="m/44'/60'/0'/0/0", source_fingerprint=1)
    assert len(rid) == 16
    assert loads(decode_parts(parts)[1])[1].bytes == rid   # echoed into the body
