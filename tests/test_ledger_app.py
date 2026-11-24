"""qeth's own Ledger client (qeth/ledger_app.py) and the signer on top of it.

Hermetic — no device:

* golden vectors captured from ledgereth/ledgerblue (tests/data/) pin the HID
  framing and every command byte-for-byte to what qeth sent before the
  migration;
* ``SoftLedger`` is an in-memory Ethereum app that parses the same APDUs and
  signs with real keys, the way the device does (including the low-byte
  EIP-155 ``v``), so ``LedgerSigner`` is checked end to end: the raw tx it
  returns must decode and recover to the account.

The emulated-device run (Speculos) lives in tests/test_ledger_speculos.py.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import rlp
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from eth_keys import KeyAPI
from eth_utils import keccak, to_checksum_address

from qeth import ledger as ledger_mod
from qeth import ledger_app
from qeth.chains import Chain
from qeth.ledger_app import (
    AppConfig, EthApp, LedgerDevice, LedgerError, LedgerNotFound,
    LedgerStatusError, LedgerTransportError, RawSignature, ResponseAssembler,
    explain_status, frame_apdu, model_for_product_id, pack_path,
    parse_address_response, recover_parity, split_status,
)
from qeth.signing import (
    MessageSigningRequest, SignerError, SigningRequest, TypedDataSigningRequest,
    unsigned_eip1559_tx, unsigned_legacy_tx,
)

GOLDEN = json.loads(
    (Path(__file__).parent / "data" / "ledger_golden_apdus.json").read_text())
TO = "0x" + "11" * 20
SIG = bytes([1]) + bytes(range(1, 33)) + bytes(range(33, 65))
DATA300 = bytes((i * 7) % 256 for i in range(300))


# --- framing -------------------------------------------------------------------


@pytest.mark.parametrize("size", sorted(GOLDEN["frames"], key=int))
def test_frame_apdu_matches_ledgerblue(size):
    vector = GOLDEN["frames"][size]
    packets = frame_apdu(bytes.fromhex(vector["apdu"]))
    assert all(len(p) == 64 for p in packets)
    assert b"".join(packets).hex() == vector["wire"]


def _response_packets(payload: bytes) -> list[bytes]:
    """A device response framed the way the Ledger sends it (same scheme)."""
    return frame_apdu(payload)


@pytest.mark.parametrize("size", [2, 3, 58, 59, 60, 200, 1000])
def test_response_assembler_round_trips(size):
    payload = bytes(i % 251 for i in range(size))
    assembler = ResponseAssembler()
    packets = _response_packets(payload)
    results = [assembler.feed(p) for p in packets]
    assert results[:-1] == [None] * (len(packets) - 1)
    assert results[-1] == payload


def test_response_assembler_rejects_wrong_channel_and_sequence():
    packets = _response_packets(bytes(100))
    bad_channel = b"\x02\x02" + packets[0][2:]
    with pytest.raises(LedgerTransportError, match="unexpected"):
        ResponseAssembler().feed(bad_channel)
    assembler = ResponseAssembler()
    assembler.feed(packets[0])
    with pytest.raises(LedgerTransportError, match="out of order"):
        assembler.feed(packets[0])


def test_split_status():
    assert split_status(b"\xab\xcd\x90\x00") == (0x9000, b"\xab\xcd")
    with pytest.raises(LedgerTransportError):
        split_status(b"\x90")


# --- encodings -----------------------------------------------------------------


class _Recorder:
    """A transport that records APDUs and answers each with ``reply``."""

    def __init__(self, reply: bytes = b"", sw: int = 0x9000):
        self.sent: list[str] = []
        self.reply = reply
        self.sw = sw

    def exchange(self, apdu: bytes):
        self.sent.append(apdu.hex())
        return self.sw, self.reply

    def close(self):
        pass


_ADDR_REPLY = bytes([65]) + bytes(65) + bytes([40]) + \
    b"7a16ff8270133f063aab6c9977183d9e72835428"


@pytest.mark.parametrize("key,path", [
    ("get_address_ll0", "44'/60'/0'/0/0"),
    ("get_address_ll3", "44'/60'/3'/0/0"),
    ("get_address_legacy7", "44'/60'/0'/7"),
])
def test_get_address_matches_ledgereth(key, path):
    rec = _Recorder(_ADDR_REPLY)
    address = EthApp(rec).get_address(path)
    assert rec.sent == GOLDEN[key]
    assert address == "0x7a16fF8270133F063aAb6C9977183D9e72835428"


def _req(**kw) -> SigningRequest:
    base = dict(chain_id=1, from_addr=TO, to_addr=TO, value_wei=0, data="0x")
    base.update(kw)
    return SigningRequest(**base)


@pytest.mark.parametrize("key,path,unsigned", [
    ("tx1559_simple", "44'/60'/0'/0/0", lambda: unsigned_eip1559_tx(_req(
        value_wei=10**18, gas=21000, nonce=5, max_fee_per_gas=30 * 10**9,
        max_priority_fee_per_gas=10**9), 1)),
    ("tx1559_long", "44'/60'/2'/0/0", lambda: unsigned_eip1559_tx(_req(
        gas=250000, nonce=1234, data="0x" + DATA300.hex(),
        max_fee_per_gas=2 * 10**9, max_priority_fee_per_gas=10**8), 8453)),
    ("tx1559_palm", "44'/60'/0'/0/0", lambda: unsigned_eip1559_tx(_req(
        value_wei=1, gas=21000, nonce=9, max_fee_per_gas=10**9,
        max_priority_fee_per_gas=1), 11297108109)),
    ("tx_legacy_bsc", "44'/60'/0'/0/0", lambda: unsigned_legacy_tx(_req(
        value_wei=12345, gas=60000, nonce=0, data="0x" + DATA300[:100].hex(),
        gas_price=3 * 10**9), 56)),
])
def test_sign_transaction_matches_ledgereth(key, path, unsigned):
    rec = _Recorder(SIG)
    sig = EthApp(rec).sign_transaction(path, unsigned())
    assert rec.sent == GOLDEN[key]
    assert sig == RawSignature.parse(SIG)


@pytest.mark.parametrize("key,path,message", [
    ("personal_long", "44'/60'/0'/0/0", ("hello qeth " * 30).encode()),
    ("personal_short", "44'/60'/1'/0/0", b"hi"),
])
def test_personal_sign_matches_ledgereth(key, path, message):
    rec = _Recorder(SIG)
    EthApp(rec).sign_personal_message(path, message)
    assert rec.sent == GOLDEN[key]


def test_eip712_hashes_match_ledgereth():
    rec = _Recorder(SIG)
    EthApp(rec).sign_eip712_hashes(
        "44'/60'/0'/0/0", bytes(range(32)), bytes(range(32, 64)))
    assert rec.sent == GOLDEN["eip712_v0"]


def test_payload_of_exact_chunk_multiple_sends_no_empty_trailer():
    """ledgereth sent an empty trailing APDU when the payload was an exact
    multiple of 255; Ledger's own client (and ours) doesn't."""
    path = "44'/60'/0'/0/0"
    rec = _Recorder(SIG)
    tx = bytes(510 - len(pack_path(path)))
    EthApp(rec).sign_transaction(path, tx)
    assert [len(bytes.fromhex(a)) for a in rec.sent] == [260, 260]


def test_get_configuration():
    rec = _Recorder(bytes([0x03, 1, 16, 0]))
    config = EthApp(rec).get_configuration()
    assert rec.sent == ["e006000000"]
    assert config == AppConfig(flags=3, version=(1, 16, 0))
    assert config.blind_signing_enabled and config.wants_token_info


def test_status_error_carries_sentence():
    with pytest.raises(LedgerStatusError) as exc:
        EthApp(_Recorder(sw=0x6985)).get_configuration()
    assert exc.value.sw == 0x6985
    assert "rejected on the ledger device" in str(exc.value).lower()


@pytest.mark.parametrize("path,expected", [
    ("44'/60'/0'/0/0", "058000002c8000003c800000000000000000000000"),
    ("m/44h/60h/1h/0/2", "058000002c8000003c800000010000000000000002"),
])
def test_pack_path(path, expected):
    assert pack_path(path).hex() == expected


@pytest.mark.parametrize("bad", ["", "44'/x", "/".join(["1"] * 11), "2147483648"])
def test_pack_path_rejects(bad):
    with pytest.raises(ValueError):
        pack_path(bad)


def test_parse_address_response_rejects_garbage():
    with pytest.raises(LedgerTransportError):
        parse_address_response(b"\x41" + bytes(10))


@pytest.mark.parametrize("pid,model", [
    (0x1011, "nanos"), (0x4015, "nanox"), (0x5011, "nanosp"),
    (0x0001, "nanos"), (0x0004, "nanox"), (0x9999, "unknown"),
])
def test_model_for_product_id(pid, model):
    assert model_for_product_id(pid) == model


@pytest.mark.parametrize("sw,needle", [
    (0x6985, "rejected"), (0x5515, "locked"), (0x6B0C, "locked"),
    (0x6E00, "ethereum app"), (0x6511, "ethereum app"), (0x6D00, "ethereum app"),
    (0x5501, "asleep"), (0x6A80, "blind signing"), (0x6F42, "0x6f42"),
    (0x6983, "communication"),
])
def test_explain_status(sw, needle):
    assert needle in explain_status(sw).lower()


# --- recovery ------------------------------------------------------------------


def test_recover_parity_picks_the_signers_parity():
    acct = Account.create()
    h = keccak(b"qeth")
    signed = Account._sign_hash(h, acct.key) if hasattr(Account, "_sign_hash") \
        else Account.unsafe_sign_hash(h, acct.key)
    parity = signed.v - 27 if signed.v >= 27 else signed.v
    assert recover_parity(h, signed.r, signed.s, acct.address) == parity
    with pytest.raises(LedgerError, match="doesn't belong"):
        recover_parity(h, signed.r, signed.s, "0x" + "22" * 20)


# --- HID transport over a fake hidapi -----------------------------------------


class _FakeHidDevice:
    def __init__(self, responses: list[bytes], *, stall_after: int | None = None):
        self.writes: list[bytes] = []
        self._queue: list[bytes] = []
        self._responses = list(responses)
        self._stall_after = stall_after
        self.closed = False

    def open_path(self, path):
        self.path = path

    def write(self, data):
        self.writes.append(bytes(data))
        # Answer once a complete command has been written.
        assembler = ResponseAssembler()
        done = None
        for report in self.writes:
            done = assembler.feed(report[1:]) or done
        if done is not None and self._responses:
            self.writes.clear()
            packets = frame_apdu(self._responses.pop(0))
            if self._stall_after is not None:
                packets = packets[:self._stall_after]
            self._queue.extend(packets)
        return len(data)

    def read(self, size, timeout_ms=0):
        return list(self._queue.pop(0)) if self._queue else []

    def close(self):
        self.closed = True


def _fake_hid(monkeypatch, device, enumerate_rows=None):
    module = SimpleNamespace(
        device=lambda: device,
        enumerate=lambda vid, pid: enumerate_rows or [],
    )
    monkeypatch.setattr(ledger_app, "_hid", lambda: module)


def test_hid_transport_exchange(monkeypatch):
    dev = _FakeHidDevice([bytes([7, 1, 24, 0]) + b"\x90\x00"])
    _fake_hid(monkeypatch, dev)
    transport = ledger_app.HidTransport(LedgerDevice(path=b"p", product_id=0x4015))
    assert EthApp(transport).get_configuration().version == (1, 24, 0)
    transport.close()
    assert dev.closed


def test_hid_transport_times_out_on_a_half_reply(monkeypatch):
    dev = _FakeHidDevice([bytes(200) + b"\x90\x00"], stall_after=1)
    _fake_hid(monkeypatch, dev)
    monkeypatch.setattr(ledger_app, "_CONTINUATION_TIMEOUT_S", 0.05)
    transport = ledger_app.HidTransport(LedgerDevice(path=b"p"))
    with pytest.raises(LedgerTransportError, match="mid-reply"):
        transport.exchange(bytes.fromhex("e006000000"))


def test_hid_open_failure_is_explained(monkeypatch):
    class _Refuses(_FakeHidDevice):
        def open_path(self, path):
            raise OSError("open failed")
    _fake_hid(monkeypatch, _Refuses([]))
    with pytest.raises(LedgerTransportError, match="udev"):
        ledger_app.HidTransport(LedgerDevice(path=b"p"))


def test_list_devices_filters_interfaces_and_sorts(monkeypatch):
    rows = [
        {"path": b"b", "interface_number": 0, "product_id": 0x1011},
        {"path": b"b-fido", "interface_number": 1, "product_id": 0x1011},
        {"path": b"a", "interface_number": -1, "usage_page": 0xFFA0,
         "product_id": 0x4015},
        {"path": b"b", "interface_number": 0, "product_id": 0x1011},
    ]
    monkeypatch.delenv(ledger_app.SPECULOS_ENV, raising=False)
    _fake_hid(monkeypatch, None, rows)
    devices = ledger_app.list_devices()
    assert [(d.path, d.model) for d in devices] == [(b"a", "nanox"), (b"b", "nanos")]


def test_list_devices_uses_speculos_when_configured(monkeypatch):
    monkeypatch.setenv(ledger_app.SPECULOS_ENV, "127.0.0.1:9999")
    [device] = ledger_app.list_devices()
    assert device.speculos == "127.0.0.1:9999" and device.model == "speculos"


# --- Speculos TCP transport against a loopback stand-in ------------------------


def test_tcp_transport_round_trip():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    seen: list[bytes] = []

    def serve():
        conn, _ = server.accept()
        with conn:
            length = struct.unpack(">I", conn.recv(4))[0]
            seen.append(conn.recv(length))
            reply = bytes([1, 1, 16, 0])
            # Dribble the reply to exercise the exact-read loop.
            for b in struct.pack(">I", len(reply)) + reply + b"\x90\x00":
                conn.send(bytes([b]))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    transport = ledger_app.TcpTransport(f"127.0.0.1:{port}")
    try:
        assert EthApp(transport).get_configuration().version == (1, 16, 0)
    finally:
        transport.close()
        thread.join(timeout=2)
        server.close()
    assert seen == [bytes.fromhex("e006000000")]


# --- SoftLedger: an in-memory Ethereum app ------------------------------------


def _key_for(seed: str, path: str):
    return KeyAPI.PrivateKey(keccak(f"{seed}/{path}".encode()))


def _rlp_total_len(buf: bytes) -> int | None:
    if not buf:
        return None
    b = buf[0]
    if b < 0xF8:
        return 1 + (b - 0xC0)
    ll = b - 0xF7
    if len(buf) < 1 + ll:
        return None
    return 1 + ll + int.from_bytes(buf[1:1 + ll], "big")


class SoftLedger:
    """Parses the Ethereum app's APDUs and signs like the device would."""

    def __init__(self, seed: str = "soft", *, version=(1, 16, 0), flags=0x01,
                 sign_sw: int | None = None, locked: bool = False,
                 token_sw: int = 0x9000):
        self.seed = seed
        self.token_sw = token_sw
        self.tokens: list[bytes] = []
        self.version = version
        self.flags = flags
        self.sign_sw = sign_sw
        self.locked = locked
        self.apdus: list[bytes] = []
        self._buf = b""
        self.closed = 0

    def address(self, path: str) -> str:
        return _key_for(self.seed, path).public_key.to_checksum_address()

    @staticmethod
    def _path(data: bytes) -> tuple[str, bytes]:
        depth = data[0]
        parts = []
        for i in range(depth):
            (n,) = struct.unpack(">I", data[1 + 4 * i:5 + 4 * i])
            parts.append(f"{n & 0x7FFFFFFF}'" if n & 0x80000000 else str(n))
        return "/".join(parts), data[1 + 4 * depth:]

    def _sign(self, path: str, msg_hash: bytes):
        return _key_for(self.seed, path).sign_msg_hash(msg_hash)

    def exchange(self, command: bytes):
        self.apdus.append(command)
        if self.locked:
            return 0x5515, b""
        ins, p1, data = command[1], command[2], command[5:]
        if ins == 0x06:
            return 0x9000, bytes([self.flags, *self.version])
        if ins == 0x0A:
            self.tokens.append(data)
            return self.token_sw, b""
        if ins == 0x02:
            path, _ = self._path(data)
            pub = _key_for(self.seed, path).public_key.to_bytes()
            ascii_addr = self.address(path)[2:].lower().encode()
            return 0x9000, bytes([65]) + b"\x04" + pub + bytes([40]) + ascii_addr
        if ins in (0x04, 0x08):
            self._buf = data if p1 == 0x00 else self._buf + data
            path, body = self._path(self._buf)
            if ins == 0x04:
                typed = body[:1] == b"\x02"
                rlp_part = body[1:] if typed else body
                total = _rlp_total_len(rlp_part)
                if total is None or len(rlp_part) < total:
                    return 0x9000, b""
                if self.sign_sw is not None:
                    return self.sign_sw, b""
                sig = self._sign(path, keccak(body))
                if typed:
                    v = sig.v
                else:
                    chain_id = int.from_bytes(rlp.decode(rlp_part)[6], "big")
                    v = (chain_id * 2 + 35 + sig.v) & 0xFF
            else:
                (length,) = struct.unpack(">I", body[:4])
                message = body[4:]
                if len(message) < length:
                    return 0x9000, b""
                prefix = b"\x19Ethereum Signed Message:\n" + str(length).encode()
                sig = self._sign(path, keccak(prefix + message))
                v = 27 + sig.v
            return 0x9000, bytes([v]) + sig.r.to_bytes(32, "big") + \
                sig.s.to_bytes(32, "big")
        if ins == 0x0C:
            path, body = self._path(data)
            sig = self._sign(path, keccak(b"\x19\x01" + body[:64]))
            return 0x9000, bytes([27 + sig.v]) + sig.r.to_bytes(32, "big") + \
                sig.s.to_bytes(32, "big")
        return 0x6D00, b""

    def close(self):
        self.closed += 1


