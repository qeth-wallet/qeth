"""Ledger Ethereum-app client: the USB-HID transport (plus Speculos TCP for
tests) and the app's APDU commands. Replaces ledgereth — see
docs/ledger-clear-signing.md.

Qt-free. Every device call must run on the ``ledger_hid`` service thread
(``run_ledger_hid_job``): hidapi's macOS backend ties a handle to the thread
that opened it. A transport is opened and closed inside one job, so no USB
handle outlives it (what ledgereth's dongle-cache clearing used to emulate).

Command encodings follow LedgerHQ/app-ethereum ``doc/apdu.md`` and its
Apache-2.0 Python client (``client/src/ledger_app_clients/ethereum``); the HID
framing is Ledger's channel/tag/sequence scheme, byte-identical to ledgerblue's.
Golden vectors captured from ledgereth pin both (tests/test_ledger_app.py).
"""

from __future__ import annotations

import os
import socket
import struct
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, NamedTuple, Protocol

LEDGER_VENDOR_ID = 0x2C97

# USB-HID framing: every 64-byte report carries channel(2) tag(1) seq(2); the
# first one also the 2-byte APDU length. Reports are zero-padded to 64.
_HID_CHANNEL = 0x0101
_HID_TAG_APDU = 0x05
_HID_PACKET = 64
# On macOS ``interface_number`` is unreliable; the Ledger's generic-HID
# interface is the one on this usage page.
_MACOS_USAGE_PAGE = 0xFFA0
# How long one blocking read waits before re-checking the deadline. The first
# response can take minutes (a human confirming on the device); continuation
# packets of a response arrive back-to-back.
_READ_POLL_MS = 250
_CONTINUATION_TIMEOUT_S = 5.0

# Set to "host:port" to talk to a Speculos emulator instead of USB — both for
# the Speculos test suite and for running qeth itself against an emulated
# device (scripts/speculos.sh).
SPECULOS_ENV = "QETH_LEDGER_SPECULOS"

CLA = 0xE0
INS_GET_ADDRESS = 0x02
INS_SIGN_TX = 0x04
INS_GET_CONFIGURATION = 0x06
INS_PERSONAL_SIGN = 0x08
INS_PROVIDE_ERC20 = 0x0A
INS_EIP712_SIGN = 0x0C

P1_FIRST_CHUNK = 0x00
P1_NEXT_CHUNK = 0x80
P2_EIP712_V0 = 0x00

SW_OK = 0x9000
MAX_APDU_DATA = 0xFF
# The Ethereum app derives at most this many BIP-32 levels.
MAX_PATH_DEPTH = 10

# GET APP CONFIGURATION flags (doc/apdu.md).
FLAG_BLIND_SIGNING = 0x01
FLAG_TOKEN_INFO_EXTERNAL = 0x02

# USB product id → device model. Current firmware reports ``model << 8 |
# interface mask``; old firmware a small fixed id per model.
_MODEL_BY_PID_HIGH = {
    0x10: "nanos", 0x40: "nanox", 0x50: "nanosp",
    0x60: "stax", 0x70: "flex", 0x80: "apex",
}
_MODEL_BY_LEGACY_PID = {
    0x0001: "nanos", 0x0004: "nanox", 0x0005: "nanosp",
    0x0006: "stax", 0x0007: "flex",
}


def model_for_product_id(product_id: int) -> str:
    """The Ledger model name for a USB product id ("unknown" if new)."""
    if product_id > 0xFF:
        return _MODEL_BY_PID_HIGH.get(product_id >> 8, "unknown")
    return _MODEL_BY_LEGACY_PID.get(product_id, "unknown")


# --- errors ------------------------------------------------------------------


class LedgerError(Exception):
    """Any failure talking to a Ledger. ``str()`` is a sentence for the user."""


class LedgerNotFound(LedgerError):
    def __init__(self) -> None:
        super().__init__(
            "Ledger device not detected. Make sure it's connected via USB and "
            "unlocked.")


class LedgerTransportError(LedgerError):
    """The USB / TCP link failed (unplugged, permissions, garbled framing)."""


