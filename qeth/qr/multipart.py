"""Multi-part (animated) UR encoding — BCR-2024-001.

A large payload (a swap's calldata makes a single QR far too dense for the
device to read) is split into a sequence of small QR "parts" shown as an
animated QR. We emit both the **fixed-rate** parts (``seqNum`` 1..``seqLen``,
each one pure fragment) AND ``seqLen`` **rateless** fountain parts
(``seqNum > seqLen``, a Xoshiro-mixed XOR of fragments — see :mod:`qeth.qr.fountain`).
The rateless parts are what remove the coupon-collector tail: the receiver
reconstructs from ANY ~seqLen parts, so a missed frame never forces a wait for
one specific fragment. A payload that fits one fragment stays a plain
single-part UR, byte-identical to what already works.

Part wire format (pinned to the spec vector in tests):
``ur:<type>/<seqNum>-<seqLen>/<bytewords( CBOR [seqNum, seqLen, messageLen,
crc32(message), fragment] )>``.
"""

from __future__ import annotations

import math
import random
import zlib
from collections.abc import Callable, Iterator

from cbor2 import dumps, loads

from . import bytewords, fountain, ur

# encode_parts (a FINITE list, for tests/round-trips) emits this × seqLen parts.
# The live signing flow uses frame_source instead — an UNBOUNDED stream of fresh
# fountain parts, which is what actually lets the device converge without
# stalling+resetting near completion.
FOUNTAIN_RATIO = 2

# A payload up to this size is a single static QR — a normal tx stays one clean
# code (no animation, no coupon-collector tail). Sized to hold a simple send.
SINGLE_PART_MAX = 150
# When a payload IS too big (a swap's calldata), fragment it into pieces this
# size. At 120 bytes each part is typically ~version 9 rather than ~version 12
# at 220 bytes. Larger modules help low-resolution cameras lock on, at the cost
# of more frames. Very large payloads still need denser parts to fit the cap.
FRAGMENT_LEN = 120
# BC-UR receivers reassemble into a fixed-size part table and reject a message
# with more parts than it holds — the Keycard Shell caps at 128 (its
# UR_MAX_PART_COUNT). _fragment_len_for() packs denser than FRAGMENT_LEN when a
# payload is large enough that FRAGMENT_LEN would exceed this.
MAX_FRAGMENTS = 128


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _fragments(message: bytes, seq_len: int, frag_len: int) -> list[bytes]:
    padded = message + b"\x00" * (seq_len * frag_len - len(message))
    return [padded[i * frag_len : (i + 1) * frag_len] for i in range(seq_len)]


def _plan(message: bytes, fragment_len: int) -> tuple[int, list[bytes], int]:
    seq_len = math.ceil(len(message) / fragment_len)
    frags = _fragments(message, seq_len, math.ceil(len(message) / seq_len))
    return seq_len, frags, _crc32(message)


