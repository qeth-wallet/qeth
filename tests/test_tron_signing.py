"""Tron signing: the hot wallet, the Trezor path (a fake device that signs
like the firmware — over its OWN re-encoding of the fields it's sent) and the
sign-and-broadcast worker. Every signature is real and checked by recovery."""

from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace

import pytest
from eth_keys import keys
from eth_keys.backends.native.ecdsa import N

from qeth.address import tron_from_hex
from qeth.chains import TRON, Chain
from qeth.signing import SignerError, TronSignAndBroadcastWorker, TronSigningRequest
from qeth.tron.client import BlockRef, TronError, TronTransportError
from qeth.tron.fees import EXPIRATION_MS
from qeth.tron.tx import (
    TransferContract, TriggerSmartContract, TronTx, decode_raw, read_fields,
    recover_signer,
)

TRON_CHAIN = Chain("Tron", 728126428, "", "TRX", family=TRON, native_decimals=6,
                   api_url="https://tron.invalid")
TO = "0x" + "cd" * 20
HEAD = BlockRef(86533542, "00000000052865a657be05bc4f1ceb00bda7bbd12a2b02ab4a1221ada11df470",
                1790272989000)
SOLID = BlockRef(86533524, "0000000005286594f5d4c2b3f41513562895cf744763883c9ba6afefc1cf5174",
                 1790272935000)


def _addr_of(priv: bytes) -> str:
    return "0x" + keys.PrivateKey(priv).public_key.to_canonical_address().hex()


def _tx(owner: str, contract=None) -> TronTx:
    return TronTx(contract or TransferContract(owner, TO, 1_500_000),
                  bytes.fromhex("6594"), bytes.fromhex("f5d4c2b3f4151356"),
                  expiration=HEAD.timestamp + EXPIRATION_MS, timestamp=HEAD.timestamp)


# --- hot wallet -------------------------------------------------------------------

def test_hot_wallet_signs_the_txid(tmp_qeth):
    from qeth.hot_wallet import HotWalletSigner
    priv = b"\x07" * 32
    owner = _addr_of(priv)
    store = SimpleNamespace(accounts=[{"address": owner, "source": "hot"}])
    signer = HotWalletSigner(store, unlocked=(owner, priv))
    from qeth import hot_wallet
    hot_wallet.KEYSTORE_DIR.mkdir(parents=True, exist_ok=True)
    hot_wallet.keystore_path(owner).write_text("{}")     # can_sign checks it exists
    tx = _tx(owner)
    sig = signer.sign_tron(TronSigningRequest(TRON_CHAIN.chain_id, tx))
    assert len(sig) == 65 and sig[64] in (27, 28)
    assert recover_signer(tx.txid(), sig) == owner


def test_signers_without_tron_support_refuse():
    from qeth.ledger import LedgerSigner
    with pytest.raises(SignerError, match="can't sign Tron"):
        LedgerSigner(SimpleNamespace(accounts=[])).sign_tron(
            TronSigningRequest(1, _tx("0x" + "11" * 20)))


# --- Trezor -------------------------------------------------------------------------

pytest.importorskip("trezorlib")
import qeth.trezor as trezor_mod  # noqa: E402

HARDENED = 1 << 31
SEED = hashlib.sha256(b"qeth-trezor-tron-seed").digest()
PATH = "44'/195'/0'/0/0"


def _derive(n: list[int]) -> bytes:
    i = hmac.new(b"Bitcoin seed", SEED, hashlib.sha512).digest()
    priv, cc = i[:32], i[32:]
    for index in n:
        if index & HARDENED:
            data = b"\0" + priv + index.to_bytes(4, "big")
        else:
            data = (keys.PrivateKey(priv).public_key.to_compressed_bytes()
                    + index.to_bytes(4, "big"))
        i = hmac.new(cc, data, hashlib.sha512).digest()
        priv = ((int.from_bytes(i[:32], "big") + int.from_bytes(priv, "big"))
                % N).to_bytes(32, "big")
        cc = i[32:]
    return priv


