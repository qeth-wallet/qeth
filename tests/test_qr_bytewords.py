"""Bytewords codec, pinned against the Blockchain Commons BCR-2020-012 test
vector (so a wrong word in the 256-entry table can't slip through)."""

import pytest

from qeth.qr import bytewords

# BCR-2020-012 worked example: a CBOR seed body, and its standard + minimal
# Bytewords encodings (which include the appended CRC32 c904f40b).
VEC_BODY = bytes.fromhex("d99d6ca20150c7098580125e2ab0981253468b2dbc5202c11947da")
VEC_STANDARD = (
    "tuna next jazz oboe acid good slot axis limp lava brag holy door puff "
    "monk brag guru frog luau drop roof grim also safe chef fuel twin solo "
    "aqua work bald"
)
VEC_MINIMAL = "tantjzoeadgdstaslplabghydrpfmkbggufgludprfgmaosecffltnsoaawkbd"


def test_encode_matches_the_spec_vector():
    assert bytewords.encode(VEC_BODY, minimal=False) == VEC_STANDARD
    assert bytewords.encode(VEC_BODY, minimal=True) == VEC_MINIMAL


def test_decode_matches_the_spec_vector():
    assert bytewords.decode(VEC_STANDARD, minimal=False) == VEC_BODY
    assert bytewords.decode(VEC_MINIMAL, minimal=True) == VEC_BODY


def test_roundtrip_including_empty_and_binary():
    for payload in (b"", b"\x00", bytes(range(256)), b"qeth air-gap"):
        assert bytewords.decode(bytewords.encode(payload)) == payload
        assert bytewords.decode(
            bytewords.encode(payload, minimal=False), minimal=False) == payload


def test_crc_mismatch_is_rejected():
    good = bytewords.encode(b"\x01\x02\x03")
    corrupt = ("ba" if good[:2] != "ba" else "ka") + good[2:]  # flip 1st byte
    with pytest.raises(ValueError, match="CRC32"):
        bytewords.decode(corrupt)


def test_unknown_word_is_rejected():
    with pytest.raises(ValueError, match="unknown byteword"):
        bytewords.decode("zzzz", minimal=False)


def test_table_is_256_unique_words_and_codes():
    assert len(bytewords._WORDS) == 256
    assert len(set(bytewords._WORDS)) == 256
    assert len(set(bytewords._MINIMAL)) == 256   # minimal codes must be unique too