class LedgerStatusError(LedgerError):
    """The device answered with a non-success status word."""

    def __init__(self, sw: int, data: bytes = b"") -> None:
        self.sw = sw
        self.data = data
        super().__init__(explain_status(sw))


_APP_NOT_OPEN = (
    "The Ethereum app isn't open on your Ledger. Wake the device and open "
    "the Ethereum app, then try again.")
_LOCKED = "Your Ledger is locked. Unlock it (enter your PIN) and try again."
_COMM_ERROR = (
    "Ledger communication error (status 0x{sw:04x}). This is usually a bug "
    "in the wallet — please report it.")

_STATUS_TEXT: dict[int, str] = {
    0x6985: "Request rejected on the Ledger device.",
    0x6982: ("The Ledger refused the request: it was rejected on the device, "
             "or the device is locked."),
    0x5515: _LOCKED,
    0x6B0C: _LOCKED,
    # 0x6511 / 0x6E00 / 0x6E01: the dashboard or another app is open;
    # 0x6D00 / 0x6D02: no such instruction / app; 0x6804: the app is asleep.
    0x6511: _APP_NOT_OPEN,
    0x6E00: _APP_NOT_OPEN,
    0x6E01: _APP_NOT_OPEN,
    0x6D00: _APP_NOT_OPEN,
    0x6D02: _APP_NOT_OPEN,
    0x6804: _APP_NOT_OPEN,
    0x6A80: ("The Ledger rejected the data. If this is a contract call, "
             "enable \"Blind signing\" in the Ethereum app's settings and try "
             "again."),
    0x6501: ("This version of the Ledger Ethereum app doesn't support this "
             "transaction type. Update the app with Ledger Live."),
}


def explain_status(sw: int) -> str:
    """A status word as one action-oriented sentence; unknown words keep their
    hex so a new case stays diagnosable."""
    text = _STATUS_TEXT.get(sw)
    if text is not None:
        return text
    if sw in (0x6700, 0x6983, 0x6B00):
        return _COMM_ERROR.format(sw=sw)
    if (sw >> 8) == 0x55:
        # Not in Ledger's public lists, but emitted reproducibly once the
        # screensaver turns the device off with USB still attached.
        return ("Your Ledger is asleep. Wake it up (press a button on the "
                "device), open the Ethereum app, and try again.")
    return f"Ledger error: unknown status word 0x{sw:04x}"


# --- transport ---------------------------------------------------------------


class Transport(Protocol):
    def exchange(self, apdu: bytes) -> tuple[int, bytes]:
        """Send one APDU; return ``(status word, response data)``."""
        ...

    def close(self) -> None: ...


def frame_apdu(apdu: bytes) -> list[bytes]:
    """Split ``apdu`` into zero-padded 64-byte HID reports."""
    data = struct.pack(">H", len(apdu)) + apdu
    packets: list[bytes] = []
    offset = 0
    seq = 0
    while offset < len(data):
        header = struct.pack(">HBH", _HID_CHANNEL, _HID_TAG_APDU, seq)
        chunk = data[offset:offset + _HID_PACKET - len(header)]
        packets.append((header + chunk).ljust(_HID_PACKET, b"\x00"))
        offset += len(chunk)
        seq += 1
    return packets


class ResponseAssembler:
    """Reassembles one framed response from HID reports, checking channel,
    tag and sequence on every packet."""

    def __init__(self) -> None:
        self._seq = 0
        self._expected: int | None = None
        self._buf = bytearray()

    def feed(self, packet: bytes) -> bytes | None:
        """Add one report; return the whole response once complete."""
        if len(packet) < 5:
            raise LedgerTransportError("Ledger sent a truncated USB packet.")
        channel, tag, seq = struct.unpack(">HBH", packet[:5])
        if channel != _HID_CHANNEL or tag != _HID_TAG_APDU:
            raise LedgerTransportError("Ledger sent an unexpected USB packet.")
        if seq != self._seq:
            raise LedgerTransportError(
                "Ledger USB packets arrived out of order.")
        body = packet[5:]
        if self._expected is None:
            if len(body) < 2:
                raise LedgerTransportError(
                    "Ledger sent a truncated USB packet.")
            self._expected = struct.unpack(">H", body[:2])[0]
            body = body[2:]
        self._seq += 1
        self._buf += body
        if len(self._buf) >= self._expected:
            return bytes(self._buf[:self._expected])
        return None


