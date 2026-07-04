"""Parse an air-gapped wallet's account export — a ``crypto-hdkey`` UR
(BCR-2020-007): an account-level extended public key qeth derives addresses
below (see :mod:`qeth.qr.derive`).

Lenient on the two BC tag generations — legacy 303/304 (what Keystone's eth
registry uses) and the current IANA-range 40303/40304 — and on the UR type name
(``crypto-hdkey`` vs ``hdkey``), since firmware differs. The bundled
``crypto-account`` (several keys in one QR, for Ledger-Live per-account) is a
later slice.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from cbor2 import CBORTag, loads

from . import ur

_HDKEY_TAGS = (303, 40303)
_KEYPATH_TAGS = (304, 40304)

# hdkey map keys (BCR-2020-007).
_KEY_DATA, _CHAIN_CODE, _ORIGIN, _PARENT_FP = 3, 4, 6, 8
# keypath map keys.
_COMPONENTS, _SOURCE_FP = 1, 2


@dataclass(frozen=True)
class AccountKey:
    """An extended public key exported by the device."""

    pubkey: bytes             # 33-byte compressed secp256k1 pubkey
    chain_code: bytes         # 32 bytes
    origin_path: list         # crypto-keypath components: [index, hardened, …]
    source_fingerprint: int   # master key fingerprint (xfp), 0 if absent
    parent_fingerprint: int   # 0 if absent

    def master_fingerprint(self) -> int:
        """The fingerprint to put in a sign-request's derivation path: the
        origin's source-fingerprint (the master xfp) when present, else the
        parent fingerprint as a best effort."""
        return self.source_fingerprint or self.parent_fingerprint


def parse_account_export(ur_string: str) -> AccountKey:
    ur_type, payload = ur.decode(ur_string)
    if ur_type not in ("crypto-hdkey", "hdkey"):
        raise ValueError(
            f"expected a crypto-hdkey account export, got {ur_type!r}")
    body = loads(payload)
    if isinstance(body, CBORTag) and body.tag in _HDKEY_TAGS:
        body = body.value
    # cbor2 hands back maps nested in a tag as a frozendict, not a dict.
    if not isinstance(body, Mapping):
        raise ValueError("hdkey body is not a map")

    key_data = body.get(_KEY_DATA)
    if not isinstance(key_data, (bytes, bytearray)) or len(key_data) != 33:
        raise ValueError("hdkey is missing a 33-byte compressed key-data")
    chain_code = body.get(_CHAIN_CODE)
    if not isinstance(chain_code, (bytes, bytearray)) or len(chain_code) != 32:
        raise ValueError("hdkey is missing a 32-byte chain-code (need an xpub)")

    origin_path: list = []
    source_fp = 0
    origin = body.get(_ORIGIN)
    if isinstance(origin, CBORTag) and origin.tag in _KEYPATH_TAGS:
        keypath = origin.value
        if isinstance(keypath, Mapping):
            origin_path = list(keypath.get(_COMPONENTS, []) or [])
            source_fp = int(keypath.get(_SOURCE_FP, 0) or 0)

    return AccountKey(
        pubkey=bytes(key_data),
        chain_code=bytes(chain_code),
        origin_path=origin_path,
        source_fingerprint=source_fp,
        parent_fingerprint=int(body.get(_PARENT_FP, 0) or 0),
    )
