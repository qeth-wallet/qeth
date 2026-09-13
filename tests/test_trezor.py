"""Trezor signer + discovery (qeth/trezor.py), hermetic.

trezorlib's Ethereum calls are replaced by a fake device holding a real BIP32
key tree, so every signature it returns is genuine: a test proves the raw tx /
message qeth assembles from the device's ``(v, r, s)`` recovers to the right
address, rather than asserting on bytes. The device thread, the session cache
and the reconnect-on-stale-session logic are the real ones.
"""

from __future__ import annotations

import hashlib
import hmac
from types import SimpleNamespace

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from eth_account.typed_transactions import TypedTransaction
from eth_keys import keys
from eth_keys.backends.native.ecdsa import N
from hexbytes import HexBytes

pytest.importorskip("trezorlib")

from trezorlib import exceptions, messages  # noqa: E402
from trezorlib.transport import TransportException  # noqa: E402

import qeth.trezor as trezor_mod  # noqa: E402
from qeth.signing import (  # noqa: E402
    MessageSigningRequest, SignerError, SigningRequest, TypedDataSigningRequest,
)
from qeth.trezor import TrezorSigner, TrezorWorker  # noqa: E402

HARDENED = 1 << 31
SEED = hashlib.sha256(b"qeth-trezor-test-seed").digest()
TO = "0x" + "cd" * 20        # lowercased, as qeth stores addresses


def _derive(n: list[int]) -> tuple[bytes, bytes]:
    """Reference BIP32 private derivation (hardened + soft) → (priv, chain code)."""
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
    return priv, cc


def _address(n: list[int]) -> str:
    return Account.from_key(_derive(n)[0]).address


