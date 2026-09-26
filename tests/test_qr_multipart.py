"""Multi-part (animated) UR encoding (qeth/qr/multipart.py). Pinned to the
BCR-2024-001 part-CBOR test vector, plus encode→decode round-trips."""

import math
import zlib

import pytest
import segno
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
        "916ec65cf77cadf55cd7f9cda1a1030026ddd42e905b77adc36e4f2d3c"
    )
    part = dumps([1, 9, 256, 0x0167AA07, fragment], canonical=True)
    assert part.hex() == "8501091901001a0167aa07581d" + fragment.hex()


def test_fragmentation_sizing_matches_the_spec():
    # 256 bytes @ fragment_len 30 → seqLen 9 (the spec vector's seqLen); with
    # the rateless fountain parts the stream is 2×seqLen = 18.
    parts = encode_parts("t", bytes(256), single_part_max=0, fragment_len=30)
    assert len(parts) == 18
    assert all(_split_part(p)[2] == 9 for p in parts)  # seqLen 9 in every part


def test_small_payload_is_a_single_plain_part():
    parts = encode_parts("eth-sign-request", bytes(range(120)))
    assert len(parts) == 1
    assert parts[0].startswith("ur:eth-sign-request/")
    assert parts[0].count("/") == 1  # no seqNum-seqLen segment


def test_large_payload_animates_with_pure_plus_rateless():
    msg = bytes((i * 7 + 3) % 256 for i in range(400))
    parts = encode_parts("eth-sign-request", msg, single_part_max=0, fragment_len=150)
    seq_len = math.ceil(400 / 150)  # 3
    assert len(parts) == 2 * seq_len  # 3 pure + 3 rateless
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
        decode_parts(parts[:1])  # only 1 of 2 parts


def test_frame_source_small_payload_is_a_constant_single_part():
    nf = frame_source("x", bytes(range(120)))
    assert nf() == nf()  # same static QR every tick
    assert nf().count("/") == 1  # no seqNum-seqLen segment


@pytest.mark.parametrize(
    "size,count", [(400, 4), (7680, 64), (7800, 65), (44_345, 128)]
)
def test_shuffled_retry_groups_preserve_coverage_and_standard_recovery(size, count):
    import random
    from qeth.qr.fountain import choose_fragments, mix
    from qeth.qr.multipart import RETRY_SEQUENCE_START

    message = random.Random(13).randbytes(size)
    source = frame_source("eth-sign-request", message, rng=random.Random(71))
    first_pass = [source() for _ in range(count)]
    assert decode_parts(first_pass) == ("eth-sign-request", message)
    _, fragments, checksum = _plan(message, _fragment_len_for(size))
    plain = 0
    positions = set()
    aliases = 0
    for group in range(count * 3):
        recovery_positions = []
        plain_indexes = []
        for position in range(3):
            _, sequence, total, cbor = _split_part(source())
            fields = loads(cbor)
            assert fields[:4] == [sequence, count, size, checksum]
            indexes = choose_fragments(sequence, total, checksum)
            assert fields[4] == mix(fragments, indexes)
            if count < sequence < RETRY_SEQUENCE_START:
                assert sequence == count + group + 1
                recovery_positions.append(position)
            else:
                assert len(indexes) == 1
                plain_indexes.append(next(iter(indexes)))
                plain += 1
                aliases += sequence >= RETRY_SEQUENCE_START
        assert sorted(plain_indexes) == sorted(
            [(2 * group) % count, (2 * group + 1) % count]
        )
        assert len(recovery_positions) == 1
        positions.add(recovery_positions[0])
    assert plain == count * 6
    assert positions == {0, 1, 2}
    assert aliases > count


def test_retry_search_is_incremental_bounded_and_falls_back(monkeypatch):
    from qeth.qr import multipart

    calls = []

    def no_singletons(sequence, count, checksum):
        calls.append(sequence)
        return {0, 1}

    monkeypatch.setattr(multipart.fountain, "choose_fragments", no_singletons)
    retries = multipart._PlainRetries(128, 123)
    assert retries.sequence(42) == 43
    assert len(calls) == multipart.RETRY_SEARCH_BATCH
    for _ in range(1000):
        assert retries.sequence(42) == 43
    assert len(calls) == 16_384
    assert len(set(calls)) == 16_384