def _plug(monkeypatch, *ledgers: SoftLedger):
    """Make ``qeth.ledger`` see ``ledgers`` as the connected devices."""
    devices = {LedgerDevice(path=f"soft{i}".encode()): led
               for i, led in enumerate(ledgers)}

    @contextmanager
    def fake_connect(device):
        led = devices[device]
        try:
            yield led
        finally:
            led.close()

    monkeypatch.setattr(ledger_mod, "list_devices", lambda: list(devices))
    monkeypatch.setattr(ledger_mod, "connect", fake_connect)


PATH = "44'/60'/0'/0/0"


def _signer(address: str, path: str = PATH):
    store = SimpleNamespace(accounts=[
        {"address": address, "source": "ledger", "path": path}])
    return ledger_mod.LedgerSigner(store, hid_timeout=10)


def _chain(chain_id: int, eip1559: bool) -> Chain:
    return Chain(name="test", chain_id=chain_id, rpc_url="http://127.0.0.1:1",
                 symbol="ETH", explorer="", eip1559=eip1559)


def _tx_req(address: str, **kw) -> SigningRequest:
    base = dict(chain_id=1, from_addr=address, to_addr=TO, value_wei=10**17,
                gas=60000, nonce=3, data="0x")
    base.update(kw)
    return SigningRequest(**base)


