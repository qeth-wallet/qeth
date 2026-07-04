"""EIP-4527 Ethereum UR registry — ``eth-sign-request`` (we build) and
``eth-signature`` (we parse), over CBOR (cbor2) + single-part UR.

Wire format pinned to Keystone's reference (KeystoneHQ/keystone-airgaped-base
ur-registry-eth): integer map keys, ``data-type`` a plain int, request-id in
CBOR tag 37 (UUID), derivation path in tag 304 (crypto-keypath). Structurally
gated in tests/test_qr_eth.py.
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass

from cbor2 import CBORTag, dumps, loads

from . import multipart, ur


def _uuid_bytes(value: object) -> bytes:
    """Normalise a decoded request-id to its 16 raw bytes. cbor2 turns tag 37
    into a ``uuid.UUID``; some encoders leave it a bare ``CBORTag`` or bytes."""
    if isinstance(value, _uuid.UUID):
        return value.bytes
    if isinstance(value, CBORTag):
        value = value.value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return b""

# CBOR tags (BC registry / EIP-4527).
UUID_TAG = 37
KEYPATH_TAG = 304

# eth-sign-request map keys.
_REQUEST_ID, _SIGN_DATA, _DATA_TYPE, _CHAIN_ID, _PATH, _ADDRESS, _ORIGIN = range(1, 8)


class DataType:
    """``eth-sign-request`` data-type enum (EIP-4527 / Keystone)."""

    TRANSACTION = 1        # legacy transaction RLP (unsigned)
    TYPED_DATA = 2         # EIP-712 typed data
    PERSONAL_MESSAGE = 3   # personal_sign raw bytes
    TYPED_TRANSACTION = 4  # EIP-2718 typed transaction (e.g. EIP-1559), unsigned


def parse_derivation_path(path: str) -> list:
    """BIP-32 path → the flat crypto-keypath ``components`` array
    ``[index, is-hardened, …]``. Accepts ``'`` or ``h``/``H`` for hardened and
    an optional leading ``m/``."""
    components: list = []
    for element in path.strip().split("/"):
        if element in ("", "m", "M"):
            continue
        hardened = element[-1] in ("'", "h", "H")
        index = int(element[:-1] if hardened else element)
        components.extend((index, hardened))
    return components


def _crypto_keypath(path: str, source_fingerprint: int) -> CBORTag:
    keypath: dict = {1: parse_derivation_path(path)}
    if source_fingerprint:
        keypath[2] = source_fingerprint   # master fingerprint (xfp)
    return CBORTag(KEYPATH_TAG, keypath)


def encode_eth_sign_request(
    *,
    sign_data: bytes,
    data_type: int,
    chain_id: int,
    path: str,
    source_fingerprint: int,
    address: bytes | None = None,
    origin: str | None = None,
    request_id: bytes | None = None,
) -> tuple[list[str], bytes]:
    """Build an ``eth-sign-request`` and return ``(ur_parts, request_id)`` — the
    list of ``ur:…`` strings for the (possibly animated) QR. A big tx (a swap's
    calldata) spans several parts; a small one is a single part.

    ``sign_data`` is the opaque payload the device signs (an unsigned tx RLP /
    typed-tx serialization / message). ``request_id`` correlates the response;
    a fresh UUID is generated when omitted."""
    rid = request_id if request_id is not None else _uuid.uuid4().bytes
    body: dict = {
        _REQUEST_ID: CBORTag(UUID_TAG, rid),
        _SIGN_DATA: sign_data,
        _DATA_TYPE: int(data_type),
        _CHAIN_ID: int(chain_id),
        _PATH: _crypto_keypath(path, source_fingerprint),
    }
    if address is not None:
        body[_ADDRESS] = address
    if origin is not None:
        body[_ORIGIN] = origin
    parts = multipart.encode_parts("eth-sign-request", dumps(body, canonical=True))
    return parts, rid


@dataclass(frozen=True)
class EthSignature:
    request_id: bytes   # the UUID bytes echoed from the request
    signature: bytes    # 65 bytes: r ‖ s ‖ v


def parse_eth_signature(ur_string: str) -> EthSignature:
    """Parse an ``eth-signature`` UR (the device's response) into
    ``(request_id, signature)``. Raises ``ValueError`` on a wrong UR type or a
    malformed body."""
    ur_type, payload = ur.decode(ur_string)
    if ur_type != "eth-signature":
        raise ValueError(f"expected an eth-signature UR, got {ur_type!r}")
    body = loads(payload)
    if not isinstance(body, dict):
        raise ValueError("eth-signature body is not a map")
    sig = body.get(2)
    if not isinstance(sig, (bytes, bytearray)):
        raise ValueError("eth-signature has no signature bytes")
    return EthSignature(
        request_id=_uuid_bytes(body.get(1)),
        signature=bytes(sig),
    )
