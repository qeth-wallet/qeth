"""QRSigner (step 3b): the full EIP-4527 signing round-trip proven headlessly.

A fake "device" decodes the ur:eth-sign-request, signs its sign-data with a
known key, and returns a ur:eth-signature — so if the unsigned serialization or
the assembly is wrong, Account.recover_transaction won't recover the signer's
address. No camera involved."""

from types import SimpleNamespace

import cbor2
import pytest
from eth_account import Account
from eth_keys import keys
from eth_utils import keccak

from qeth.qr import ur as urmod
from qeth.qr_signer import QRSigner, unsigned_eip1559
from qeth.signing import SignerError, SigningRequest

PRIV = b"\x24" * 32
_PK = keys.PrivateKey(PRIV)
ADDRESS = _PK.public_key.to_checksum_address()
CHAIN = SimpleNamespace(chain_id=1, eip1559=True)


def _req(**over):
    base = dict(
        chain_id=1, from_addr=ADDRESS, to_addr="0x" + "22" * 20,
        value_wei=10**18, data="0x", gas=21000,
        max_fee_per_gas=2 * 10**9, max_priority_fee_per_gas=10**9, nonce=3,
    )
    base.update(over)
    return SigningRequest(**base)


def _account():
    return {"address": ADDRESS, "path": "m/44'/60'/0'/0/0",
            "xfp": 0x12345678, "source": "qr"}


class _FakeDevice:
    """Plays the air-gapped wallet: reads our request QR, signs, shows the
    signature QR. ``tamper_request_id`` simulates scanning the wrong response."""

    def __init__(self, *, cancel=False, tamper_request_id=False):
        self.cancel = cancel
        self.tamper = tamper_request_id
        self.seen_request = None

    def exchange_qr(self, request_parts):
        self.seen_request = request_parts
        if self.cancel:
            return None
        # Reassemble the (possibly multi-part / animated) request, like a real
        # device's BC-UR decoder.
        from qeth.qr.multipart import decode_parts
        _, payload = decode_parts(request_parts)
        body = cbor2.loads(payload)
        sign_data = body[2]
        request_id = body[1].bytes if not self.tamper else b"\x00" * 16
        sig = _PK.sign_msg_hash(keccak(sign_data))       # v is 0/1
        sig_bytes = (sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big")
                     + bytes([sig.v]))
        resp = {1: cbor2.CBORTag(37, request_id), 2: sig_bytes}
        return urmod.encode("eth-signature", cbor2.dumps(resp, canonical=True))


def test_unsigned_serialization_matches_eth_account():
    # Our hand-rolled 0x02||rlp(9) must hash identically to eth-account's.
    from eth_account.typed_transactions import TypedTransaction
    req = _req()
    td = TypedTransaction.from_dict({
        "type": 2, "chainId": 1, "nonce": req.nonce,
        "maxPriorityFeePerGas": req.max_priority_fee_per_gas,
        "maxFeePerGas": req.max_fee_per_gas, "gas": req.gas,
        "to": req.to_addr, "value": req.value_wei, "data": b"", "accessList": [],
    })
    assert keccak(unsigned_eip1559(req, 1)) == td.hash()


def test_sign_roundtrip_recovers_the_signer():
    device = _FakeDevice()
    signer = QRSigner(_account(), device)
    raw = signer.sign(_req(), CHAIN)
    raw_hex = "0x" + raw.hex()
    # The assembled raw tx must recover to the key that signed the sign-data.
    assert Account.recover_transaction(raw_hex) == ADDRESS
    # …and a small tx is shown as a single eth-sign-request part.
    assert len(device.seen_request) == 1
    assert device.seen_request[0].startswith("ur:eth-sign-request/")


def test_sign_roundtrip_with_data_and_contract_creation():
    device = _FakeDevice()
    signer = QRSigner(_account(), device)
    raw = signer.sign(_req(to_addr=None, value_wei=0, data="0xabcdef"), CHAIN)
    assert Account.recover_transaction("0x" + raw.hex()) == ADDRESS


def test_sign_large_calldata_animates_and_still_recovers():
    """The Curve-swap case: a big calldata tx must span several animated QR
    parts (not one giant QR that hangs the device) and still assemble."""
    device = _FakeDevice()
    signer = QRSigner(_account(), device)
    big_calldata = "0x" + "ab" * 400          # ~400 bytes → several UR parts
    raw = signer.sign(_req(data=big_calldata), CHAIN)
    assert len(device.seen_request) > 1        # animated, multiple parts
    assert all(p.startswith("ur:eth-sign-request/") for p in device.seen_request)
    assert Account.recover_transaction("0x" + raw.hex()) == ADDRESS


def test_cancel_raises():
    signer = QRSigner(_account(), _FakeDevice(cancel=True))
    with pytest.raises(SignerError, match="cancelled"):
        signer.sign(_req(), CHAIN)


def test_request_id_mismatch_raises():
    signer = QRSigner(_account(), _FakeDevice(tamper_request_id=True))
    with pytest.raises(SignerError, match="different request"):
        signer.sign(_req(), CHAIN)


def test_non_eip1559_chain_rejected():
    signer = QRSigner(_account(), _FakeDevice())
    with pytest.raises(SignerError, match="EIP-1559"):
        signer.sign(_req(), SimpleNamespace(chain_id=1, eip1559=False))


def test_can_sign_matches_only_its_account():
    signer = QRSigner(_account(), _FakeDevice())
    assert signer.can_sign(ADDRESS) is True
    assert signer.can_sign(ADDRESS.lower()) is True
    assert signer.can_sign("0x" + "99" * 20) is False


def test_registry_exposes_the_qr_plugin():
    from qeth.qr_signer import QRSigner as _QRS
    from qeth.signers import signer_for_source
    plugin = signer_for_source("qr")
    assert plugin is not None
    assert plugin.can_sign() is True
    assert plugin.progress_text == ""        # no spinner — drives its own window
    signer = plugin.make_signer(SimpleNamespace(), _account(), _FakeDevice())
    assert isinstance(signer, _QRS)
