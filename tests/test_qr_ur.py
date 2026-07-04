"""Single-part UR (ur:<type>/<bytewords>) encode/decode."""

import pytest

from qeth.qr import ur


def test_roundtrip_and_prefix():
    payload = bytes(range(40))
    s = ur.encode("eth-signature", payload)
    assert s.startswith("ur:eth-signature/")
    ur_type, out = ur.decode(s)
    assert ur_type == "eth-signature"
    assert out == payload


def test_decode_is_case_insensitive():
    s = ur.encode("eth-signature", b"\x01\x02\x03")
    ur_type, out = ur.decode(s.upper())
    assert ur_type == "eth-signature" and out == b"\x01\x02\x03"


def test_multipart_ur_is_rejected_clearly():
    with pytest.raises(ValueError, match="multi-part"):
        ur.decode("ur:eth-sign-request/1-3/aeadcylabntfgm")


def test_not_a_ur():
    with pytest.raises(ValueError, match="ur:"):
        ur.decode("just-some-text")


def test_invalid_type_is_rejected():
    with pytest.raises(ValueError, match="invalid UR type"):
        ur.encode("Eth_Sign_Request", b"\x00")   # uppercase + underscore
