"""Hot wallet keystores — Ethereum's standard JSON Web3 Secret
Storage v3 format, same as ``~/.brownie/accounts/*.json``.

The encrypted file lives at ``~/.qeth/keystores/<addr_lower>.json``
and holds an scrypt-encrypted private key. Decryption is
intentionally slow (scrypt KDF — that's the whole point); both
encrypt and decrypt are run off the Qt main thread by their
callers.

The keystore file + the passphrase are BOTH required to recover
the funds. Lose either and the key is gone. The application
never transmits either anywhere.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from .chains import Chain
from .signing import (
    MessageSigningRequest, Signer, SignerError, SigningRequest,
    TypedDataSigningRequest,
)


log = logging.getLogger("qeth.hot_wallet")


KEYSTORE_DIR = Path.home() / ".qeth" / "keystores"


def keystore_path(address: str) -> Path:
    """Filesystem path for the keystore of ``address``. Addresses
    are stored lower-cased so the file name is deterministic
    regardless of how the caller cases the input."""
    return KEYSTORE_DIR / f"{address.lower()}.json"


def encrypt_keystore(private_key: bytes, passphrase: str) -> tuple[str, dict]:
    """Encrypt a 32-byte private key under ``passphrase`` and
    return (checksummed_address, keystore_dict). The caller picks
    the key — either generated via ``os.urandom(32)`` (see
    ``generate_random_private_key``) or pasted by the user.
    Persistence is the caller's job; pass the dict to
    ``save_keystore``."""
    if len(private_key) != 32:
        raise SignerError(
            f"Private key must be 32 bytes (got {len(private_key)})"
        )
    from eth_account import Account
    acct = Account.from_key(private_key)
    keystore = Account.encrypt(private_key, passphrase)
    return acct.address, keystore


def generate_random_private_key() -> bytes:
    """Cryptographically random 32-byte private key. ``os.urandom``
    reads from ``/dev/urandom`` on Linux / macOS and
    ``BCryptGenRandom`` on Windows — both are CSPRNG, so we don't
    need an OS-specific entropy source."""
    return os.urandom(32)


def parse_private_key_hex(text: str) -> bytes:
    """Accept a 64-char hex string (with or without ``0x``) and
    return the raw 32 bytes. Raises ``SignerError`` on anything
    that isn't a well-formed 32-byte hex private key."""
    text = text.strip()
    if text.startswith("0x") or text.startswith("0X"):
        text = text[2:]
    if len(text) != 64:
        raise SignerError(
            "Private key must be 64 hex characters (32 bytes); "
            f"got {len(text)}"
        )
    try:
        priv = bytes.fromhex(text)
    except ValueError as e:
        raise SignerError(f"Invalid hex in private key: {e}") from e
    return priv


def save_keystore(address: str, keystore: dict) -> Path:
    """Write the keystore JSON to disk and return its path. Fails
    if a keystore for the address already exists — overwriting
    silently would be a footgun."""
    KEYSTORE_DIR.mkdir(parents=True, exist_ok=True)
    p = keystore_path(address)
    if p.exists():
        raise SignerError(f"Keystore already exists at {p}")
    p.write_text(json.dumps(keystore))
    return p


def load_keystore(address: str) -> dict:
    """Return the keystore dict for ``address``. Raises
    ``SignerError`` when missing or unreadable — callers use this
    via HotWalletSigner.sign so the same uniform error shape
    surfaces to the dapp."""
    p = keystore_path(address)
    if not p.exists():
        raise SignerError(f"No keystore on disk for {address}")
    try:
        return json.loads(p.read_text())
    except Exception as e:
        raise SignerError(f"Failed to read keystore: {e}") from e


def delete_keystore(address: str) -> bool:
    """Remove the keystore file for ``address``. Returns True if
    a file was removed, False if it didn't exist. Used by the
    wallets plugin's Remove flow so the on-disk key dies with the
    account record."""
    p = keystore_path(address)
    if not p.exists():
        return False
    p.unlink()
    return True


