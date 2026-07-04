"""Single-part Uniform Resources (BCR-2020-005): ``ur:<type>/<bytewords>``.

A UR wraps a CBOR payload (encoded as minimal Bytewords) behind a type tag. This
handles the single-part form only — a lone ``eth-sign-request`` / ``eth-signature``
fits one part. The multi-part fountain form (``ur:<type>/<seq>-<len>/<frag>``,
the animated QR) is deferred; it's detected and rejected with a clear error.
"""

from __future__ import annotations

import re

from . import bytewords

# UR type: lowercase letters, digits, hyphens (BCR-2020-005 §Types).
_TYPE_RE = re.compile(r"^[a-z0-9-]+$")


def encode(ur_type: str, payload: bytes) -> str:
    """CBOR ``payload`` → ``ur:<ur_type>/<minimal-bytewords>``."""
    if not _TYPE_RE.match(ur_type):
        raise ValueError(f"invalid UR type: {ur_type!r}")
    return f"ur:{ur_type}/{bytewords.encode(payload, minimal=True)}"


def decode(ur_string: str) -> tuple[str, bytes]:
    """``ur:<type>/<bytewords>`` → ``(type, cbor_payload)``. Raises ``ValueError``
    on a malformed UR, an unknown type shape, or a multi-part UR (not yet
    supported). UR is case-insensitive; we normalise to lowercase."""
    s = ur_string.strip().lower()
    if not s.startswith("ur:"):
        raise ValueError("not a UR (missing 'ur:' scheme)")
    parts = s[3:].split("/")
    if len(parts) < 2:
        raise ValueError("malformed UR (no payload)")
    ur_type = parts[0]
    if not _TYPE_RE.match(ur_type):
        raise ValueError(f"invalid UR type: {ur_type!r}")
    if len(parts) > 2:
        # ur:<type>/<seqNum>-<seqLen>/<fragment> — the animated, fountain form.
        raise ValueError(
            "multi-part (animated) UR is not supported yet — "
            "the payload must fit a single QR")
    return ur_type, bytewords.decode(parts[1], minimal=True)