@pytest.mark.parametrize("chain_id", [1, 8453, 11297108109])
def test_signer_eip1559_round_trip(monkeypatch, chain_id):
    led = SoftLedger()
    _plug(monkeypatch, led)
    address = led.address(PATH)
    raw = _signer(address).sign(
        _tx_req(address, chain_id=chain_id, data="0x" + DATA300.hex(),
                max_fee_per_gas=2 * 10**9, max_priority_fee_per_gas=10**8),
        _chain(chain_id, True))
    assert Account.recover_transaction(raw) == address
    assert raw[0] == 2
    assert led.closed >= 1


@pytest.mark.parametrize("chain_id", [1, 56, 100, 117, 4_000_000_000])
def test_signer_legacy_round_trip_any_v_byte(monkeypatch, chain_id):
    """The device returns only the low byte of an EIP-155 ``v``; recovery must
    rebuild the full value for every chain id, incl. ones whose ``v`` wraps."""
    led = SoftLedger()
    _plug(monkeypatch, led)
    address = led.address(PATH)
    raw = _signer(address).sign(
        _tx_req(address, chain_id=chain_id, gas_price=3 * 10**9),
        _chain(chain_id, False))
    assert Account.recover_transaction(raw) == address
    v = rlp.decode(raw)[6]
    assert int.from_bytes(v, "big") in (chain_id * 2 + 35, chain_id * 2 + 36)


