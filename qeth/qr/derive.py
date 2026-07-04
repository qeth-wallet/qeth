"""BIP32 public-child key derivation (CKDpub) + Ethereum addresses.

An air-gapped wallet exports an account-level extended *public* key (compressed
pubkey + chain code); qeth derives the individual addresses locally, without
another QR exchange — the "one scan → many addresses" case (BIP44 / Legacy).

secp256k1 point math is reused from eth-keys' vetted native backend
(``fast_add`` / ``fast_multiply``); only the (trivial) point compression is
ours. Non-hardened derivation only — hardened public derivation is impossible by
construction, which is exactly why Ledger-Live accounts need a scan each.
Cross-checked against private derivation (the defining BIP32 property) in tests.
"""

from __future__ import annotations

import hashlib
import hmac

from eth_keys import keys
from eth_keys.backends.native.ecdsa import G, N, P, fast_add, fast_multiply

_HARDENED = 1 << 31


def _decompress(pubkey: bytes) -> tuple[int, int]:
    """A 33-byte compressed secp256k1 point → ``(x, y)``. y² = x³ + 7; P ≡ 3
    (mod 4), so the square root is a single modular exponentiation."""
    if len(pubkey) != 33 or pubkey[0] not in (2, 3):
        raise ValueError("not a 33-byte compressed secp256k1 point")
    x = int.from_bytes(pubkey[1:], "big")
    y = pow((pow(x, 3, P) + 7) % P, (P + 1) // 4, P)
    if (y & 1) != (pubkey[0] & 1):
        y = P - y
    return x, y


def _compress(point: tuple[int, int]) -> bytes:
    x, y = point
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")


def ckd_pub(pubkey: bytes, chain_code: bytes, index: int) -> tuple[bytes, bytes]:
    """One non-hardened CKDpub step: ``(parent_pubkey, chain_code, i)`` →
    ``(child_pubkey, child_chain_code)``. Raises on a hardened index (can't be
    done from a public key) or the ~2⁻¹²⁷ invalid-child case."""
    if index >= _HARDENED:
        raise ValueError("cannot derive a hardened child from a public key")
    digest = hmac.new(
        chain_code, pubkey + index.to_bytes(4, "big"), hashlib.sha512).digest()
    il = int.from_bytes(digest[:32], "big")
    if il >= N:
        raise ValueError("invalid child key (IL ≥ n)")
    child_point = fast_add(fast_multiply(G, il), _decompress(pubkey))
    return _compress(child_point), digest[32:]


def derive_pubkey(pubkey: bytes, chain_code: bytes, path: list[int]) -> bytes:
    """Walk a list of non-hardened indices from ``(pubkey, chain_code)`` and
    return the leaf's compressed pubkey."""
    for index in path:
        pubkey, chain_code = ckd_pub(pubkey, chain_code, index)
    return pubkey


def pubkey_to_address(pubkey: bytes) -> str:
    """A 33-byte compressed secp256k1 pubkey → EIP-55 checksummed address."""
    x, y = _decompress(pubkey)
    public = keys.PublicKey(x.to_bytes(32, "big") + y.to_bytes(32, "big"))
    return public.to_checksum_address()


def derive_address(pubkey: bytes, chain_code: bytes, path: list[int]) -> str:
    """Derive the address ``path`` (non-hardened indices) below an account-level
    extended public key."""
    return pubkey_to_address(derive_pubkey(pubkey, chain_code, path))
