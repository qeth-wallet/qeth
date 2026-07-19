"""Import hot-wallet accounts from other wallet apps.

Two sources today, both pluggable behind ``ImportSource``:

- **Brownie** (``~/.brownie/accounts/*.json``) — already in Web3
  Secret Storage v3 (same as ours). Import is a file copy; the
  user's existing brownie passphrase is preserved and used to sign
  in qeth. Label defaults to the filename without ``.json``.

- **Frame** (``~/.config/frame/signers/*.json``, ``type:"ring"``) —
  custom format. ``encryptedKeys`` is ``salt:iv:ciphertext`` hex,
  scrypt(N=32768, r=8, p=1, dklen=32) → AES-256-CBC with PKCS7
  padding. Plaintext is colon-joined hex private keys in the same
  order as the ``addresses`` array. We decrypt with the Frame
  passphrase, derive each key into a qeth keystore via
  ``hot_wallet.encrypt_keystore`` under a new (or same) qeth
  passphrase. Format verified against Frame's worker.js in
  github.com/floating/frame/main/signers/hot/RingSigner.
"""

from __future__ import annotations

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from eth_utils import to_checksum_address


log = logging.getLogger("qeth.import_sources")


@dataclass
class ImportCandidate:
    """One discovered account from an external wallet. Holds enough
    to display in the import dialog (address + human label) and
    enough for the source to perform the import (``extras`` carries
    source-specific data — the brownie keystore dict, the Frame
    encryptedKeys blob, etc.)."""
    address: str       # EIP-55 checksummed
    label: str
    source_path: Path
    extras: dict = field(default_factory=dict)


class ImportSource(ABC):
    """Backend that knows how to enumerate and import accounts from
    one external wallet app. Concrete sources advertise whether
    they need a passphrase from the source app and/or a new one for
    the qeth keystore, so the dialog can show or hide the fields."""

    name: str = ""
    # Tells the dialog whether to show "Source passphrase" /
    # "New passphrase" fields. Brownie copies the keystore as-is
    # (passphrase unchanged), so it needs neither; Frame must
    # decrypt + re-encrypt so it needs both.
    needs_source_passphrase: bool = False
    needs_target_passphrase: bool = False
    # Whether the source's per-candidate label is human-
    # meaningful. Brownie keystore filenames usually are (the
    # user chose them); Frame ring-signer IDs are not (hex
    # blobs). When False, the import flow async-fetches an ENS
    # reverse name and overwrites the label with the verified
    # name if one resolves.
    label_is_user_meaningful: bool = True

    @abstractmethod
    def default_dir(self) -> Path: ...

    @abstractmethod
    def discover(self, directory: Path) -> list[ImportCandidate]: ...

    @abstractmethod
    def import_one(
        self, candidate: ImportCandidate, *,
        source_passphrase: str | None = None,
        target_passphrase: str | None = None,
    ) -> tuple[str, dict]:
        """Produce a (checksummed_address, qeth_keystore_dict) pair
        ready for ``hot_wallet.save_keystore``. The caller persists
        and registers the account record."""


# --------------------------------------------------------------------------
# Brownie — direct copy
# --------------------------------------------------------------------------


class BrownieSource(ImportSource):
    """Brownie keystores are Web3 Secret Storage v3 — bit-identical
    to what we generate ourselves. Importing one is just dropping
    the file at ``~/.qeth/keystores/<addr>.json``. The user's
    brownie passphrase carries over unchanged (no decryption
    happens during import; scrypt only runs when they later sign)."""

    name = "Brownie"
    needs_source_passphrase = False
    needs_target_passphrase = False

    def default_dir(self) -> Path:
        return Path.home() / ".brownie" / "accounts"

    def discover(self, directory: Path) -> list[ImportCandidate]:
        out: list[ImportCandidate] = []
        if not directory.exists():
            return out
        for p in sorted(directory.glob("*.json")):
            try:
                data = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError) as e:
                log.debug("skip %s: %s", p, e)
                continue
            if data.get("version") != 3:
                continue
            addr = data.get("address")
            if not addr:
                continue
            if not addr.startswith("0x"):
                addr = "0x" + addr
            try:
                addr_cs = to_checksum_address(addr)
            except Exception as e:
                log.debug("skip %s: bad address %s (%s)", p, addr, e)
                continue
            out.append(ImportCandidate(
                address=addr_cs,
                label=p.stem,
                source_path=p,
                extras={"keystore": data},
            ))
        return out

    def import_one(
        self, candidate: ImportCandidate, *,
        source_passphrase: str | None = None,
        target_passphrase: str | None = None,
    ) -> tuple[str, dict]:
        return candidate.address, candidate.extras["keystore"]