def test_signer_refuses_legacy_chain_id_above_32_bits(monkeypatch):
    led = SoftLedger()
    _plug(monkeypatch, led)
    address = led.address(PATH)
    with pytest.raises(SignerError, match="32 bits"):
        _signer(address).sign(
            _tx_req(address, chain_id=11297108109, gas_price=1),
            _chain(11297108109, False))
    assert led.apdus == []


def test_signer_personal_sign_round_trip(monkeypatch):
    led = SoftLedger()
    _plug(monkeypatch, led)
    address = led.address(PATH)
    message = ("sign in to qeth " * 40).encode()
    sig = _signer(address).sign_message(MessageSigningRequest(
        from_addr=address, raw=message))
    assert len(sig) == 65 and sig[64] in (27, 28)
    assert Account.recover_message(encode_defunct(message), signature=sig) == address


TYPED = {
    "types": {
        "EIP712Domain": [{"name": "name", "type": "string"},
                         {"name": "chainId", "type": "uint256"}],
        "Mail": [{"name": "contents", "type": "string"}],
    },
    "primaryType": "Mail",
    "domain": {"name": "qeth", "chainId": 1},
    "message": {"contents": "hello"},
}


def test_signer_typed_data_round_trip(monkeypatch):
    led = SoftLedger()
    _plug(monkeypatch, led)
    address = led.address(PATH)
    sig = _signer(address).sign_typed_data(TypedDataSigningRequest(
        from_addr=address, typed_data=TYPED))
    assert Account.recover_message(
        encode_typed_data(full_message=TYPED), signature=sig) == address