class HotWalletSigner(Signer):
    """``Signer`` backed by a passphrase-encrypted keystore on
    disk. The passphrase is collected on the main thread by the
    host (so the user sees the prompt before the worker starts)
    and passed in at construction; sign() then runs on the worker
    thread where the scrypt KDF doesn't block the UI."""

    def __init__(self, store, passphrase: str):
        self._store = store
        self._passphrase = passphrase

    def can_sign(self, address: str) -> bool:
        addr = address.lower()
        if not any(a.get("source") == "hot"
                    and a["address"].lower() == addr
                    for a in self._store.accounts):
            return False
        return keystore_path(address).exists()

    def _load_priv(self, address: str) -> bytes:
        """Shared decrypt path for sign / sign_message /
        sign_typed_data — scrypt + AES + the friendly
        ``Wrong passphrase`` re-raise."""
        if not self.can_sign(address):
            raise SignerError(
                f"No hot wallet keystore for {address}"
            )
        keystore = load_keystore(address)
        try:
            from eth_account import Account
            return Account.decrypt(keystore, self._passphrase)
        except ValueError as e:
            msg = str(e).lower()
            if "mac" in msg or "password" in msg or "decryption" in msg:
                raise SignerError("Wrong passphrase.") from e
            raise SignerError(f"Failed to decrypt keystore: {e}") from e
        except Exception as e:
            raise SignerError(f"Failed to decrypt keystore: {e}") from e

    def sign_message(self, req: MessageSigningRequest) -> bytes:
        """personal_sign — EIP-191 prefixed bytes, 65-byte ECDSA."""
        priv = self._load_priv(req.from_addr)
        from eth_account import Account
        from eth_account.messages import encode_defunct
        signable = encode_defunct(primitive=req.raw)
        signed = Account.sign_message(signable, private_key=priv)
        return self._extract_signature(signed)

    def sign_typed_data(self, req: TypedDataSigningRequest) -> bytes:
        """EIP-712 — domain + types + primaryType + message."""
        priv = self._load_priv(req.from_addr)
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        # encode_typed_data accepts the full v4 typed-data dict
        # (with EIP712Domain in types or auto-inferred).
        signable = encode_typed_data(full_message=req.typed_data)
        signed = Account.sign_message(signable, private_key=priv)
        return self._extract_signature(signed)

    @staticmethod
    def _extract_signature(signed) -> bytes:
        """``SignedMessage`` exposes ``signature`` (Hexbytes) — but
        the attribute name varies across eth_account versions
        (some have ``signature``, some only the int components).
        Try the common shapes and fall back to constructing from
        r/s/v if needed."""
        sig = getattr(signed, "signature", None)
        if sig is not None:
            if hasattr(sig, "to_0x_hex"):
                hex_str = sig.to_0x_hex()
            elif hasattr(sig, "hex"):
                h = sig.hex()
                hex_str = h if h.startswith("0x") else "0x" + h
            else:
                hex_str = str(sig)
            return bytes.fromhex(hex_str[2:])
        # Fallback: build from r/s/v (eth_account always has these).
        r = signed.r.to_bytes(32, "big")
        s = signed.s.to_bytes(32, "big")
        v = bytes([signed.v])
        return r + s + v

    def sign(self, req: SigningRequest, chain: Chain) -> bytes:
        if not self.can_sign(req.from_addr):
            raise SignerError(
                f"No hot wallet keystore for {req.from_addr}"
            )
        keystore = load_keystore(req.from_addr)
        # eth_account validates EVERYTHING; bad passphrase raises
        # ValueError with a fixed message. Map that to a friendly
        # SignerError so the dialog popup the host parents to the
        # sign dialog reads as "wrong passphrase" rather than the
        # raw exception text.
        try:
            from eth_account import Account
            priv = Account.decrypt(keystore, self._passphrase)
        except ValueError as e:
            msg = str(e).lower()
            if "mac" in msg or "password" in msg or "decryption" in msg:
                raise SignerError("Wrong passphrase.") from e
            raise SignerError(f"Failed to decrypt keystore: {e}") from e
        except Exception as e:
            raise SignerError(f"Failed to decrypt keystore: {e}") from e

        if req.gas is None or req.nonce is None:
            raise SignerError("gas and nonce must be set before signing")

        tx: dict = {
            "chainId": chain.chain_id,
            "nonce": req.nonce,
            "to": req.to_addr,
            "value": req.value_wei,
            "data": req.data or "0x",
            "gas": req.gas,
        }
        if chain.eip1559:
            if (req.max_fee_per_gas is None
                    or req.max_priority_fee_per_gas is None):
                raise SignerError(
                    "EIP-1559 fees missing — finalise gas suggestion first"
                )
            tx["maxFeePerGas"] = req.max_fee_per_gas
            tx["maxPriorityFeePerGas"] = req.max_priority_fee_per_gas
            tx["type"] = 2
        else:
            if req.gas_price is None:
                raise SignerError(
                    "Legacy gas_price missing — finalise gas suggestion first"
                )
            tx["gasPrice"] = req.gas_price
            tx["type"] = 0

        from eth_account import Account
        acct = Account.from_key(priv)
        signed = acct.sign_transaction(tx)
        # eth_account exposes the encoded tx as ``rawTransaction``
        # (property) or ``raw_transaction`` (varies by version).
        raw = getattr(signed, "rawTransaction", None)
        if raw is None:
            raw = getattr(signed, "raw_transaction", None)
            if callable(raw):
                raw = raw()
        if raw is None:
            raise SignerError("Signed transaction has no raw payload")
        if isinstance(raw, str):
            raw = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
        return bytes(raw)
