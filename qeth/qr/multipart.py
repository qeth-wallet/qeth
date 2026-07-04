"""Multi-part (animated) UR encoding — BCR-2024-001.

A large payload (a swap's calldata makes a single QR far too dense for the
device to read) is split into a sequence of small QR "parts" shown as an
animated QR. We emit the **fixed-rate** parts (``seqNum`` 1..``seqLen``): each
carries one pure fragment, and every BC-UR decoder reconstructs the message once
it has seen all ``seqLen`` of them — no randomness involved. The **rateless**
fountain parts (``seqNum > seqLen``, a Xoshiro-mixed XOR of fragments) are only a
loss-tolerance optimisation for lossy channels; a screen→camera exchange cycles,
so it doesn't need them (and hand-rolling the RNG bit-exactly is where the risk
is). A payload that fits one fragment is emitted as the plain single-part UR,
byte-identical to what already works.

Part wire format (pinned to the spec vector in tests):
``ur:<type>/<seqNum>-<seqLen>/<bytewords( CBOR [seqNum, seqLen, messageLen,
crc32(message), fragment] )>``.
"""

from __future__ import annotations

import math
import zlib

from cbor2 import dumps, loads

from . import bytewords, ur

# Message bytes per fragment. Small enough that each part's QR stays a low,
# reliably-scannable version; a typical tx stays single-part, a swap animates.
MAX_FRAGMENT_LEN = 150


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _fragments(message: bytes, seq_len: int, frag_len: int) -> list[bytes]:
    padded = message + b"\x00" * (seq_len * frag_len - len(message))
    return [padded[i * frag_len:(i + 1) * frag_len] for i in range(seq_len)]


def encode_parts(
    ur_type: str, message: bytes, *, max_fragment_len: int = MAX_FRAGMENT_LEN,
) -> list[str]:
    """A payload → the list of ``ur:…`` part strings for an animated QR (a
    single plain part when it fits ``max_fragment_len``)."""
    if len(message) <= max_fragment_len:
        return [ur.encode(ur_type, message)]
    seq_len = math.ceil(len(message) / max_fragment_len)
    frag_len = math.ceil(len(message) / seq_len)
    checksum = _crc32(message)
    parts = []
    for i, fragment in enumerate(_fragments(message, seq_len, frag_len)):
        part = dumps(
            [i + 1, seq_len, len(message), checksum, fragment], canonical=True)
        parts.append(
            f"ur:{ur_type}/{i + 1}-{seq_len}/{bytewords.encode(part)}")
    return parts


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