def split_status(response: bytes) -> tuple[int, bytes]:
    """``data ‖ sw`` → ``(sw, data)``."""
    if len(response) < 2:
        raise LedgerTransportError("Ledger response has no status word.")
    return struct.unpack(">H", response[-2:])[0], response[:-2]


@dataclass(frozen=True)
class LedgerDevice:
    """One connected Ledger: a USB HID path, or a Speculos ``host:port``."""

    path: bytes
    product_id: int = 0
    speculos: str | None = None

    @property
    def model(self) -> str:
        if self.speculos is not None:
            return "speculos"
        return model_for_product_id(self.product_id)


def _hid() -> Any:
    """The hidapi module (``hid``), imported lazily: only Ledger work needs it."""
    import hid
    return hid


def list_devices() -> list[LedgerDevice]:
    """Every connected Ledger, in a stable order. With ``QETH_LEDGER_SPECULOS``
    set, just the emulator."""
    speculos = os.environ.get(SPECULOS_ENV)
    if speculos:
        return [LedgerDevice(path=f"tcp:{speculos}".encode(), speculos=speculos)]
    found: dict[bytes, LedgerDevice] = {}
    for info in _hid().enumerate(LEDGER_VENDOR_ID, 0):
        if (info.get("interface_number") == 0
                or info.get("usage_page") == _MACOS_USAGE_PAGE):
            path = info["path"]
            found.setdefault(path, LedgerDevice(
                path=path, product_id=int(info.get("product_id") or 0)))
    return sorted(found.values(), key=lambda d: d.path)


class HidTransport:
    """A Ledger over USB HID. Open it, exchange, close — all on one thread."""

    def __init__(self, device: LedgerDevice) -> None:
        self.device = device
        self._dev = _hid().device()
        try:
            self._dev.open_path(device.path)
        except OSError as e:
            raise LedgerTransportError(
                "Couldn't open the Ledger over USB. On Linux this usually "
                "means Ledger's udev rules aren't installed.") from e

    def exchange(self, apdu: bytes) -> tuple[int, bytes]:
        try:
            for packet in frame_apdu(apdu):
                # hidapi wants the report id first; Ledger uses report 0.
                if self._dev.write(b"\x00" + packet) < 0:
                    raise LedgerTransportError(
                        "Writing to the Ledger over USB failed.")
            return split_status(self._read_response())
        except OSError as e:
            raise LedgerTransportError(
                "Lost the connection to the Ledger (was it unplugged?)") from e

    def _read_response(self) -> bytes:
        assembler = ResponseAssembler()
        started = False
        last = time.monotonic()
        while True:
            packet = bytes(self._dev.read(_HID_PACKET + 1, _READ_POLL_MS))
            if not packet:
                # The first packet waits as long as a human takes on the
                # device (the caller's job timeout bounds that); once a
                # response has started, the rest must follow promptly.
                if started and time.monotonic() - last > _CONTINUATION_TIMEOUT_S:
                    raise LedgerTransportError(
                        "The Ledger stopped responding mid-reply.")
                continue
            started = True
            last = time.monotonic()
            response = assembler.feed(packet)
            if response is not None:
                return response

    def close(self) -> None:
        try:
            self._dev.close()
        except OSError:
            pass