class FakeTron:
    """``trezorlib.tron`` with a real key tree. ``sign_tx`` signs what the
    FIRMWARE would: sha256 of its own re-encoding of ``tx`` + ``contract``."""

    def __init__(self) -> None:
        from trezorlib import tron
        self.from_raw_data = tron.from_raw_data
        self.calls: list[str] = []

    def get_address(self, session, n):
        self.calls.append("get_address")
        return tron_from_hex(_addr_of(_derive(n)))

    def sign_tx(self, session, tx, contract, n):
        self.calls.append("sign_tx")
        signed_bytes = trezor_mod._trezor_tron_encoding(tx, contract)
        sig = keys.PrivateKey(_derive(n)).sign_msg_hash(
            hashlib.sha256(signed_bytes).digest()).to_bytes()
        return SimpleNamespace(signature=sig[:64] + bytes([sig[64] + 27]))


class FakeSession:
    def __init__(self, model="Safe 3", version=(2, 12, 5)) -> None:
        major, minor, patch = version
        self.client = SimpleNamespace(features=SimpleNamespace(
            model=model, passphrase_protection=False, major_version=major,
            minor_version=minor, patch_version=patch))

    def get_root_fingerprint(self) -> bytes:
        return bytes.fromhex("9bd26194")

    def close(self) -> None:
        pass


@pytest.fixture
def device(monkeypatch):
    fake = FakeTron()
    fake.session = FakeSession()
    monkeypatch.setattr(trezor_mod, "_tron", lambda: fake)
    monkeypatch.setattr(trezor_mod, "_open_session", lambda conn: fake.session)
    trezor_mod._CONNECTION.reset()
    yield fake
    trezor_mod._CONNECTION.reset()


def _trezor_owner() -> str:
    return _addr_of(_derive(trezor_mod._address_n(PATH)))


def _trezor_signer(owner: str | None = None):
    return trezor_mod.TrezorSigner({"address": owner or _trezor_owner(),
                                    "path": PATH, "source": "trezor",
                                    "family": TRON})


def test_trezor_signs_a_trx_transfer(device):
    owner = _trezor_owner()
    tx = _tx(owner)
    sig = _trezor_signer().sign_tron(TronSigningRequest(TRON_CHAIN.chain_id, tx))
    assert recover_signer(tx.txid(), sig) == owner
    assert device.calls == ["get_address", "sign_tx"]


def test_trezor_signs_a_trc20_call(device):
    from qeth.tron.fees import trc20_transfer_data
    owner = _trezor_owner()
    tx = _tx(owner, TriggerSmartContract(owner, TO, trc20_transfer_data(TO, 10**6)))
    tx = TronTx(tx.contract, tx.ref_block_bytes, tx.ref_block_hash,
                tx.expiration, tx.timestamp, fee_limit=20_000_000)
    sig = _trezor_signer().sign_tron(TronSigningRequest(TRON_CHAIN.chain_id, tx))
    assert recover_signer(tx.txid(), sig) == owner


def test_trezor_refuses_a_field_its_firmware_would_drop(device, monkeypatch):
    """If the device's re-encoding differs from our bytes (a field it doesn't
    model), the device must not be asked to sign — its signature would be
    over a different transaction."""
    owner = _trezor_owner()
    monkeypatch.setattr(trezor_mod, "_trezor_tron_encoding",
                        lambda tx, contract: b"something else")
    with pytest.raises(SignerError, match="exactly as built"):
        _trezor_signer().sign_tron(TronSigningRequest(1, _tx(owner)))
    assert "sign_tx" not in device.calls


def test_trezor_refuses_another_wallets_address(device):
    other = "0x" + "ee" * 20
    with pytest.raises(SignerError, match="doesn't hold"):
        _trezor_signer(other).sign_tron(TronSigningRequest(1, _tx(other)))


@pytest.mark.parametrize("model, version, match", [
    ("1", (1, 12, 1), "Trezor One"),
    ("T", (2, 10, 0), "2.11.0 or newer"),
])
def test_trezor_without_tron_support_explains(device, model, version, match):
    device.session = FakeSession(model, version)
    owner = _trezor_owner()
    with pytest.raises(SignerError, match=match):
        _trezor_signer().sign_tron(TronSigningRequest(1, _tx(owner)))


