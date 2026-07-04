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

import itertools
import math
import zlib
from collections.abc import Callable

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
# size. At 150 each part's QR is ~version 10 (57×57) — the low end of what
# Keystone/Sparrow use, dense enough to keep the frame count (and animation)
# short, still comfortably camera-readable. The rateless fountain parts absorb
# the occasional missed dense frame, so density here is cheap. Drop it (→ lower
# QR version, more frames) only if a device's camera can't lock on reliably.
FRAGMENT_LEN = 150


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _fragments(message: bytes, seq_len: int, frag_len: int) -> list[bytes]:
    padded = message + b"\x00" * (seq_len * frag_len - len(message))
    return [padded[i * frag_len:(i + 1) * frag_len] for i in range(seq_len)]


def _plan(message: bytes, fragment_len: int) -> tuple[int, list[bytes], int]:
    seq_len = math.ceil(len(message) / fragment_len)
    frags = _fragments(message, seq_len, math.ceil(len(message) / seq_len))
    return seq_len, frags, _crc32(message)


def _part(ur_type: str, seq_num: int, seq_len: int, message_len: int,
          checksum: int, frags: list[bytes]) -> str:
    """One ``ur:…`` part. seqNum 1..seqLen is a pure fragment; beyond that a
    rateless fountain mix (fountain.choose_fragments picks the same set the
    device will)."""
    data = fountain.mix(frags, fountain.choose_fragments(seq_num, seq_len, checksum))
    cbor = dumps([seq_num, seq_len, message_len, checksum, data], canonical=True)
    return f"ur:{ur_type}/{seq_num}-{seq_len}/{bytewords.encode(cbor)}"


def frame_source(
    ur_type: str, message: bytes, *,
    single_part_max: int = SINGLE_PART_MAX, fragment_len: int = FRAGMENT_LEN,
) -> Callable[[], str]:
    """Return ``next_frame() -> ur_string`` for the exchange dialog to show one
    QR per animation tick. Small payload → a constant single part; large payload
    → an UNBOUNDED stream of ever-fresh parts (pure fragments 1..seqLen first,
    then rateless fountain parts forever), so the device keeps getting new
    equations and converges without stalling."""
    if len(message) <= single_part_max:
        part = ur.encode(ur_type, message)
        return lambda: part
    seq_len, frags, checksum = _plan(message, fragment_len)
    counter = itertools.count(1)
    return lambda: _part(
        ur_type, next(counter), seq_len, len(message), checksum, frags)


def encode_parts(
    ur_type: str, message: bytes, *,
    single_part_max: int = SINGLE_PART_MAX, fragment_len: int = FRAGMENT_LEN,
) -> list[str]:
    """A FINITE list of parts (single, else FOUNTAIN_RATIO×seqLen). Used by tests
    and decode_parts round-trips; the live flow uses :func:`frame_source`."""
    if len(message) <= single_part_max:
        return [ur.encode(ur_type, message)]
    seq_len, frags, checksum = _plan(message, fragment_len)
    return [_part(ur_type, n, seq_len, len(message), checksum, frags)
            for n in range(1, FOUNTAIN_RATIO * seq_len + 1)]


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