def test_signer_refuses_a_ledger_without_the_account(monkeypatch):
    led = SoftLedger(seed="other")
    _plug(monkeypatch, led)
    wanted = SoftLedger().address(PATH)
    with pytest.raises(SignerError, match="doesn't hold"):
        _signer(wanted).sign_message(MessageSigningRequest(
            from_addr=wanted, raw=b"x"))
    # Only the silent address read reached the device — never a sign.
    assert [c[1] for c in led.apdus] == [0x02]


def test_signer_picks_the_ledger_holding_the_account(monkeypatch):
    """Two Ledgers plugged in (the user's Nano S + Nano X): sign on the one
    that derives the account, skipping a locked one while looking."""
    locked, other, holder = (SoftLedger(seed="a", locked=True),
                             SoftLedger(seed="b"), SoftLedger(seed="c"))
    _plug(monkeypatch, locked, other, holder)
    address = holder.address(PATH)
    sig = _signer(address).sign_message(MessageSigningRequest(
        from_addr=address, raw=b"two ledgers"))
    assert Account.recover_message(
        encode_defunct(b"two ledgers"), signature=sig) == address
    assert [c[1] for c in other.apdus] == [0x02]


def test_signer_none_of_several_hold_it(monkeypatch):
    _plug(monkeypatch, SoftLedger(seed="a"), SoftLedger(seed="b"))
    wanted = SoftLedger(seed="c").address(PATH)
    with pytest.raises(SignerError, match="None of the connected Ledgers"):
        _signer(wanted).sign_message(MessageSigningRequest(
            from_addr=wanted, raw=b"x"))