# --------------------------------------------------------------------------
# Frame — decrypt + re-encrypt
# --------------------------------------------------------------------------


# Frame's hot signer KDF + cipher params, lifted verbatim from
# github.com/floating/frame main/signers/hot/HotSigner/worker.js:
# scryptSync(password, salt, 32, { N: 32768, r: 8, p: 1, maxmem: 36000000 })
# then aes-256-cbc with the derived 32-byte key.
_FRAME_SCRYPT_N = 32768
_FRAME_SCRYPT_R = 8
_FRAME_SCRYPT_P = 1
_FRAME_SCRYPT_DKLEN = 32
# 128 * N * r ≈ 33 MB for N=32768/r=8; bump to 64 MB to leave
# headroom for hashlib's bookkeeping (default maxmem is 32 MB and
# would refuse the call).
_FRAME_SCRYPT_MAXMEM = 64 * 1024 * 1024


def _decrypt_frame_ring(
    encrypted_keys: str, passphrase: str,
) -> list[bytes]:
    """Decrypt a Frame ring signer's ``encryptedKeys`` blob and
    return the list of raw 32-byte private keys (one per address
    in the signer's ``addresses`` array, same order).

    Raises ``ValueError`` on a bad format or wrong passphrase.
    'Wrong passphrase' surfaces as PKCS7 padding garbage (random
    last byte) rather than a MAC check — there's no MAC in Frame's
    format, so we can only detect it indirectly."""
    parts = encrypted_keys.split(":")
    if len(parts) != 3:
        raise ValueError(
            "encryptedKeys must be 'salt:iv:ciphertext' (got "
            f"{len(parts)} colon-separated parts)"
        )
    try:
        salt = bytes.fromhex(parts[0])
        iv = bytes.fromhex(parts[1])
        ciphertext = bytes.fromhex(parts[2])
    except ValueError as e:
        raise ValueError(f"non-hex in encryptedKeys: {e}") from e
    if len(salt) != 16:
        raise ValueError(f"salt must be 16 bytes (got {len(salt)})")
    if len(iv) != 16:
        raise ValueError(f"iv must be 16 bytes (got {len(iv)})")
    if len(ciphertext) % 16 != 0 or len(ciphertext) == 0:
        raise ValueError(
            f"ciphertext must be a positive multiple of 16 bytes "
            f"(got {len(ciphertext)})"
        )

    key = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=_FRAME_SCRYPT_N,
        r=_FRAME_SCRYPT_R,
        p=_FRAME_SCRYPT_P,
        dklen=_FRAME_SCRYPT_DKLEN,
        maxmem=_FRAME_SCRYPT_MAXMEM,
    )

    try:
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes,
        )
    except ImportError as e:
        # cryptography is an optional dep (only the Frame-import path uses it).
        # The UI catches this and shows the message — keep it actionable.
        raise ImportError(
            "Importing Frame accounts needs the optional 'cryptography' "
            "package. Install it with:  uv pip install 'qeth[frame]'"
        ) from e
    # AES-CBC (unauthenticated) is Frame's export format, not our choice — we
    # decrypt to match what Frame encrypted with. The missing MAC is benign
    # here: the ciphertext is the user's own local export file, not data from a
    # tampering MITM, and a wrong passphrase fails the PKCS7 unpad below.
    # (Flagged by semgrep crypto-mode-without-authentication; safe in context.)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    # PKCS7 unpad — a wrong passphrase typically produces a last
    # byte > 16 or inconsistent padding bytes; treat both as
    # "wrong passphrase".
    pad_len = padded[-1]
    if pad_len < 1 or pad_len > 16:
        raise ValueError("Wrong passphrase (bad PKCS7 length)")
    if not all(b == pad_len for b in padded[-pad_len:]):
        raise ValueError("Wrong passphrase (bad PKCS7 padding bytes)")
    plain = padded[:-pad_len]

    try:
        text = plain.decode("ascii")
    except UnicodeDecodeError as e:
        raise ValueError(f"Wrong passphrase (plaintext not ASCII): {e}") from e

    keys: list[bytes] = []
    for hex_key in text.split(":"):
        h = hex_key[2:] if hex_key.startswith("0x") else hex_key
        if len(h) != 64:
            raise ValueError(
                "Wrong passphrase (decrypted key is not 64 hex chars: "
                f"got {len(h)})"
            )
        try:
            keys.append(bytes.fromhex(h))
        except ValueError as e:
            raise ValueError(f"Wrong passphrase (non-hex in keys): {e}") from e
    return keys