class TcpTransport:
    """The Speculos emulator's APDU socket: ``len(4) ‖ apdu`` out,
    ``len(4) ‖ data ‖ sw(2)`` back."""

    def __init__(self, address: str) -> None:
        host, _, port = address.rpartition(":")
        try:
            self._sock = socket.create_connection((host or "127.0.0.1", int(port)))
        except (OSError, ValueError) as e:
            raise LedgerTransportError(
                f"Couldn't reach the Speculos emulator at {address}.") from e

    def _recv_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise LedgerTransportError("Speculos closed the connection.")
            buf += chunk
        return bytes(buf)

    def exchange(self, apdu: bytes) -> tuple[int, bytes]:
        self._sock.sendall(struct.pack(">I", len(apdu)) + apdu)
        length = struct.unpack(">I", self._recv_exact(4))[0]
        data = self._recv_exact(length)
        sw = struct.unpack(">H", self._recv_exact(2))[0]
        return sw, data

    def close(self) -> None:
        self._sock.close()


@contextmanager
def connect(device: LedgerDevice) -> Iterator[Transport]:
    """Open ``device`` for the duration of a ``with`` block."""
    transport: Transport = (TcpTransport(device.speculos)
                            if device.speculos is not None
                            else HidTransport(device))
    try:
        yield transport
    finally:
        transport.close()


# --- the Ethereum app --------------------------------------------------------


def pack_path(path: str) -> bytes:
    """``44'/60'/0'/0/0`` → depth byte ‖ 4-byte big-endian indices (hardened
    with ``'`` or ``h``; a leading ``m/`` is accepted)."""
    parts = [p for p in path.strip().removeprefix("m/").split("/") if p]
    if not parts or len(parts) > MAX_PATH_DEPTH:
        raise ValueError(f"Invalid derivation path: {path!r}")
    out = bytearray([len(parts)])
    for part in parts:
        hardened = part[-1] in "'hH"
        digits = part[:-1] if hardened else part
        if not digits.isdigit() or int(digits) >= 0x80000000:
            raise ValueError(f"Invalid derivation path: {path!r}")
        out += struct.pack(">I", int(digits) | (0x80000000 if hardened else 0))
    return bytes(out)


def apdu(ins: int, p1: int = 0, p2: int = 0, data: bytes = b"") -> bytes:
    if len(data) > MAX_APDU_DATA:
        raise ValueError("APDU data exceeds 255 bytes")
    return bytes([CLA, ins, p1, p2, len(data)]) + data


def chunked_apdus(ins: int, payload: bytes, p2: int = 0) -> list[bytes]:
    """The app's multi-APDU streaming: 255-byte chunks, P1 ``00`` on the first
    and ``80`` on the rest."""
    chunks = [payload[i:i + MAX_APDU_DATA]
              for i in range(0, len(payload), MAX_APDU_DATA)] or [b""]
    return [apdu(ins, P1_FIRST_CHUNK if i == 0 else P1_NEXT_CHUNK, p2, chunk)
            for i, chunk in enumerate(chunks)]


@dataclass(frozen=True)
class AppConfig:
    flags: int
    version: tuple[int, int, int]

    @property
    def blind_signing_enabled(self) -> bool:
        return bool(self.flags & FLAG_BLIND_SIGNING)

    @property
    def wants_token_info(self) -> bool:
        return bool(self.flags & FLAG_TOKEN_INFO_EXTERNAL)


class RawSignature(NamedTuple):
    """The device's ``v ‖ r ‖ s``. ``v`` is whatever the command returns (a
    y-parity, 27/28, or the low byte of an EIP-155 ``v``) — callers resolve the
    parity by recovery instead of trusting its encoding."""

    v: int
    r: int
    s: int

    @classmethod
    def parse(cls, data: bytes) -> RawSignature:
        if len(data) < 65:
            raise LedgerTransportError(
                f"Ledger returned a {len(data)}-byte signature, expected 65.")
        return cls(data[0], int.from_bytes(data[1:33], "big"),
                   int.from_bytes(data[33:65], "big"))


