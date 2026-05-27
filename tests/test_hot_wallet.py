"""Tests for the hot-wallet keystore + signer.

Generates real keystores (small scrypt parameters via the
``eth_account`` defaults — still slow at ~1s/round, so we keep
encryption calls per test minimal).
"""

import json

import pytest

from qeth.hot_wallet import (
    KEYSTORE_DIR,
    HotWalletSigner,
    delete_keystore,
    encrypt_keystore,
    keystore_path,
    load_keystore,
    save_keystore,
)
from qeth.signing import SignerError, SigningRequest


PASSPHRASE = "correct horse battery staple"

# A fixed private key for tests so the address derivation is
# deterministic — generated once via os.urandom and pasted in.
_TEST_PRIV = bytes.fromhex(
    "b74b6d236486599a9fbbb8d765d0b5a500cc39c6bbe25d60efe701ad6a1ec992"
)


def _fake_store(*accounts):
    class _S:
        pass
    s = _S()
    s.accounts = list(accounts)
    return s


class TestParsePrivateKeyHex:
    """The dialog accepts either bare hex or 0x-prefixed; everything
    else is rejected up front so the user gets a clear message
    before we try to derive an address."""

    def test_64_chars_no_prefix(self):
        from qeth.hot_wallet import parse_private_key_hex
        priv = parse_private_key_hex("a" * 64)
        assert priv == bytes([0xaa]) * 32

    def test_0x_prefix_accepted(self):
        from qeth.hot_wallet import parse_private_key_hex
        priv = parse_private_key_hex("0x" + "a" * 64)
        assert priv == bytes([0xaa]) * 32

    def test_whitespace_stripped(self):
        from qeth.hot_wallet import parse_private_key_hex
        priv = parse_private_key_hex("   " + "a" * 64 + "\n")
        assert priv == bytes([0xaa]) * 32

    def test_wrong_length_raises(self):
        from qeth.hot_wallet import parse_private_key_hex
        with pytest.raises(SignerError, match="64 hex"):
            parse_private_key_hex("a" * 63)

    def test_non_hex_raises(self):
        from qeth.hot_wallet import parse_private_key_hex
        with pytest.raises(SignerError, match="Invalid hex"):
            parse_private_key_hex("z" * 64)


class TestGenerateRandomPrivateKey:
    def test_returns_32_bytes(self):
        from qeth.hot_wallet import generate_random_private_key
        priv = generate_random_private_key()
        assert isinstance(priv, bytes)
        assert len(priv) == 32

    def test_each_call_returns_different_bytes(self):
        """Sanity check on the CSPRNG plumbing — two consecutive
        calls must not collide. (Probability of collision is
        2**-256; if this ever fails, we'd have bigger problems.)"""
        from qeth.hot_wallet import generate_random_private_key
        assert generate_random_private_key() != generate_random_private_key()


class TestEncryptKeystoreLengthCheck:
    def test_rejects_non_32_byte_key(self):
        from qeth.hot_wallet import encrypt_keystore
        with pytest.raises(SignerError, match="32 bytes"):
            encrypt_keystore(b"\x01" * 31, PASSPHRASE)


class TestKeystoreFile:
    """Round-trip the keystore on disk: generate → save → load →
    decrypt → recovered key matches."""

    def test_generate_save_load_round_trip(self, tmp_qeth):
        from qeth import hot_wallet as _hw
        addr, ks = encrypt_keystore(_TEST_PRIV, PASSPHRASE)
        save_keystore(addr, ks)
        loaded = load_keystore(addr)
        assert loaded == ks
        # File lives where keystore_path says it does.
        assert keystore_path(addr).exists()
        assert keystore_path(addr).parent == _hw.KEYSTORE_DIR

    def test_save_refuses_to_clobber_existing(self, tmp_qeth):
        addr, ks = encrypt_keystore(_TEST_PRIV, PASSPHRASE)
        save_keystore(addr, ks)
        with pytest.raises(SignerError, match="already exists"):
            save_keystore(addr, ks)

    def test_load_missing_keystore_raises(self, tmp_qeth):
        with pytest.raises(SignerError, match="No keystore"):
            load_keystore("0x" + "ee" * 20)

    def test_delete_keystore_returns_true_when_present(self, tmp_qeth):
        addr, ks = encrypt_keystore(_TEST_PRIV, PASSPHRASE)
        save_keystore(addr, ks)
        assert delete_keystore(addr) is True
        assert not keystore_path(addr).exists()

    def test_delete_keystore_returns_false_when_absent(self, tmp_qeth):
        assert delete_keystore("0x" + "ee" * 20) is False


