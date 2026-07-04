"""BC-UR fountain (qeth/qr/fountain.py), pinned BIT-EXACTLY to the BCR-2024-001
'Wolf' test vector. The device rebuilds each rateless mix from seqNum+checksum,
so a single wrong bit here would corrupt its decode — hence a hard vector pin."""

import hashlib
import zlib

import pytest

from qeth.qr.fountain import Xoshiro256, choose_fragments, mix

CHECKSUM = 0x0167AA07
FRAG0 = bytes.fromhex("916ec65cf77cadf55cd7f9cda1a1030026ddd42e905b77adc36e4f2d3c")
PART10 = bytes.fromhex("330f0f33a05eead4f331df229871bee733b50de71afd2e5a79f196de09")
PART11 = bytes.fromhex("3b205ce5e52d8c24a52cffa34c564fa1af3fdffcd349dc4258ee4ee828")


def _wolf() -> bytes:
    # bc-ur makeMessage("Wolf", 256): Xoshiro256 seeded from sha256("Wolf"), one
    # byte = next() >> 56 per output. The CRC-32 over all 256 bytes validates the
    # RNG (256 outputs) end to end.
    rng = Xoshiro256(hashlib.sha256(b"Wolf").digest())
    message = bytes((rng.next() >> 56) for _ in range(256))
    assert zlib.crc32(message) & 0xFFFFFFFF == CHECKSUM
    return message


def _fragments() -> list[bytes]:
    wolf = _wolf()
    seq_len, frag_len = 9, 29
    padded = wolf + b"\x00" * (seq_len * frag_len - len(wolf))
    return [padded[i * frag_len:(i + 1) * frag_len] for i in range(seq_len)]


def test_reconstructed_wolf_and_fragment_zero():
    assert _fragments()[0] == FRAG0            # (also asserts the CRC inside)


def test_choose_fragments_matches_the_spec_indexes():
    assert choose_fragments(1, 9, CHECKSUM) == {0}      # pure fragment
    assert choose_fragments(9, 9, CHECKSUM) == {8}      # pure fragment (last)
    assert choose_fragments(10, 9, CHECKSUM) == {0, 2, 3, 5, 6, 8}
    assert choose_fragments(11, 9, CHECKSUM) == {1, 2, 4, 5, 6, 8}


def test_rateless_part_data_matches_the_spec():
    frags = _fragments()
    assert mix(frags, choose_fragments(10, 9, CHECKSUM)) == PART10
    assert mix(frags, choose_fragments(11, 9, CHECKSUM)) == PART11


def test_mix_of_a_single_index_is_that_fragment():
    frags = _fragments()
    assert mix(frags, {4}) == frags[4]


def test_xoshiro_rejects_a_bad_seed_length():
    with pytest.raises(ValueError):
        Xoshiro256(b"\x00" * 16)
