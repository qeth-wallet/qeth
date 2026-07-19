"""Tests for ``qeth.plugins.wallets.import_sources``.

Brownie discovery is just JSON parsing — fast.

Frame decryption uses scrypt with N=32768 (~30 MB / ~150 ms) which
is fine to run a couple of times per test session. We build Frame
signer files synthetically (encrypt a known private key with the
same scheme Frame uses) rather than depending on a real Frame
install, so the assertions are bit-exact.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from eth_account import Account


PASSPHRASE_SRC = "frame-source-pass"
PASSPHRASE_DST = "qeth-target-pass"

# Same fixed test key used elsewhere — deterministic addresses
# make the cross-import assertions readable.
_TEST_PRIV = bytes.fromhex(
    "b74b6d236486599a9fbbb8d765d0b5a500cc39c6bbe25d60efe701ad6a1ec992"
)
_TEST_ADDR = Account.from_key(_TEST_PRIV).address


def _frame_encrypt(plaintext_keys: list[bytes], passphrase: str) -> str:
    """Re-implement Frame's ring-signer encryption to feed a known
    blob into the decryptor. Mirrors Frame's worker.js exactly so
    any drift between this and ``_decrypt_frame_ring`` would catch
    a regression."""
    import os
    from cryptography.hazmat.primitives.ciphers import (
        Cipher, algorithms, modes,
    )
    salt = os.urandom(16)
    iv = os.urandom(16)
    key = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=32768, r=8, p=1, dklen=32,
        maxmem=64 * 1024 * 1024,
    )
    plaintext = ":".join(k.hex() for k in plaintext_keys).encode("ascii")
    # PKCS7
    pad_len = 16 - (len(plaintext) % 16)
    plaintext += bytes([pad_len]) * pad_len
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    ciphertext = enc.update(plaintext) + enc.finalize()
    return f"{salt.hex()}:{iv.hex()}:{ciphertext.hex()}"


class TestBrownieSource:
    """Brownie keystores are bit-identical to ours — discover lists
    every v3 keystore in the directory, and import_one hands back
    the dict unchanged so the caller can save it as-is."""

    def test_discover_lists_valid_v3_keystores(self, tmp_path):
        from qeth.plugins.wallets.import_sources import BrownieSource
        # Write a real keystore (eth_account format) under a few
        # filenames, plus one malformed file that should be skipped.
        ks = Account.encrypt(_TEST_PRIV, PASSPHRASE_SRC)
        (tmp_path / "alice.json").write_text(json.dumps(ks))
        (tmp_path / "bob.json").write_text(json.dumps(ks))
        (tmp_path / "junk.json").write_text("not json")
        # Non-v3 should be filtered.
        (tmp_path / "v1.json").write_text(json.dumps(
            {"version": 1, "address": "abc"}
        ))

        cands = BrownieSource().discover(tmp_path)
        labels = sorted(c.label for c in cands)
        assert labels == ["alice", "bob"]
        # Address is plaintext in v3 keystores — discover must
        # pick it up without needing the passphrase.
        assert all(c.address == _TEST_ADDR for c in cands)

    def test_import_one_returns_keystore_unchanged(self, tmp_path):
        from qeth.plugins.wallets.import_sources import BrownieSource
        ks = Account.encrypt(_TEST_PRIV, PASSPHRASE_SRC)
        (tmp_path / "alice.json").write_text(json.dumps(ks))
        cand = BrownieSource().discover(tmp_path)[0]
        addr, out_ks = BrownieSource().import_one(cand)
        assert addr == _TEST_ADDR
        # Same dict — caller will save it as-is.
        assert out_ks == ks
        # And the existing brownie passphrase still decrypts it
        # (no re-encryption happened during import).
        assert Account.decrypt(out_ks, PASSPHRASE_SRC) == _TEST_PRIV

    def test_discover_returns_empty_for_missing_dir(self, tmp_path):
        from qeth.plugins.wallets.import_sources import BrownieSource
        assert BrownieSource().discover(tmp_path / "does-not-exist") == []


class TestFrameSource:
    """Frame discovery is plaintext (address list is unencrypted),
    so listing doesn't need the passphrase. Decryption uses
    scrypt+AES-256-CBC; we synthesise a real Frame-format file and
    verify the round-trip + the address-mismatch refusal."""

    def _write_signer(self, dirpath, plaintext_keys, passphrase):
        import os
        sid = os.urandom(32).hex()
        addresses = [Account.from_key(k).address for k in plaintext_keys]
        data = {
            "id": sid,
            "type": "ring",
            "addresses": addresses,
            "encryptedKeys": _frame_encrypt(plaintext_keys, passphrase),
        }
        (dirpath / f"{sid}.json").write_text(json.dumps(data))
        return data

    def test_discover_single_key(self, tmp_path):
        from qeth.plugins.wallets.import_sources import FrameSource
        self._write_signer(tmp_path, [_TEST_PRIV], PASSPHRASE_SRC)
        cands = FrameSource().discover(tmp_path)
        assert len(cands) == 1
        assert cands[0].address == _TEST_ADDR

    def test_discover_multi_key_ring_emits_one_candidate_per_address(
        self, tmp_path,
    ):
        from qeth.plugins.wallets.import_sources import FrameSource
        k2 = bytes(
            b ^ 0x42 for b in _TEST_PRIV
        )  # second deterministic key
        self._write_signer(tmp_path, [_TEST_PRIV, k2], PASSPHRASE_SRC)
        cands = FrameSource().discover(tmp_path)
        assert len(cands) == 2
        addrs = sorted(c.address for c in cands)
        assert addrs == sorted([
            Account.from_key(_TEST_PRIV).address,
            Account.from_key(k2).address,
        ])

    def test_discover_skips_non_ring_types(self, tmp_path):
        from qeth.plugins.wallets.import_sources import FrameSource
        (tmp_path / "seed.json").write_text(json.dumps({
            "id": "x" * 64, "type": "seed",
            "addresses": ["0x" + "0" * 40],
            "encryptedKeys": "00:00:00",
        }))
        assert FrameSource().discover(tmp_path) == []

    def test_import_one_round_trip(self, tmp_path):
        from qeth.plugins.wallets.import_sources import FrameSource
        self._write_signer(tmp_path, [_TEST_PRIV], PASSPHRASE_SRC)
        cand = FrameSource().discover(tmp_path)[0]
        addr, keystore = FrameSource().import_one(
            cand,
            source_passphrase=PASSPHRASE_SRC,
            target_passphrase=PASSPHRASE_DST,
        )
        assert addr == _TEST_ADDR
        # The new keystore opens with the NEW passphrase, not the
        # Frame one — re-encryption actually happened.
        assert Account.decrypt(keystore, PASSPHRASE_DST) == _TEST_PRIV
        with pytest.raises(Exception):
            Account.decrypt(keystore, PASSPHRASE_SRC)

    def test_import_wrong_source_passphrase_raises(self, tmp_path):
        from qeth.plugins.wallets.import_sources import FrameSource
        self._write_signer(tmp_path, [_TEST_PRIV], PASSPHRASE_SRC)
        cand = FrameSource().discover(tmp_path)[0]
        with pytest.raises(ValueError):
            FrameSource().import_one(
                cand,
                source_passphrase="wrong",
                target_passphrase=PASSPHRASE_DST,
            )

    def test_import_missing_passphrases_rejected(self, tmp_path):
        from qeth.plugins.wallets.import_sources import FrameSource
        self._write_signer(tmp_path, [_TEST_PRIV], PASSPHRASE_SRC)
        cand = FrameSource().discover(tmp_path)[0]
        with pytest.raises(ValueError, match="Frame"):
            FrameSource().import_one(
                cand, source_passphrase=None,
                target_passphrase=PASSPHRASE_DST,
            )
        with pytest.raises(ValueError, match="qeth"):
            FrameSource().import_one(
                cand, source_passphrase=PASSPHRASE_SRC,
                target_passphrase=None,
            )

    def test_import_picks_correct_key_in_multi_ring(self, tmp_path):
        from qeth.plugins.wallets.import_sources import FrameSource
        k2 = bytes(b ^ 0x42 for b in _TEST_PRIV)
        addr2 = Account.from_key(k2).address
        self._write_signer(tmp_path, [_TEST_PRIV, k2], PASSPHRASE_SRC)
        cands = FrameSource().discover(tmp_path)
        # Find the candidate for the second key — order in
        # discover() follows the addresses array, so index matters.
        cand2 = next(c for c in cands if c.address == addr2)
        addr, ks = FrameSource().import_one(
            cand2,
            source_passphrase=PASSPHRASE_SRC,
            target_passphrase=PASSPHRASE_DST,
        )
        assert addr == addr2
        # And the decrypted key is k2, not _TEST_PRIV — index was
        # honoured.
        assert Account.decrypt(ks, PASSPHRASE_DST) == k2


def test_frame_import_without_cryptography_is_actionable(monkeypatch):
    """Without the optional cryptography dep, Frame import must fail with an
    actionable 'install qeth[frame]' message — not a raw ImportError."""
    import sys

    from qeth.plugins.wallets import import_sources

    # Skip the ~150ms scrypt (irrelevant to the guard) and simulate the
    # cryptography submodule being unavailable.
    monkeypatch.setattr(import_sources.hashlib, "scrypt",
                        lambda *a, **k: b"\x00" * 32)
    monkeypatch.setitem(
        sys.modules, "cryptography.hazmat.primitives.ciphers", None)
    blob = ":".join(["00" * 16, "00" * 16, "00" * 16])  # salt:iv:ciphertext
    with pytest.raises(ImportError, match=r"qeth\[frame\]"):
        import_sources._decrypt_frame_ring(blob, "passphrase")