def test_signer_locked_single_device_explains(monkeypatch):
    led = SoftLedger(locked=True)
    _plug(monkeypatch, led)
    address = SoftLedger().address(PATH)
    with pytest.raises(SignerError, match="locked"):
        _signer(address).sign_message(MessageSigningRequest(
            from_addr=address, raw=b"x"))


def test_signer_blind_signing_off_gets_a_precise_message(monkeypatch):
    led = SoftLedger(flags=0x00, sign_sw=0x6A80)
    _plug(monkeypatch, led)
    address = led.address(PATH)
    with pytest.raises(SignerError, match="Blind signing is off"):
        _signer(address).sign(
            _tx_req(address, data="0xa9059cbb" + "00" * 64,
                    max_fee_per_gas=10**9, max_priority_fee_per_gas=1),
            _chain(1, True))


def test_signer_rejection_on_device(monkeypatch):
    led = SoftLedger(sign_sw=0x6985)
    _plug(monkeypatch, led)
    address = led.address(PATH)
    with pytest.raises(SignerError, match="rejected"):
        _signer(address).sign(
            _tx_req(address, max_fee_per_gas=10**9, max_priority_fee_per_gas=1),
            _chain(1, True))
    # The device is closed even when the job fails.
    assert led.closed == 1


def test_signer_never_returns_a_signature_for_another_key(monkeypatch):
    """A device answering the address check correctly but signing with a
    different key (or a serialization mismatch) must not yield a raw tx."""
    led = SoftLedger()
    _plug(monkeypatch, led)
    address = led.address(PATH)
    led.seed = "evil"   # sign with another key from here on…
    monkeypatch.setattr(led, "address", lambda path: address)
    original = led.exchange

    def exchange(command):
        if command[1] == 0x02:   # …but keep answering the holder's address
            ascii_addr = address[2:].lower().encode()
            return 0x9000, bytes([65]) + bytes(65) + bytes([40]) + ascii_addr
        return original(command)
    led.exchange = exchange
    with pytest.raises(SignerError, match="doesn't belong"):
        _signer(address).sign(
            _tx_req(address, max_fee_per_gas=10**9, max_priority_fee_per_gas=1),
            _chain(1, True))