def test_fragment_len_caps_parts_for_a_huge_payload():
    # Readable default for a normal-sized payload...
    assert _fragment_len_for(10_000) == 120
    # ...but a payload too big for 128 parts at 120 packs denser to stay under the
    # cap that a Keycard-class receiver enforces.
    big = 128 * 120 + 5_000
    fl = _fragment_len_for(big)
    assert fl > 120
    assert _plan(bytes(big), fl)[0] <= MAX_FRAGMENTS

    # frame_source uses the capped fragment length by default
    nf = frame_source("eth-sign-request", bytes(big))
    assert all(_split_part(nf())[2] <= MAX_FRAGMENTS for _ in range(10))


@pytest.mark.parametrize("size,parts", [(149, 1), (150, 1), (151, 2)])
def test_default_static_boundary_roundtrips(size, parts):
    message = bytes((i * 13 + 1) % 256 for i in range(size))
    nf = frame_source("eth-sign-request", message)
    frames = [nf() for _ in range(parts)]
    assert (_split_part(frames[0])[2] or 1) == parts
    assert decode_parts(frames) == ("eth-sign-request", message)
    if parts == 1:
        assert nf() == frames[0]


@pytest.mark.parametrize(
    "size,fragment_size,part_count",
    [(15_359, 120, 128), (15_360, 120, 128), (15_361, 121, 127), (30_000, 235, 128)],
)
def test_default_fragment_cap_boundary_roundtrips(size, fragment_size, part_count):
    message = bytes((i * 13 + 1) % 256 for i in range(size))
    assert _fragment_len_for(size) == fragment_size
    nf = frame_source("eth-sign-request", message)
    frames = [nf() for _ in range(part_count)]
    assert all(_split_part(frame)[2] == part_count for frame in frames)
    assert decode_parts(frames) == ("eth-sign-request", message)


def test_default_frames_are_less_dense_without_losing_payload():
    message = bytes((i * 13 + 1) % 256 for i in range(10_000))
    nf = frame_source("eth-sign-request", message)
    frames = [nf() for _ in range(84)]
    assert all(_split_part(frame)[2] == 84 for frame in frames)
    assert decode_parts(frames) == ("eth-sign-request", message)
    old_frame = frame_source("eth-sign-request", message, fragment_len=220)()
    assert segno.make_qr(old_frame.upper(), error="l").symbol_size() == (73, 73)
    assert all(
        segno.make_qr(frame.upper(), error="l").symbol_size() == (61, 61)
        for frame in (frames[0], frames[-1])
    )


@pytest.mark.parametrize("size", [1000, 44_345])
@pytest.mark.parametrize("loss", ["periodic", "random", "bursts", "late"])
def test_exact_reconstruction_under_losses(size, loss):
    import random
    from qeth.qr.fountain import choose_fragments

    message = random.Random(912).randbytes(size)
    source = frame_source("eth-sign-request", message, rng=random.Random(84))
    rng = random.Random(117)
    known = {}
    pending = []
    count = math.ceil(size / _fragment_len_for(size))
    for tick in range(2500):
        frame = source()
        if (
            loss == "periodic"
            and tick % 3 != 0
            or loss == "random"
            and rng.random() < 0.6
            or loss == "bursts"
            and tick % 90 < 60
            or loss == "late"
            and tick < count * 3
        ):
            continue
        _, seq, total, payload = _split_part(frame)
        number, n, length, checksum, data = loads(payload)
        assert (number, n, length, checksum) == (seq, total, size, zlib.crc32(message))
        pending.append((choose_fragments(seq, total, checksum), data))
        while True:
            progress = False
            remaining = []
            for indexes, data in pending:
                indexes = set(indexes)
                for index in list(indexes):
                    if index in known:
                        data = bytes(a ^ b for a, b in zip(data, known[index]))
                        indexes.remove(index)
                if len(indexes) == 1:
                    known[indexes.pop()] = data
                    progress = True
                elif indexes:
                    remaining.append((indexes, data))
                else:
                    assert data == bytes(len(data))
            pending = remaining
            if not progress:
                break
        if len(known) == count:
            assert b"".join(known[i] for i in range(count))[:size] == message
            return
    pytest.fail(f"incomplete {loss} transfer: {len(known)}/{count}")