class FakeEthereum:
    """Stands in for ``trezorlib.ethereum``: same call shapes, real signatures."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_next: BaseException | None = None
        self.message_v_as_recovery_id = False

    def _record(self, name: str, **kw) -> None:
        self.calls.append((name, kw))
        if self.fail_next is not None:
            e, self.fail_next = self.fail_next, None
            raise e

    def get_address(self, session, n):
        self._record("get_address", n=n)
        return _address(n)

    def get_public_node(self, session, n):
        self._record("get_public_node", n=n)
        priv, cc = _derive(n)
        pub = keys.PrivateKey(priv).public_key.to_compressed_bytes()
        return SimpleNamespace(node=SimpleNamespace(public_key=pub, chain_code=cc))

    def sign_tx_eip1559(self, session, n, **kw):
        self._record("sign_tx_eip1559", **kw)
        signed = Account.sign_transaction({
            "type": 2, "chainId": kw["chain_id"], "nonce": kw["nonce"],
            "gas": kw["gas_limit"], "to": kw["to"], "value": kw["value"],
            "data": kw["data"], "maxFeePerGas": kw["max_gas_fee"],
            "maxPriorityFeePerGas": kw["max_priority_fee"], "accessList": [],
        }, _derive(n)[0])
        return signed.v, signed.r.to_bytes(32, "big"), signed.s.to_bytes(32, "big")

    def sign_tx(self, session, n, **kw):
        self._record("sign_tx", **kw)
        signed = Account.sign_transaction({
            "chainId": kw["chain_id"], "nonce": kw["nonce"],
            "gas": kw["gas_limit"], "gasPrice": kw["gas_price"], "to": kw["to"],
            "value": kw["value"], "data": kw["data"],
        }, _derive(n)[0])
        return signed.v, signed.r.to_bytes(32, "big"), signed.s.to_bytes(32, "big")

    def sign_message(self, session, n, message):
        self._record("sign_message", message=message)
        sig = bytes(Account.sign_message(
            encode_defunct(primitive=message), _derive(n)[0]).signature)
        if self.message_v_as_recovery_id:
            sig = sig[:64] + bytes([sig[64] - 27])
        return SimpleNamespace(signature=sig)

    def sign_typed_data(self, session, n, data, *, metamask_v4_compat):
        self._record("sign_typed_data", metamask_v4_compat=metamask_v4_compat)
        return SimpleNamespace(signature=bytes(
            Account.sign_typed_data(_derive(n)[0], full_message=data).signature))

    def sign_typed_data_hash(self, session, n, domain_hash, message_hash):
        self._record("sign_typed_data_hash")
        signable = SimpleNamespace(version=b"\x01", header=domain_hash, body=message_hash)
        return SimpleNamespace(signature=bytes(
            Account.sign_message(signable, _derive(n)[0]).signature))


class FakeSession:
    def __init__(self, model: str = "Safe 3", passphrase: bool = False) -> None:
        self.client = SimpleNamespace(features=SimpleNamespace(
            model=model, passphrase_protection=passphrase))
        self.closed = False

    def get_root_fingerprint(self) -> bytes:
        return bytes.fromhex("9bd26194")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def device(monkeypatch):
    """A connected fake Trezor. Yields the fake ``ethereum`` module; ``opened``
    counts sessions opened (a reconnect opens a second)."""
    eth = FakeEthereum()
    eth.opened = 0
    eth.model = "Safe 3"
    eth.passphrase = False
    eth.sessions = []

    def open_session(conn):
        eth.opened += 1
        eth.sessions.append(FakeSession(eth.model, eth.passphrase))
        return eth.sessions[-1]

    monkeypatch.setattr(trezor_mod, "_ethereum", lambda: eth)
    monkeypatch.setattr(trezor_mod, "_open_session", open_session)
    monkeypatch.setattr(trezor_mod, "_fetch_definition", lambda url: None)
    trezor_mod._CONNECTION.reset()
    yield eth
    trezor_mod._CONNECTION.reset()


PATH = "44'/60'/0'/0/1"
N_PATH = [44 | HARDENED, 60 | HARDENED, HARDENED, 0, 1]


def _account(address: str | None = None) -> dict:
    return {"address": address or _address(N_PATH), "path": PATH,
            "source": "trezor", "label": ""}


def _req(**kw) -> SigningRequest:
    base = dict(chain_id=1, from_addr=_address(N_PATH), to_addr=TO,
                value_wei=10**15, data="0x", gas=21_000, nonce=7,
                max_fee_per_gas=2 * 10**9, max_priority_fee_per_gas=10**9)
    base.update(kw)
    return SigningRequest(**base)


def _chain(chain_id: int = 1, eip1559: bool = True):
    return SimpleNamespace(chain_id=chain_id, eip1559=eip1559)


# --- transactions -------------------------------------------------------------


def test_eip1559_tx_recovers_to_the_account(device):
    raw = TrezorSigner(_account()).sign(_req(data="0xa9059cbb"), _chain())

    assert Account.recover_transaction(raw) == _address(N_PATH)
    tx = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
    assert (tx["chainId"], tx["nonce"], tx["value"]) == (1, 7, 10**15)
    assert "0x" + bytes(tx["to"]).hex() == TO
    # Checked the device holds the address first, then signed — asking for
    # the chain/token descriptions so the device can name them.
    names = [c[0] for c in device.calls]
    assert names == ["get_address", "sign_tx_eip1559"]
    sign_kw = device.calls[1][1]
    assert sign_kw["supports_definition_request"] is True
    assert sign_kw["definition_source"] is not None


def test_legacy_tx_recovers_with_eip155_v(device):
    req = _req(chain_id=56, max_fee_per_gas=None, max_priority_fee_per_gas=None,
               gas_price=3 * 10**9)
    raw = TrezorSigner(_account()).sign(req, _chain(56, eip1559=False))

    assert raw[0] >= 0xC0                              # legacy RLP list, no type byte
    assert Account.recover_transaction(raw) == _address(N_PATH)
    assert [c[0] for c in device.calls] == ["get_address", "sign_tx"]


def test_contract_creation_signs_with_empty_to(device):
    raw = TrezorSigner(_account()).sign(_req(to_addr=None, data="0x6080"), _chain())
    assert device.calls[1][1]["to"] == ""
    assert Account.recover_transaction(raw) == _address(N_PATH)


def test_refuses_when_the_device_holds_a_different_address(device):
    other = "0x" + "ee" * 20
    with pytest.raises(SignerError, match="doesn't hold"):
        TrezorSigner(_account(other)).sign(_req(from_addr=other), _chain())
    assert [c[0] for c in device.calls] == ["get_address"]   # never signed


def test_wrong_passphrase_wallet_ends_the_session_so_a_retry_asks_again(device):
    """A typo'd passphrase opens a different wallet: the holds-check refuses,
    and must END that session — else every retry reuses the wrong wallet until
    the device is re-plugged."""
    device.passphrase = True
    other = "0x" + "ee" * 20
    with pytest.raises(SignerError, match="enter that passphrase"):
        TrezorSigner(_account(other)).sign(_req(from_addr=other), _chain())
    assert device.sessions[0].closed

    raw = TrezorSigner(_account()).sign(_req(), _chain())    # the retry
    assert device.opened == 2                  # a new session → passphrase re-asked
    assert Account.recover_transaction(raw) == _address(N_PATH)


def test_missing_fees_or_nonce_fail_before_touching_the_device(device):
    with pytest.raises(SignerError, match="gas and nonce"):
        TrezorSigner(_account()).sign(_req(nonce=None), _chain())
    with pytest.raises(SignerError, match="EIP-1559 fees"):
        TrezorSigner(_account()).sign(_req(max_fee_per_gas=None), _chain())
    assert device.calls == []


# --- messages -------------------------------------------------------------------


def test_personal_sign_normalises_v_and_recovers(device):
    device.message_v_as_recovery_id = True             # device returns v as 0/1
    msg = b"hello trezor"
    sig = TrezorSigner(_account()).sign_message(
        MessageSigningRequest(from_addr=_address(N_PATH), raw=msg))

    assert len(sig) == 65 and sig[64] in (27, 28)
    assert Account.recover_message(
        encode_defunct(primitive=msg), signature=sig) == _address(N_PATH)


TYPED = {
    "types": {
        "EIP712Domain": [{"name": "name", "type": "string"},
                         {"name": "chainId", "type": "uint256"}],
        "Mail": [{"name": "contents", "type": "string"}],
    },
    "primaryType": "Mail",
    "domain": {"name": "qeth", "chainId": 1},
    "message": {"contents": "hi"},
}


@pytest.mark.parametrize("model,call", [
    ("Safe 3", "sign_typed_data"),        # walks the fields on-device
    ("1", "sign_typed_data_hash"),        # Trezor One: hashes only
])
def test_typed_data_recovers_on_each_model(device, model, call):
    device.model = model
    sig = TrezorSigner(_account()).sign_typed_data(
        TypedDataSigningRequest(from_addr=_address(N_PATH), typed_data=TYPED))

    assert Account.recover_message(
        encode_typed_data(full_message=TYPED), signature=sig) == _address(N_PATH)
    assert device.calls[-1][0] == call


# --- connection handling ---------------------------------------------------------


def test_session_is_reused_across_signatures(device):
    signer = TrezorSigner(_account())
    signer.sign(_req(), _chain())
    signer.sign(_req(nonce=8), _chain())
    assert device.opened == 1


def test_stale_session_reconnects_once(device):
    TrezorSigner(_account()).sign(_req(), _chain())       # opens session #1
    device.fail_next = TransportException("device was re-plugged")
    raw = TrezorSigner(_account()).sign(_req(nonce=8), _chain())

    assert device.opened == 2
    assert Account.recover_transaction(raw) == _address(N_PATH)


def test_user_rejection_is_not_retried(device):
    device.fail_next = exceptions.Cancelled()
    with pytest.raises(SignerError, match="Cancelled on the Trezor"):
        TrezorSigner(_account()).sign_message(
            MessageSigningRequest(from_addr=_address(N_PATH), raw=b"x"))
    assert device.opened == 1


def test_missing_trezorlib_explains_the_extra(monkeypatch):
    def no_lib(conn):
        raise ImportError("No module named 'trezorlib'")
    monkeypatch.setattr(trezor_mod, "_open_session", no_lib)
    trezor_mod._CONNECTION.reset()
    with pytest.raises(SignerError, match=r"qeth\[trezor\]"):
        TrezorSigner(_account()).sign(_req(), _chain())


def test_error_messages_are_actionable():
    explain = trezor_mod.explain_trezor_error
    assert "Connect it via USB" in explain(TransportException("No Trezor device found"))
    assert "udev" in explain(TransportException("LIBUSB_ERROR_ACCESS [-3]"))
    failure = messages.Failure(code=messages.FailureType.PinInvalid, message="x")
    assert explain(exceptions.TrezorFailure(failure)) == "Wrong PIN."


def test_definition_fetch_failure_answers_none_and_success_is_memoized(monkeypatch):
    fetched: list[str] = []

    def flaky(url):
        fetched.append(url)
        if "network" in url:
            raise OSError("offline")
        return b"signed-token"
    monkeypatch.setattr(trezor_mod, "_fetch_definition", flaky)
    monkeypatch.setattr(trezor_mod, "_DEFINITIONS", {})
    source = trezor_mod._definition_source()

    assert source.fetch_path("eth", "chain-id", "1", "network.dat") is None
    assert source.fetch_path("eth", "chain-id", "1", "token-ab.dat") == b"signed-token"
    assert source.fetch_path("eth", "chain-id", "1", "token-ab.dat") == b"signed-token"
    assert len(fetched) == 2        # the token was fetched once; the failure isn't cached


class FakeUI:
    def __init__(self, secret: str | None = "123") -> None:
        self.progress_calls: list[str] = []
        self.secret = secret

    def progress(self, text):
        self.progress_calls.append(text)

    def request_secret(self, prompt, *, title=""):
        return self.secret

    def exchange_qr(self, next_frame):
        raise AssertionError("not used")


def test_device_prompts_reach_the_interaction_host():
    conn = trezor_mod._Connection()
    conn.ui = FakeUI()
    conn.on_button(messages.ButtonRequest(code=messages.ButtonRequestType.PinEntry))
    conn.on_button(messages.ButtonRequest(code=messages.ButtonRequestType.SignTx))
    assert conn.ui.progress_calls == [
        "Enter your PIN on the Trezor…", "Confirm on your Trezor…"]
    assert conn.on_pin(messages.PinMatrixRequest()) == "123"

    conn.ui = FakeUI(secret=None)                  # the user cancelled the prompt
    with pytest.raises(exceptions.Cancelled):
        conn.on_passphrase()


# --- discovery -------------------------------------------------------------------


def _discover(scheme: str, count: int) -> tuple[list, list[str]]:
    worker = TrezorWorker(scheme, count)
    found: list = []
    fps: list[str] = []
    failures: list[str] = []
    worker.discovered.connect(found.append)
    worker.fingerprint.connect(fps.append)
    worker.failed.connect(failures.append)
    worker.run()                 # synchronously; the device jobs still hop threads
    assert failures == []
    return found, fps


def test_bip44_discovery_derives_on_the_host_from_one_public_node(qtbot, device):
    found, fps = _discover("BIP44 Standard", 7)

    assert fps == ["0x9bd26194"]
    assert [d.path for d in found] == [f"44'/60'/0'/0/{i}" for i in range(7)]
    assert [d.address for d in found] == [
        _address([44 | HARDENED, 60 | HARDENED, HARDENED, 0, i]) for i in range(7)]
    assert [c[0] for c in device.calls] == ["get_public_node"]


def test_legacy_discovery_uses_the_account_node(qtbot, device):
    found, _ = _discover("Legacy", 2)
    assert [d.address for d in found] == [
        _address([44 | HARDENED, 60 | HARDENED, HARDENED, i]) for i in range(2)]


def test_ledger_live_discovery_asks_the_device_per_address(qtbot, device):
    found, fps = _discover("Ledger Live", 3)

    assert fps == ["0x9bd26194"]
    assert [d.address for d in found] == [
        _address([44 | HARDENED, 60 | HARDENED, i | HARDENED, 0, 0]) for i in range(3)]
    assert [c[0] for c in device.calls] == ["get_address"] * 3


@pytest.mark.parametrize("scheme", ["BIP44 Standard", "Ledger Live"])
def test_each_scan_starts_a_fresh_session(qtbot, device, scheme):
    """The scan is where the user picks which passphrase wallet to import, so
    it must not reuse whatever wallet a signature last opened."""
    TrezorSigner(_account()).sign(_req(), _chain())          # session #1 open
    _discover(scheme, 1)
    assert device.sessions[0].closed
    assert device.opened == 2


def test_auto_detect_stops_after_consecutive_unused(qtbot, device):
    found, _ = _discover("BIP44 Standard", 0)      # no chain → every nonce is 0
    assert len(found) == trezor_mod.AUTO_STOP_CONSECUTIVE_ZEROS