# --- discovery + probe ---------------------------------------------------------


def test_derive_paths_on_one_device(monkeypatch):
    led = SoftLedger()
    _plug(monkeypatch, led)
    paths = [f"44'/60'/{i}'/0/0" for i in range(3)]
    assert ledger_mod._derive_ledger_paths(paths) == [
        (p, led.address(p)) for p in paths]


def test_derive_paths_refuses_two_devices(monkeypatch):
    _plug(monkeypatch, SoftLedger(seed="a"), SoftLedger(seed="b"))
    with pytest.raises(SignerError, match="More than one Ledger"):
        ledger_mod._derive_ledger_paths([PATH])


def test_derive_paths_without_device(monkeypatch):
    _plug(monkeypatch)
    with pytest.raises(LedgerNotFound):
        ledger_mod._derive_ledger_paths([PATH])


def test_probe_reports_availability(monkeypatch):
    _plug(monkeypatch, SoftLedger(locked=True), SoftLedger())
    assert ledger_mod.is_ledger_available() == (True, None)
    _plug(monkeypatch, SoftLedger(locked=True))
    ok, reason = ledger_mod.is_ledger_available()
    assert not ok and "locked" in reason.lower()
    _plug(monkeypatch)
    ok, reason = ledger_mod.is_ledger_available()
    assert not ok and "not detected" in reason.lower()


def test_unsigned_serializers_hash_like_eth_account():
    """The bytes the device signs hash to what eth-account signs."""
    from eth_account._utils.legacy_transactions import (
        serializable_unsigned_transaction_from_dict,
    )
    req = _tx_req(TO, chain_id=56, gas_price=5, data="0xdead")
    legacy = serializable_unsigned_transaction_from_dict({
        "nonce": 3, "gasPrice": 5, "gas": 60000,
        "to": to_checksum_address(TO), "value": 10**17,
        "data": "0xdead", "chainId": 56})
    assert keccak(unsigned_legacy_tx(req, 56)) == legacy.hash()


# --- M4: token info from the local mirror ---------------------------------------

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
# Ledger's real, prod-signed USDC descriptor (CAL, 2026-11-24).
USDC_ENTRY = (
    "55534443a0b86991c6218b36c1d19d4a2e9eb0ce3606eb480000000600000001:"
    "3045022100b2e358726e4e6a6752cf344017c0e9d45b9a904120758d45f61b2804f9ad5299"
    "022015161ef28d8c4481bd9432c13562def9cce688bcfec896ef244c9a213f106cdd")
TRANSFER_DATA = "0xa9059cbb" + "00" * 12 + "22" * 20 + (10**6).to_bytes(32, "big").hex()
APPROVE_DATA = "0x095ea7b3" + "00" * 12 + "22" * 20 + "00" * 32