class TestHotWalletSigner:
    def test_can_sign_for_persisted_keystore(self, tmp_qeth):
        addr, ks = encrypt_keystore(_TEST_PRIV, PASSPHRASE)
        save_keystore(addr, ks)
        signer = HotWalletSigner(
            _fake_store({"address": addr, "source": "hot", "label": ""}),
            PASSPHRASE,
        )
        assert signer.can_sign(addr)
        # Case-insensitive.
        assert signer.can_sign(addr.lower())

    def test_can_sign_false_for_unknown_address(self, tmp_qeth):
        signer = HotWalletSigner(_fake_store(), PASSPHRASE)
        assert not signer.can_sign("0x" + "ee" * 20)

    def test_can_sign_false_when_keystore_missing_on_disk(self, tmp_qeth):
        """Account record present but keystore file gone (manual
        deletion / corrupted state) — can_sign refuses."""
        addr = "0x7a16fF8270133F063aAb6C9977183D9e72835428"
        signer = HotWalletSigner(
            _fake_store({"address": addr, "source": "hot", "label": ""}),
            PASSPHRASE,
        )
        assert not signer.can_sign(addr)

    def test_sign_raises_with_wrong_passphrase(self, tmp_qeth):
        addr, ks = encrypt_keystore(_TEST_PRIV, PASSPHRASE)
        save_keystore(addr, ks)
        signer = HotWalletSigner(
            _fake_store({"address": addr, "source": "hot", "label": ""}),
            "wrong " + PASSPHRASE,
        )
        req = SigningRequest(
            chain_id=1, from_addr=addr, to_addr="0x" + "ff" * 20,
            value_wei=0, data="0x",
            gas=21000, nonce=0,
            max_fee_per_gas=10**9, max_priority_fee_per_gas=10**8,
        )
        from qeth.chains import DEFAULT_CHAINS
        with pytest.raises(SignerError, match="passphrase"):
            signer.sign(req, DEFAULT_CHAINS[0])

    def test_sign_message_round_trips_via_recover(self, tmp_qeth):
        # personal_sign: sign a plain UTF-8 message, then use
        # eth_account.Account.recover_message to verify the
        # signature came from our address.
        from qeth.signing import MessageSigningRequest
        from eth_account import Account
        from eth_account.messages import encode_defunct
        addr, ks = encrypt_keystore(_TEST_PRIV, PASSPHRASE)
        save_keystore(addr, ks)
        signer = HotWalletSigner(
            _fake_store({"address": addr, "source": "hot", "label": ""}),
            PASSPHRASE,
        )
        msg = b"Welcome to qeth!"
        sig = signer.sign_message(
            MessageSigningRequest(from_addr=addr, raw=msg),
        )
        assert isinstance(sig, bytes) and len(sig) == 65
        signable = encode_defunct(primitive=msg)
        recovered = Account.recover_message(signable, signature=sig)
        assert recovered.lower() == addr.lower()

    def test_sign_typed_data_round_trips_via_recover(self, tmp_qeth):
        # eth_signTypedData_v4: a minimal EIP-712 message (the
        # canonical Mail example from the spec). recover_message
        # with the same structured data must return our address.
        from qeth.signing import TypedDataSigningRequest
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        addr, ks = encrypt_keystore(_TEST_PRIV, PASSPHRASE)
        save_keystore(addr, ks)
        signer = HotWalletSigner(
            _fake_store({"address": addr, "source": "hot", "label": ""}),
            PASSPHRASE,
        )
        typed = {
            "domain": {
                "name": "qeth-test",
                "version": "1",
                "chainId": 1,
                "verifyingContract": "0x" + "00" * 20,
            },
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Greeting": [
                    {"name": "to", "type": "string"},
                    {"name": "body", "type": "string"},
                ],
            },
            "primaryType": "Greeting",
            "message": {"to": "World", "body": "hello"},
        }
        sig = signer.sign_typed_data(
            TypedDataSigningRequest(from_addr=addr, typed_data=typed),
        )
        assert isinstance(sig, bytes) and len(sig) == 65
        signable = encode_typed_data(full_message=typed)
        recovered = Account.recover_message(signable, signature=sig)
        assert recovered.lower() == addr.lower()

    def test_sign_produces_a_valid_signed_eip1559_tx(self, tmp_qeth):
        """End-to-end: generate, sign, then have eth_account
        round-trip the raw bytes back to a Transaction to confirm
        the signed payload is well-formed (type, chain, from)."""
        addr, ks = encrypt_keystore(_TEST_PRIV, PASSPHRASE)
        save_keystore(addr, ks)
        signer = HotWalletSigner(
            _fake_store({"address": addr, "source": "hot", "label": ""}),
            PASSPHRASE,
        )
        req = SigningRequest(
            chain_id=1, from_addr=addr, to_addr="0x" + "11" * 20,
            value_wei=0, data="0xa9059cbb",
            gas=21000, nonce=7,
            max_fee_per_gas=10 * 10**9,
            max_priority_fee_per_gas=1 * 10**9,
        )
        from qeth.chains import DEFAULT_CHAINS
        raw = signer.sign(req, DEFAULT_CHAINS[0])
        assert isinstance(raw, (bytes, bytearray))
        assert raw[:1] == b"\x02"  # type-2 (EIP-1559) envelope byte
        # Recover the sender and confirm it's our address.
        from eth_account import Account
        rebuilt = Account.recover_transaction(raw)
        assert rebuilt.lower() == addr.lower()
