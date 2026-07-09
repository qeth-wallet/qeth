"""Multi-part (animated) UR encoding (qeth/qr/multipart.py). Pinned to the
BCR-2024-001 part-CBOR test vector, plus encode→decode round-trips."""

import math
import zlib

from cbor2 import dumps, loads

from qeth.qr.multipart import (
    MAX_FRAGMENTS,
    _fragment_len_for,
    _plan,
    _split_part,
    decode_parts,
    encode_parts,
    frame_source,
)


def test_part_cbor_matches_the_spec_vector():
    # BCR-2024-001 worked example, part seqNum=1 of the 256-byte "Wolf" message,
    # maxFragmentLen 30 → seqLen 9, checksum 0x0167aa07, 29-byte fragment.
    fragment = bytes.fromhex(
        "916ec65cf77cadf55cd7f9cda1a1030026ddd42e905b77adc36e4f2d3c")
    part = dumps([1, 9, 256, 0x0167AA07, fragment], canonical=True)
    assert part.hex() == "8501091901001a0167aa07581d" + fragment.hex()


def test_fragmentation_sizing_matches_the_spec():
    # 256 bytes @ fragment_len 30 → seqLen 9 (the spec vector's seqLen); with
    # the rateless fountain parts the stream is 2×seqLen = 18.
    parts = encode_parts("t", bytes(256), single_part_max=0, fragment_len=30)
    assert len(parts) == 18
    assert all(_split_part(p)[2] == 9 for p in parts)   # seqLen 9 in every part


def test_small_payload_is_a_single_plain_part():
    parts = encode_parts("eth-sign-request", bytes(range(120)))
    assert len(parts) == 1
    assert parts[0].startswith("ur:eth-sign-request/")
    assert parts[0].count("/") == 1               # no seqNum-seqLen segment


def test_large_payload_animates_with_pure_plus_rateless():
    msg = bytes((i * 7 + 3) % 256 for i in range(400))
    parts = encode_parts("eth-sign-request", msg, single_part_max=0, fragment_len=150)
    seq_len = math.ceil(400 / 150)                 # 3
    assert len(parts) == 2 * seq_len               # 3 pure + 3 rateless
    assert [_split_part(p)[1] for p in parts] == list(range(1, 2 * seq_len + 1))
    assert all(_split_part(p)[2] == seq_len for p in parts)
    # the pure parts (rateless are skipped by decode_parts) reconstruct the msg
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


def test_frame_source_small_payload_is_a_constant_single_part():
    nf = frame_source("x", bytes(range(120)))
    assert nf() == nf()                           # same static QR every tick
    assert nf().count("/") == 1                   # no seqNum-seqLen segment


def test_frame_source_reinjects_pure_fragments_every_cycle():
    # A large payload streams pure fragments (seqNum 1..seqLen), then a batch of
    # rateless parts (seqNum > seqLen), then the pure fragments AGAIN — so a
    # device that joined late can still catch a pure fragment.
    msg = bytes((i * 7 + 3) % 256 for i in range(2000))
    nf = frame_source("eth-sign-request", msg, fragment_len=150)
    seq_len = _plan(msg, 150)[0]
    frames = [nf() for _ in range(3 * seq_len)]
    seqnums = [_split_part(f)[1] for f in frames]
    assert seqnums[:seq_len] == list(range(1, seq_len + 1))          # pure block first
    assert all(s > seq_len for s in seqnums[seq_len:2 * seq_len])    # then rateless
    assert seqnums[2 * seq_len:] == list(range(1, seq_len + 1))      # pures re-injected
    # a pure block reconstructs the message on its own
    assert decode_parts(frames[:seq_len]) == ("eth-sign-request", msg)


def test_fragment_len_caps_parts_for_a_huge_payload():
    # FRAGMENT_LEN (v12) for a normal-sized payload...
    assert _fragment_len_for(10_000) == 220
    # ...but a payload too big for 128 parts at 220 packs denser to stay under the
    # cap that a Keycard-class receiver enforces.
    big = 128 * 220 + 5_000
    fl = _fragment_len_for(big)
    assert fl > 220
    assert _plan(bytes(big), fl)[0] <= MAX_FRAGMENTS

    # frame_source uses the capped fragment length by default
    nf = frame_source("eth-sign-request", bytes(big))
    assert all(_split_part(nf())[2] <= MAX_FRAGMENTS for _ in range(10))