@pytest.fixture
def usdc_mirror(tmp_path, monkeypatch):
    from qeth import ledger_metadata
    monkeypatch.setattr(ledger_metadata, "CACHE_DIR", tmp_path / "ledger")
    path = tmp_path / "ledger" / "tokens" / "1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"tokens": {USDC: USDC_ENTRY}}))
    return path


def _usdc_tx(address: str, data: str) -> SigningRequest:
    return _tx_req(address, to_addr=USDC, value_wei=0, data=data,
                   max_fee_per_gas=10**9, max_priority_fee_per_gas=1)


def test_signer_sends_token_info_before_signing(monkeypatch, usdc_mirror):
    led = SoftLedger(flags=0x02)
    _plug(monkeypatch, led)
    address = led.address(PATH)
    raw = _signer(address).sign(_usdc_tx(address, TRANSFER_DATA), _chain(1, True))
    assert Account.recover_transaction(raw) == address
    # holds-check, config, token info, then the sign.
    assert [c[1] for c in led.apdus][:4] == [0x02, 0x06, 0x0A, 0x04]
    [payload] = led.tokens
    assert payload[0] == 4 and payload[1:5] == b"USDC"


def test_signer_skips_token_info_when_the_app_doesnt_want_it(monkeypatch, usdc_mirror):
    led = SoftLedger(flags=0x01)       # an app with its own token list
    _plug(monkeypatch, led)
    address = led.address(PATH)
    _signer(address).sign(_usdc_tx(address, TRANSFER_DATA), _chain(1, True))
    assert led.tokens == []


def test_signer_signs_anyway_when_the_device_refuses_token_info(monkeypatch, usdc_mirror):
    led = SoftLedger(flags=0x02, token_sw=0x6A80)
    _plug(monkeypatch, led)
    address = led.address(PATH)
    raw = _signer(address).sign(_usdc_tx(address, APPROVE_DATA), _chain(1, True))
    assert Account.recover_transaction(raw) == address


def test_signer_never_sends_a_tampered_descriptor(monkeypatch, usdc_mirror):
    data_hex, sig_hex = USDC_ENTRY.split(":")
    forged = data_hex.replace("00000006", "00000012") + ":" + sig_hex  # 18 decimals
    usdc_mirror.write_text(json.dumps({"tokens": {USDC: forged}}))
    led = SoftLedger(flags=0x02)
    _plug(monkeypatch, led)
    address = led.address(PATH)
    _signer(address).sign(_usdc_tx(address, TRANSFER_DATA), _chain(1, True))
    assert led.tokens == []


def test_plain_transfer_never_consults_the_mirror(monkeypatch, usdc_mirror):
    led = SoftLedger(flags=0x02)
    _plug(monkeypatch, led)
    address = led.address(PATH)
    _signer(address).sign(
        _tx_req(address, to_addr=USDC, max_fee_per_gas=10**9,
                max_priority_fee_per_gas=1), _chain(1, True))
    assert led.tokens == []


@pytest.mark.parametrize("data,needle", [
    ("0x", "Confirm on your Ledger device…"),
    (TRANSFER_DATA, "USDC transfer"),
    (APPROVE_DATA, "USDC approval"),
    ("0xdeadbeef", "blind sign"),
])
def test_device_review_text(usdc_mirror, data, needle):
    text = ledger_mod.device_review_text(_usdc_tx(TO, data), _chain(1, True))
    assert needle in text


def test_device_review_text_unknown_token_is_blind(usdc_mirror):
    req = _tx_req(TO, to_addr="0x" + "33" * 20, data=TRANSFER_DATA)
    assert "blind sign" in ledger_mod.device_review_text(req, _chain(1, True))


def test_ledger_plugin_progress_text_says_what_the_device_shows(usdc_mirror):
    from qeth.signers.ledger import LedgerSignerPlugin
    from qeth.signers.hot import HotWalletSignerPlugin
    req = _usdc_tx(TO, TRANSFER_DATA)
    assert "USDC transfer" in LedgerSignerPlugin().tx_progress_text(req, _chain(1, True))
    hot = HotWalletSignerPlugin()
    assert hot.tx_progress_text(req, _chain(1, True)) == hot.progress_text