def parse_address_response(data: bytes) -> str:
    """GET ADDRESS → ``pk_len ‖ pk ‖ addr_len ‖ addr (ASCII hex) [‖ chaincode]``,
    returned EIP-55 checksummed."""
    from eth_utils import to_checksum_address
    try:
        pk_len = data[0]
        addr_len = data[1 + pk_len]
        ascii_addr = data[2 + pk_len:2 + pk_len + addr_len]
        if addr_len != 40 or len(ascii_addr) != 40:
            raise IndexError
        return to_checksum_address("0x" + ascii_addr.decode("ascii"))
    except (IndexError, UnicodeDecodeError, ValueError) as e:
        raise LedgerTransportError(
            "Ledger returned a malformed address.") from e


class EthApp:
    """The Ethereum app's commands over one open transport."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def send(self, command: bytes) -> bytes:
        """One command; its data, or ``LedgerStatusError`` on any other status
        than success."""
        sw, data = self._transport.exchange(command)
        if sw != SW_OK:
            raise LedgerStatusError(sw, data)
        return data

    def _send_all(self, commands: list[bytes]) -> bytes:
        data = b""
        for command in commands:
            data = self.send(command)
        return data

    def get_configuration(self) -> AppConfig:
        data = self.send(apdu(INS_GET_CONFIGURATION))
        if len(data) < 4:
            raise LedgerTransportError("Ledger returned a malformed app config.")
        return AppConfig(flags=data[0], version=(data[1], data[2], data[3]))

    def get_address(self, path: str, *, display: bool = False) -> str:
        """The checksummed address at ``path``. ``display`` asks the user to
        confirm it on the device."""
        return parse_address_response(
            self.send(apdu(INS_GET_ADDRESS, int(display), 0, pack_path(path))))

    def provide_erc20_token_info(self, payload: bytes) -> None:
        """PROVIDE ERC 20 TOKEN INFORMATION (legacy format, P1 = 00): a
        Ledger-signed ticker/decimals for the token the next transaction calls
        (``ledger_metadata.TokenDescriptor.apdu_payload``). The app keeps it
        for that signature, so it shows a transfer/approval in the token's
        units instead of asking to blind sign."""
        self.send(apdu(INS_PROVIDE_ERC20, 0, 0, payload))

    def sign_transaction(self, path: str, unsigned_tx: bytes) -> RawSignature:
        """Sign an unsigned serialized tx (legacy EIP-155 RLP, or a typed
        ``0x02 ‖ rlp(...)``); the device shows it for review."""
        return RawSignature.parse(self._send_all(
            chunked_apdus(INS_SIGN_TX, pack_path(path) + unsigned_tx)))

    def sign_personal_message(self, path: str, message: bytes) -> RawSignature:
        """EIP-191 ``personal_sign`` — the device adds the prefix itself."""
        payload = pack_path(path) + struct.pack(">I", len(message)) + message
        return RawSignature.parse(self._send_all(
            chunked_apdus(INS_PERSONAL_SIGN, payload)))

    def sign_eip712_hashes(self, path: str, domain_hash: bytes,
                           message_hash: bytes) -> RawSignature:
        """EIP-712 by its two hashes (the app's V0 mode): the device shows the
        hashes, not the fields."""
        if len(domain_hash) != 32 or len(message_hash) != 32:
            raise ValueError("EIP-712 hashes must be 32 bytes each")
        return RawSignature.parse(self.send(apdu(
            INS_EIP712_SIGN, 0, P2_EIP712_V0,
            pack_path(path) + domain_hash + message_hash)))


def recover_parity(msg_hash: bytes, r: int, s: int, expected_address: str) -> int:
    """The y-parity (0/1) under which ``(r, s)`` over ``msg_hash`` recovers to
    ``expected_address``. Raises if neither does — the signature isn't the
    expected account's, so it must never be broadcast."""
    from eth_keys import KeyAPI
    from eth_keys.exceptions import BadSignature
    want = expected_address.lower()
    for parity in (0, 1):
        try:
            public_key = KeyAPI.Signature(vrs=(parity, r, s)) \
                .recover_public_key_from_msg_hash(msg_hash)
        except (BadSignature, ValueError):
            continue
        if public_key.to_checksum_address().lower() == want:
            return parity
    raise LedgerError(
        f"The Ledger's signature doesn't belong to {expected_address}. "
        "Nothing was sent.")