def _fragment_len_for(message_len: int) -> int:
    """Bytes per fragment for a payload of ``message_len``: :data:`FRAGMENT_LEN`
    (QR ~v9) normally, but packed denser when the payload is big enough that
    FRAGMENT_LEN would split it into more than :data:`MAX_FRAGMENTS` parts (which
    a Keycard-class receiver rejects outright)."""
    return max(FRAGMENT_LEN, -(-message_len // MAX_FRAGMENTS))


def _part(
    ur_type: str,
    seq_num: int,
    seq_len: int,
    message_len: int,
    checksum: int,
    frags: list[bytes],
) -> str:
    """One ``ur:…`` part. seqNum 1..seqLen is a pure fragment; beyond that a
    rateless fountain mix (fountain.choose_fragments picks the same set the
    device will)."""
    data = fountain.mix(frags, fountain.choose_fragments(seq_num, seq_len, checksum))
    cbor = dumps([seq_num, seq_len, message_len, checksum, data], canonical=True)
    return f"ur:{ur_type}/{seq_num}-{seq_len}/{bytewords.encode(cbor)}"


# Search a disjoint uint32 sequence range, bounded per message and per retry.
RETRY_SEARCH_LIMIT = 16_384
RETRY_SEARCH_BATCH = 32
RETRY_SEQUENCE_START = 1 << 31


class _PlainRetries:
    """Find degree-one fountain sequences without delaying the first pass.

    Shell runs its pending-equation recovery routine for seqNum > seqLen only.
    A valid degree-one fountain part delivers the same plain bytes through that
    path. Missing aliases fall back to the ordinary systematic sequence.
    """

    def __init__(self, seq_len: int, checksum: int) -> None:
        self.seq_len = seq_len
        self.checksum = checksum
        self.searched = 0
        self.aliases: dict[int, int] = {}

    def sequence(self, index: int) -> int:
        if len(self.aliases) < self.seq_len:
            for _ in range(min(RETRY_SEARCH_BATCH, RETRY_SEARCH_LIMIT - self.searched)):
                sequence = RETRY_SEQUENCE_START + self.searched
                self.searched += 1
                indexes = fountain.choose_fragments(
                    sequence, self.seq_len, self.checksum
                )
                if len(indexes) == 1:
                    self.aliases.setdefault(next(iter(indexes)), sequence)
        return self.aliases.get(index, index + 1)


def frame_source(
    ur_type: str,
    message: bytes,
    *,
    single_part_max: int = SINGLE_PART_MAX,
    fragment_len: int | None = None,
    rng: random.Random | None = None,
) -> Callable[[], str]:
    """First show every original chunk, then shuffled groups of two plain
    retries and one fresh recovery part. Plain coverage stays cyclic across
    groups; recovery sequences retain the standard fountain distribution.

    Retry aliases are searched incrementally with ordinary retries as fallback.
    Small requests remain a static single UR. The optional RNG makes diagnostic
    comparisons reproducible without changing transaction contents.
    """
    if len(message) <= single_part_max:
        part = ur.encode(ur_type, message)
        return lambda: part
    if fragment_len is None:
        fragment_len = _fragment_len_for(len(message))
    seq_len, frags, checksum = _plan(message, fragment_len)
    order = rng if rng is not None else random.Random()
    retries = _PlainRetries(seq_len, checksum)

    def frames() -> Iterator[str]:
        for n in range(1, seq_len + 1):
            yield _part(ur_type, n, seq_len, len(message), checksum, frags)
        pure = 0
        rateless = seq_len
        while True:
            sequences = [retries.sequence(pure), retries.sequence((pure + 1) % seq_len)]
            pure = (pure + 2) % seq_len
            rateless += 1
            sequences.append(rateless)
            order.shuffle(sequences)
            for sequence in sequences:
                yield _part(ur_type, sequence, seq_len, len(message), checksum, frags)

    gen = frames()
    return lambda: next(gen)


def encode_parts(
    ur_type: str,
    message: bytes,
    *,
    single_part_max: int = SINGLE_PART_MAX,
    fragment_len: int = FRAGMENT_LEN,
) -> list[str]:
    """A FINITE list of parts (single, else FOUNTAIN_RATIO×seqLen). Used by tests
    and decode_parts round-trips; the live flow uses :func:`frame_source`."""
    if len(message) <= single_part_max:
        return [ur.encode(ur_type, message)]
    seq_len, frags, checksum = _plan(message, fragment_len)
    return [
        _part(ur_type, n, seq_len, len(message), checksum, frags)
        for n in range(1, FOUNTAIN_RATIO * seq_len + 1)
    ]


def _split_part(ur_string: str) -> tuple[str, int | None, int | None, bytes]:
    """Parse one part → ``(ur_type, seqNum, seqLen, cbor_bytes)``. ``seqNum`` /
    ``seqLen`` are ``None`` for a single-part UR."""
    s = ur_string.strip().lower()
    if not s.startswith("ur:"):
        raise ValueError("not a UR")
    segs = s[3:].split("/")
    if len(segs) == 2:
        return segs[0], None, None, bytewords.decode(segs[1])
    if len(segs) == 3:
        seq_num, seq_len = (int(x) for x in segs[1].split("-"))
        return segs[0], seq_num, seq_len, bytewords.decode(segs[2])
    raise ValueError("malformed UR part")


def decode_parts(parts: list[str]) -> tuple[str, bytes]:
    """Reassemble fixed-rate parts into ``(ur_type, message)``. Handles the
    single-part case, ignores any rateless (``seqNum > seqLen``) parts we don't
    emit, and verifies the CRC-32. Mainly for tests + robustness — the device
    response (a signature) is single-part. Raises until fully reassembled."""
    if len(parts) == 1:
        ur_type, seq_num, _seq_len, payload = _split_part(parts[0])
        if seq_num is None:
            return ur_type, payload

    ur_type = ""
    seq_len = message_len = checksum = None
    fragments: dict[int, bytes] = {}
    for part in parts:
        typ, seq_num, sl, cbor = _split_part(part)
        if seq_num is None or sl is None or seq_num > sl:  # single-part / rateless
            continue
        seq_num, sl, mlen, chk, data = loads(cbor)
        ur_type, seq_len, message_len, checksum = typ, sl, mlen, chk
        fragments[seq_num] = bytes(data)
    if seq_len is None or len(fragments) < seq_len:
        raise ValueError("incomplete multi-part UR")
    message = b"".join(fragments[i] for i in range(1, seq_len + 1))[:message_len]
    if _crc32(message) != checksum:
        raise ValueError("multi-part UR checksum mismatch")
    return ur_type, message