def test_trezor_tron_discovery_reads_each_address(qtbot, device):
    found = []
    worker = trezor_mod.TrezorWorker("Tron", 3, chain=None)
    worker.discovered.connect(found.append)
    worker.run()
    assert [d.path for d in found] == [f"44'/195'/0'/0/{i}" for i in range(3)]
    assert found[0].address.lower() == _trezor_owner()
    assert all(d.family == TRON for d in found)


# --- sign + broadcast worker ---------------------------------------------------------

class FakeClient:
    instances: list[FakeClient] = []

    def __init__(self, chain) -> None:
        self.broadcasted: list[bytes] = []
        self.fail: Exception | None = None
        FakeClient.instances.append(self)

    def solid_block(self):
        return SOLID

    def head_block(self):
        return HEAD

    def broadcast(self, signed: bytes) -> str:
        if self.fail is not None:
            raise self.fail
        self.broadcasted.append(signed)
        return "ok"


class _KeySigner:
    def __init__(self, priv: bytes) -> None:
        self._priv = priv

    def sign_tron(self, req):
        sig = keys.PrivateKey(self._priv).sign_msg_hash(req.tx.txid()).to_bytes()
        return sig[:64] + bytes([sig[64] + 27])


def _run_worker(monkeypatch, signer, contract, fail=None):
    import qeth.signing as signing
    FakeClient.instances.clear()

    class Client(FakeClient):
        def __init__(self, chain):
            super().__init__(chain)
            self.fail = fail
    monkeypatch.setattr(signing, "TronClient", Client)
    w = TronSignAndBroadcastWorker(signer, contract, TRON_CHAIN, fee_limit=0)
    out: dict = {}
    w.broadcast.connect(lambda h, raw, ok: out.update(hash=h, raw=raw, ok=ok))
    w.failed.connect(lambda m: out.update(failed=m))
    w.built.connect(lambda req: out.update(req=req))
    w.run()
    return out, FakeClient.instances[0]


def test_worker_builds_fresh_tapos_signs_and_broadcasts(monkeypatch):
    priv = b"\x05" * 32
    owner = _addr_of(priv)
    out, client = _run_worker(monkeypatch, _KeySigner(priv),
                              TransferContract(owner, TO, 42))
    tx = out["req"].tx
    # TaPoS references the SOLID block; expiration runs from HEAD's time.
    assert tx.ref_block_hash == bytes.fromhex(SOLID.block_id)[8:16]
    assert tx.expiration == HEAD.timestamp + EXPIRATION_MS
    assert out["ok"] is True and out["hash"] == "0x" + tx.txid().hex()
    fields = read_fields(client.broadcasted[0])
    assert decode_raw(fields[0][2]) == tx     # broadcast exactly what was signed


def test_worker_refuses_a_signature_from_another_key(monkeypatch):
    owner = _addr_of(b"\x05" * 32)
    out, client = _run_worker(monkeypatch, _KeySigner(b"\x06" * 32),
                              TransferContract(owner, TO, 42))
    assert "doesn't match" in out["failed"]
    assert client.broadcasted == []


def test_worker_keeps_a_tx_whose_broadcast_didnt_reach_a_node(monkeypatch):
    priv = b"\x05" * 32
    out, _ = _run_worker(monkeypatch, _KeySigner(priv),
                         TransferContract(_addr_of(priv), TO, 42),
                         fail=TronTransportError("down"))
    assert out["ok"] is False and out["raw"].startswith("0x0a")


def test_worker_reports_a_node_rejection(monkeypatch):
    priv = b"\x05" * 32
    out, _ = _run_worker(monkeypatch, _KeySigner(priv),
                         TransferContract(_addr_of(priv), TO, 42),
                         fail=TronError("balance is not sufficient"))
    assert out["failed"] == "Broadcast failed: balance is not sufficient"