class FrameSource(ImportSource):
    """Frame stores ring signers under ``~/.config/frame/signers/``.
    A ring signer holds one or more private keys with a single
    shared passphrase. We list each (file, address) pair as a
    candidate; import_one decrypts the whole ring, picks the key
    matching the candidate's address by index, and re-encrypts it
    as a standalone qeth keystore."""

    name = "Frame"
    needs_source_passphrase = True
    needs_target_passphrase = True
    label_is_user_meaningful = False   # hex ID, replace via ENS

    def default_dir(self) -> Path:
        return Path.home() / ".config" / "frame" / "signers"

    def discover(self, directory: Path) -> list[ImportCandidate]:
        out: list[ImportCandidate] = []
        if not directory.exists():
            return out
        for p in sorted(directory.glob("*.json")):
            try:
                data = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError) as e:
                log.debug("skip %s: %s", p, e)
                continue
            # Seed signers and Trezor/Ledger entries also live in
            # this directory but use different formats — filter to
            # ring-type hot signers only.
            if data.get("type") != "ring":
                continue
            addresses = data.get("addresses") or []
            encrypted = data.get("encryptedKeys")
            if not addresses or not isinstance(encrypted, str):
                continue
            for idx, addr in enumerate(addresses):
                if not isinstance(addr, str):
                    continue
                if not addr.startswith("0x"):
                    addr = "0x" + addr
                try:
                    addr_cs = to_checksum_address(addr)
                except Exception as e:
                    log.debug("skip %s addr %s: %s", p, addr, e)
                    continue
                # Frame's file ids are 64-hex-char blobs — useless
                # as a label. Leave empty for single-key signers
                # so the wallet tree just shows the address; the
                # post-import ENS lookup will fill in a verified
                # name when one exists. For multi-key rings we
                # keep the "key N/M" tag so the user can tell the
                # keys apart in the import dialog before picking
                # which ones to import.
                if len(addresses) > 1:
                    label = f"key {idx + 1}/{len(addresses)}"
                else:
                    label = ""
                out.append(ImportCandidate(
                    address=addr_cs,
                    label=label,
                    source_path=p,
                    extras={
                        "encryptedKeys": encrypted,
                        "addresses": list(addresses),
                        "index": idx,
                    },
                ))
        return out

    def import_one(
        self, candidate: ImportCandidate, *,
        source_passphrase: str | None = None,
        target_passphrase: str | None = None,
    ) -> tuple[str, dict]:
        if not source_passphrase:
            raise ValueError("Frame passphrase is required")
        if not target_passphrase:
            raise ValueError("New qeth passphrase is required")
        keys = _decrypt_frame_ring(
            candidate.extras["encryptedKeys"], source_passphrase,
        )
        idx = candidate.extras["index"]
        expected_count = len(candidate.extras["addresses"])
        if len(keys) != expected_count:
            raise ValueError(
                f"Decrypted {len(keys)} keys but signer file lists "
                f"{expected_count} addresses (file is inconsistent)"
            )
        priv = keys[idx]
        from ...hot_wallet import encrypt_keystore
        addr, keystore = encrypt_keystore(priv, target_passphrase)
        # Sanity check: derived address must match what Frame
        # claimed for this index. If it doesn't, the signer file is
        # internally inconsistent (or the user-provided passphrase
        # happened to decrypt to something valid-looking) — refuse
        # rather than persist a mislabeled key.
        if addr.lower() != candidate.address.lower():
            raise ValueError(
                f"Decrypted key derives to {addr}, but Frame listed "
                f"{candidate.address} at index {idx}. Refusing import."
            )
        return addr, keystore


# Registry — exposed for the dialog. Keep Brownie first since it's
# the more common case (and the no-passphrase one).
SOURCES: list[ImportSource] = [BrownieSource(), FrameSource()]
