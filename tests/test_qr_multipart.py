"""Multi-part (animated) UR encoding (qeth/qr/multipart.py). Pinned to the
BCR-2024-001 part-CBOR test vector, plus encode→decode round-trips."""

import math
import zlib

from cbor2 import dumps, loads

from qeth.qr.multipart import _split_part, decode_parts, encode_parts


def test_part_cbor_matches_the_spec_vector():
    # BCR-2024-001 worked example, part seqNum=1 of the 256-byte "Wolf" message,
    # maxFragmentLen 30 → seqLen 9, checksum 0x0167aa07, 29-byte fragment.
    fragment = bytes.fromhex(
        "916ec65cf77cadf55cd7f9cda1a1030026ddd42e905b77adc36e4f2d3c")
    part = dumps([1, 9, 256, 0x0167AA07, fragment], canonical=True)
    assert part.hex() == "8501091901001a0167aa07581d" + fragment.hex()


def test_fragmentation_sizing_matches_the_spec():
    # 256 bytes @ maxFragmentLen 30 → 9 fragments (the spec vector's seqLen).
    parts = encode_parts("t", bytes(256), single_part_max=0, fragment_len=30)
    assert len(parts) == 9


def test_small_payload_is_a_single_plain_part():
    parts = encode_parts("eth-sign-request", bytes(range(120)))
    assert len(parts) == 1
    assert parts[0].startswith("ur:eth-sign-request/")
    assert parts[0].count("/") == 1               # no seqNum-seqLen segment


def test_large_payload_animates_and_roundtrips():
    msg = bytes((i * 7 + 3) % 256 for i in range(400))
    parts = encode_parts("eth-sign-request", msg, single_part_max=0, fragment_len=150)
    assert len(parts) == math.ceil(400 / 150) == 3
    for i, p in enumerate(parts):
        assert p.startswith(f"ur:eth-sign-request/{i + 1}-3/")   # 1-3, 2-3, 3-3
    ur_type, out = decode_parts(parts)
    assert ur_type == "eth-sign-request" and out == msg


def test_roundtrip_across_sizes_including_padding():
    for size in (1, 149, 150, 151, 299, 1000):
        msg = bytes((i * 13 + 1) % 256 for i in range(size))
        assert decode_parts(encode_parts("x", msg, fragment_len=150)) == ("x", msg)


def test_encoded_part_carries_the_right_fields():
    msg = bytes(range(200))
    parts = encode_parts("x", msg, fragment_len=150)
    _type, seq_num, seq_len, cbor = _split_part(parts[0])
    assert (seq_num, seq_len) == (1, 2)
    arr = loads(cbor)
    assert arr[0] == 1 and arr[1] == 2 and arr[2] == 200
    assert arr[3] == (zlib.crc32(msg) & 0xFFFFFFFF)


def test_decode_rejects_incomplete():
    parts = encode_parts("x", bytes(range(200)), single_part_max=0, fragment_len=150)
    import pytest
    with pytest.raises(ValueError, match="incomplete"):
        decode_parts(parts[:1])   # only 1 of 2 parts
