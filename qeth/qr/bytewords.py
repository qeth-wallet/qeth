"""Bytewords — Blockchain Commons BCR-2020-012.

Encodes bytes as QR-safe text: each byte maps to one of 256 four-letter words,
and a big-endian CRC32 of the payload is appended before encoding (so a decode
self-checks). Two styles: ``standard`` (space-joined full words) and ``minimal``
(each word's first+last letter, concatenated) — UR uses ``minimal``.

The 256-word list is spec data (BCR-2020-012), pinned by the published test
vector in tests/test_qr_bytewords.py; the algorithm is ours.
"""

from __future__ import annotations

import zlib

# The canonical 256 bytewords, index 0..255 (BCR-2020-012).
_WORDS = (
    "able acid also apex aqua arch atom aunt away axis "
    "back bald barn belt beta bias blue body brag brew "
    "bulb buzz calm cash cats chef city claw code cola "
    "cook cost crux curl cusp cyan dark data days deli "
    "dice diet door down draw drop drum dull duty each "
    "easy echo edge epic even exam exit eyes fact fair "
    "fern figs film fish fizz flap flew flux foxy free "
    "frog fuel fund gala game gear gems gift girl glow "
    "good gray grim guru gush gyro half hang hard hawk "
    "heat help high hill holy hope horn huts iced idea "
    "idle inch inky into iris iron item jade jazz join "
    "jolt jowl judo jugs jump junk jury keep keno kept "
    "keys kick kiln king kite kiwi knob lamb lava lazy "
    "leaf legs liar limp lion list logo loud love luau "
    "luck lung main many math maze memo menu meow mild "
    "mint miss monk nail navy need news next noon note "
    "numb obey oboe omit onyx open oval owls paid part "
    "peck play plus poem pool pose puff puma purr quad "
    "quiz race ramp real redo rich road rock roof ruby "
    "ruin runs rust safe saga scar sets silk skew slot "
    "soap solo song stub surf swan taco task taxi tent "
    "tied time tiny toil tomb toys trip tuna twin ugly "
    "undo unit urge user vast very veto vial vibe view "
    "visa void vows wall wand warm wasp wave waxy webs "
    "what when whiz wolf work yank yawn yell yoga yurt "
    "zaps zero zest zinc zone zoom"
).split()

assert len(_WORDS) == 256, f"bytewords list must be 256, got {len(_WORDS)}"

# byte value -> minimal 2-letter code (first + last letter of its word).
_MINIMAL = [w[0] + w[3] for w in _WORDS]
_MINIMAL_TO_BYTE = {code: i for i, code in enumerate(_MINIMAL)}
_WORD_TO_BYTE = {w: i for i, w in enumerate(_WORDS)}


def _crc32(data: bytes) -> bytes:
    return (zlib.crc32(data) & 0xFFFFFFFF).to_bytes(4, "big")


def encode(payload: bytes, *, minimal: bool = True) -> str:
    """``payload`` → bytewords (with the CRC32 tail appended). ``minimal`` is the
    compact first+last-letter form UR uses; otherwise space-joined full words."""
    body = payload + _crc32(payload)
    if minimal:
        return "".join(_MINIMAL[b] for b in body)
    return " ".join(_WORDS[b] for b in body)


def decode(text: str, *, minimal: bool = True) -> bytes:
    """Inverse of :func:`encode`. Verifies and strips the CRC32 tail; raises
    ``ValueError`` on an unknown word or a checksum mismatch."""
    if minimal:
        if len(text) % 2 != 0:
            raise ValueError("minimal bytewords length must be even")
        try:
            body = bytes(_MINIMAL_TO_BYTE[text[i:i + 2]]
                         for i in range(0, len(text), 2))
        except KeyError as e:
            raise ValueError(f"unknown byteword code {e}") from e
    else:
        try:
            body = bytes(_WORD_TO_BYTE[w] for w in text.split())
        except KeyError as e:
            raise ValueError(f"unknown byteword {e}") from e
    if len(body) < 4:
        raise ValueError("bytewords too short to hold a CRC32")
    payload, crc = body[:-4], body[-4:]
    if _crc32(payload) != crc:
        raise ValueError("bytewords CRC32 mismatch")
    return payload
