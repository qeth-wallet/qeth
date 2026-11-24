"""Trezor hardware-wallet signing + account discovery, over trezorlib.

Every trezorlib call runs as a job on ONE process-lifetime thread
(``trezor_service()``, a ``DeviceJobService``) — the same discipline as Ledger
(see CLAUDE.md), and here it's also what lets the trezorlib client + session be
CACHED between jobs: a session carries the opened (passphrase) wallet, so the
device doesn't ask for the passphrase again on every signature. trezorlib opens
the USB transport per call, so qeth doesn't hold the device between jobs and
Trezor Suite can still use it. A cached session that went stale (device
unplugged / re-plugged, session evicted) is rebuilt once, transparently.

trezorlib is the optional ``trezor`` extra and is imported lazily: without it,
qeth runs normally and the Trezor add / sign flows explain how to install it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, TypeVar

import io

from eth_utils import keccak, to_bytes, to_checksum_address

from PySide6.QtCore import QThread, Signal

from .chain import EthClient
from .device_thread import DEFAULT_DEVICE_TIMEOUT_S, DeviceJobService
from .eip712_values import (
    DOMAIN, DOMAIN_FIELDS, Eip712Unsupported, read_address, read_bool,
    read_bytes, read_int,
)
from .ledger import (
    AUTO_DETECT_BATCH_SIZE,
    AUTO_DETECT_HARD_CAP,
    AUTO_STOP_CONSECUTIVE_ZEROS,
    BIP44,
    LEDGER_LIVE,
    LEGACY,
    DiscoveredAccount,
)
from .address import tron_from_hex, tron_to_hex
from .ledger_app import LedgerError, recover_parity
from .qr.derive import derive_address
from .signing import (
    MessageSigningRequest,
    Signer,
    SignerError,
    SigningRequest,
    TronSigningRequest,
    TypedDataSigningRequest,
    signed_eip1559_tx,
    signed_legacy_tx,
    typed_data_hashes,
)
from .tron.client import TronClient, TronError
from .tron.paths import PATH_SCHEMES as TRON_PATH_SCHEMES
from .tron.tx import TYPE_URL_PREFIX, signature_v27

from .chains import EVM, TRON

if TYPE_CHECKING:
    from .chains import Chain
    from .signers.interaction import SignerInteraction

log = logging.getLogger("qeth.trezor")

T = TypeVar("T")

NOT_INSTALLED = (
    "Trezor support isn't installed. Install it with:  "
    "uv pip install 'qeth[trezor]'"
)

# The derivation layouts qeth offers — BIP44 first, as Trezor Suite and
# MetaMask use it; Ledger Live and Legacy for seeds that came from elsewhere.
PATH_SCHEMES: dict[str, str] = {
    "BIP44 Standard": BIP44,
    "Ledger Live": LEDGER_LIVE,
    "Legacy": LEGACY,
}
# Schemes whose addresses are the non-hardened children ``…/i`` of one shared
# node: export that node's public key once, derive every address on the host.
# (Ledger Live hardens the account index, so it needs a device call per address.)
_SHARED_PARENT: dict[str, str] = {
    "BIP44 Standard": "44'/60'/0'/0",
    "Legacy": "44'/60'/0'",
}

# Tron accounts (qeth.tron.paths). The device's Tron app exports no public
# node, so each Tron address is one device call.
# Tron needs the core firmware's Tron app — Model T / Safe 3/5/7 on 2.11+.
_TRON_MIN_FIRMWARE = (2, 11, 0)


_SERVICE: DeviceJobService | None = None


def trezor_service() -> DeviceJobService:
    """The process-wide Trezor device thread (created on first use)."""
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = DeviceJobService(name="qeth-trezor-usb", device_label="Trezor")
    return _SERVICE


# --- the cached connection (Trezor thread only) ------------------------------


# What the device is waiting for, by ButtonRequestType name → the spinner text.
_BUTTON_PROMPTS = {
    "PinEntry": "Enter your PIN on the Trezor…",
    "PassphraseEntry": "Enter your passphrase on the Trezor…",
}


class _Connection:
    """The cached trezorlib session, plus the interaction host of the job in
    flight (so the device's callbacks — PIN, passphrase, "confirm on device" —
    reach the right window). Touched only on the Trezor thread."""

    def __init__(self) -> None:
        self.session: Any = None
        self.ui: SignerInteraction | None = None

    def get(self) -> Any:
        if self.session is None:
            self.session = _open_session(self)
        return self.session

    def reset(self) -> None:
        """Drop a session that went stale with the transport (nothing to close)."""
        self.session = None

    def forget(self) -> None:
        """End the open wallet session — the device forgets its passphrase —
        so the next job opens a new one and asks for the passphrase again."""
        if self.session is not None:
            self.session.close()      # trezorlib swallows a failed EndSession
            self.session = None

    # trezorlib callbacks — run on the Trezor thread, mid-exchange.

    def on_button(self, msg: Any) -> None:
        if self.ui is None:
            return
        from trezorlib import messages
        name = messages.ButtonRequestType(msg.code).name if msg.code else ""
        self.ui.progress(_BUTTON_PROMPTS.get(name, "Confirm on your Trezor…"))

    def on_pin(self, _msg: Any) -> str:
        """Trezor One: the PIN is typed on the computer against the scrambled
        grid shown on the device."""
        pin = self._ask(
            "Enter your PIN by position — the device shows a scrambled 3×3 "
            "grid; type the positions as on a numeric keypad (7 8 9 top row, "
            "1 2 3 bottom row).", "Trezor PIN")
        return pin

    def on_passphrase(self) -> str:
        """A device that can't take the passphrase itself (Trezor One)."""
        return self._ask(
            "Passphrase for your Trezor wallet (empty = the standard wallet):",
            "Trezor passphrase")

    def on_pairing_code(self) -> str:
        """THP devices (Safe 7): the one-time code shown when pairing."""
        return self._ask(
            "Enter the pairing code shown on your Trezor:", "Pair Trezor")

    def _ask(self, prompt: str, title: str) -> str:
        from trezorlib import exceptions
        if self.ui is None:
            raise SignerError(f"{title} needed, but there's no window to ask in")
        answer = self.ui.request_secret(prompt, title=title)
        if answer is None:
            raise exceptions.Cancelled
        return answer


def _open_session(conn: _Connection) -> Any:
    """Connect to the first Trezor and open its default wallet session — the
    PIN unlock (and on-device passphrase entry) happens here."""
    from trezorlib.client import get_default_client, get_default_session
    client = get_default_client(
        "qeth",
        button_callback=conn.on_button,
        pin_callback=conn.on_pin,
        code_entry_callback=conn.on_pairing_code,
    )
    return get_default_session(client, passphrase_callback=conn.on_passphrase)


_CONNECTION = _Connection()


def _is_stale_connection(e: BaseException) -> bool:
    """A failure that a fresh client + session can fix: the device was
    unplugged / re-plugged (transport / libusb errors), or it dropped our
    session (a new PassphraseRequest surfaces as InvalidSessionError)."""
    from trezorlib.exceptions import InvalidSessionError
    from trezorlib.transport import TransportException
    return (isinstance(e, (TransportException, InvalidSessionError))
            or type(e).__module__.startswith("usb1"))


def run_trezor_job(
    fn: Callable[[Any], T],
    ui: SignerInteraction | None = None,
    *,
    timeout: float = DEFAULT_DEVICE_TIMEOUT_S,
    fresh_session: bool = False,
) -> T:
    """Run ``fn(session)`` on the Trezor thread with the cached session,
    reconnecting once if it went stale. ``fresh_session`` ends the cached one
    first, so a passphrase wallet is asked for again. Every failure comes back
    as a ``SignerError`` with a user-facing message."""
    def job() -> T:
        _CONNECTION.ui = ui
        try:
            if fresh_session:
                _CONNECTION.forget()
            try:
                return fn(_CONNECTION.get())
            except Exception as e:
                if not _is_stale_connection(e):
                    raise
                log.info("Trezor connection went stale (%s); reconnecting", e)
                _CONNECTION.reset()
            try:
                return fn(_CONNECTION.get())
            except Exception as e:
                if _is_stale_connection(e):
                    _CONNECTION.reset()
                raise
        finally:
            _CONNECTION.ui = None

    try:
        return trezor_service().call(job, timeout=timeout)
    except ImportError as e:
        raise SignerError(NOT_INSTALLED) from e
    except SignerError:
        raise
    except Exception as e:
        raise SignerError(explain_trezor_error(e)) from e


def explain_trezor_error(e: BaseException) -> str:
    """One action-oriented sentence for a trezorlib / USB failure, falling back
    to the raw text so an unknown case stays diagnosable."""
    from trezorlib import exceptions, messages
    from trezorlib.transport import TransportException

    text = str(e)
    if isinstance(e, exceptions.Cancelled):
        return "Cancelled on the Trezor."
    if "LIBUSB_ERROR_ACCESS" in text or "LIBUSB_ERROR_BUSY" in text:
        return ("Couldn't open the Trezor over USB. If Trezor Suite is using "
                "it, close Suite; on Linux, install Trezor's udev rules "
                "(data.trezor.io/udev).")
    if isinstance(e, TransportException) or type(e).__module__.startswith("usb1"):
        return ("Trezor not detected. Connect it via USB and unlock it, then "
                "try again.")
    if isinstance(e, exceptions.OutdatedFirmwareError):
        return "Your Trezor's firmware is too old — update it in Trezor Suite."
    if isinstance(e, exceptions.TrezorFailure):
        failure = messages.FailureType
        if e.code == failure.PinInvalid:
            return "Wrong PIN."
        if e.code == failure.NotInitialized:
            return "This Trezor isn't set up yet — set it up in Trezor Suite first."
        if e.code == failure.DataError:
            return f"The Trezor rejected the request: {e.message}"
        return f"Trezor error: {e.message or text}"
    return f"Trezor error: {text}"


# --- signed network / token / clear-signing definitions -----------------------


def _definition_source() -> Any:
    """Trezor's signed chain / token / ERC-7730 descriptions, answered from
    the local copy of Trezor's archive (``trezor_definitions``) when the
    firmware asks mid-signature, so the device can show "CRV" or "Supply
    1,000 USDC" instead of raw values — without telling any server what is
    being signed. A definition the copy lacks is answered with none (the
    device shows the raw value). ``supplied`` says whether it answered any."""
    from trezorlib.definitions import Source

    from .trezor_definitions import default_definitions

    class _Local(Source):
        supplied = False

        def fetch_path(self, *components: str) -> bytes | None:
            blob = default_definitions().lookup("/".join(components))
            self.supplied |= blob is not None
            return blob

    return _Local()


def _no_definitions() -> Any:
    from trezorlib.definitions import NullSource
    return NullSource()


def _refused_definitions(e: BaseException) -> bool:
    """The firmware refused a definition it was given (an outdated one — it
    keeps a minimum date — or one it can't use). It does so before it shows
    anything, and aborts the signature."""
    from trezorlib import exceptions, messages
    return (isinstance(e, exceptions.TrezorFailure)
            and e.code == messages.FailureType.DataError
            and "definition" in (e.message or "").lower())


def _sign_with_definitions(sign: Callable[[Any], T]) -> T:
    """``sign(definition_source)`` with the local definitions; if the firmware
    refuses one, again without any — the device then shows raw values rather
    than refusing to sign."""
    source = _definition_source()
    try:
        return sign(source)
    except Exception as e:
        if not (source.supplied and _refused_definitions(e)):
            raise
        log.info("Trezor refused a definition (%s); signing without", e)
    return sign(_no_definitions())


# --- device operations (each runs inside a job) ------------------------------


def _ethereum() -> Any:
    """``trezorlib.ethereum``, typed ``Any``: its ``@workflow`` decorator's
    ParamSpec typing makes mypy read every parameter as ``Never``."""
    from trezorlib import ethereum
    return ethereum


def _tron() -> Any:
    """``trezorlib.tron``, typed ``Any`` for the same ParamSpec reason as
    ``_ethereum``."""
    from trezorlib import tron
    return tron


def _require_tron_support(session: Any) -> None:
    """Refuse up front on a device without the Tron app, with a reason the
    user can act on (instead of the device's bare "unknown message")."""
    f = session.client.features
    if f.model == "1":
        raise SignerError("The Trezor One doesn't support Tron — it needs a "
                          "Trezor Model T or Safe.")
    version = (f.major_version, f.minor_version, f.patch_version)
    if version < _TRON_MIN_FIRMWARE:
        raise SignerError(
            "Tron needs Trezor firmware 2.11.0 or newer (this device runs "
            f"{'.'.join(map(str, version))}). Update it in Trezor Suite.")


def _trezor_tron_encoding(tx: Any, contract: Any) -> bytes:
    """The raw_data bytes the firmware will build and sign from ``tx`` +
    ``contract`` (it re-serializes; it never signs the host's bytes). Reuses
    trezorlib's own protobuf encoder, which matches the firmware's."""
    from trezorlib import messages, protobuf
    kind = {
        messages.TronTransferContract: "TransferContract",
        messages.TronTriggerSmartContract: "TriggerSmartContract",
    }.get(type(contract))
    if kind is None:
        raise SignerError(f"unsupported Tron contract {type(contract).__name__}")
    value = io.BytesIO()
    protobuf.dump_message(value, contract)
    raw = messages.TronRawTransaction(
        ref_block_bytes=tx.ref_block_bytes, ref_block_hash=tx.ref_block_hash,
        expiration=tx.expiration, timestamp=tx.timestamp,
        fee_limit=tx.fee_limit, data=tx.data,
        contract=[messages.TronRawContract(
            type=getattr(messages.TronRawContractType, kind),
            parameter=messages.TronRawParameter(
                type_url=TYPE_URL_PREFIX + kind, value=value.getvalue()))])
    out = io.BytesIO()
    protobuf.dump_message(out, raw)
    return out.getvalue()


def _require_holds_tron(session: Any, address: str, path: str) -> None:
    """``_require_holds`` for a Tron account: the device must derive
    ``address`` at ``path`` through its Tron app."""
    derived = _tron().get_address(session, _address_n(path))
    if (tron_to_hex(derived) or "").lower() == address.lower():
        return
    _CONNECTION.forget()
    raise SignerError(
        f"This Trezor wallet doesn't hold {tron_from_hex(address)} — it derives "
        f"{derived} at {path}. If it's in a passphrase wallet, try again and "
        f"enter that passphrase.")


def _address_n(path: str) -> list[int]:
    from trezorlib.tools import parse_path
    return parse_path(path if path.startswith("m/") else f"m/{path}")


def _fingerprint(session: Any) -> str:
    """The wallet's root fingerprint as ``0x`` + 8 hex — distinct per seed AND
    per passphrase, so it identifies the device tree an import belongs to."""
    return "0x" + bytes(session.get_root_fingerprint()).hex()


def _require_holds(session: Any, address: str, path: str) -> None:
    """Refuse to sign unless the connected Trezor derives ``address`` at
    ``path`` — a different device, seed or passphrase wallet would otherwise
    produce a valid signature for the wrong account.

    On a mismatch the cached session is ENDED: it may be the wrong passphrase
    wallet (a typo opens a different, empty one), and keeping it would refuse
    every retry until the device was re-plugged. The next attempt asks for the
    passphrase again."""
    ethereum = _ethereum()
    derived = ethereum.get_address(session, _address_n(path))
    if derived.lower() == address.lower():
        return
    _CONNECTION.forget()
    if session.client.features.passphrase_protection:
        hint = ("If it's in a passphrase wallet, try again and enter that "
                "passphrase — a typo opens a different wallet.")
    else:
        hint = f"Connect the Trezor that owns {address} and try again."
    raise SignerError(
        f"This Trezor wallet doesn't hold {address} — it derives {derived} "
        f"at {path}. {hint}")


# --- EIP-712 as trezorlib can send it ------------------------------------------

_ARRAY = re.compile(r"^(.*)\[(\d*)\]$")
_INT_TYPE = re.compile(r"^(u?)int(\d+)$")
_BYTES_TYPE = re.compile(r"^bytes(\d+)$")
# trezorlib reads a type by these names before it looks at the structs.
_BUILTIN_TYPE = re.compile(r"^(u?int\d*|bytes\d*|string|bool|address)$")
_MAX_TREZOR_ARRAY = 0xFFFF          # trezorlib sends an array's length in 2 bytes


def trezor_typed_data(typed: Mapping[str, Any]) -> dict[str, Any]:
    """The dapp's typed data as trezorlib sends it and the firmware hashes it
    the way qeth does (eth_account): every value in the form trezorlib
    encodes — integers as ints (dapps send ``"0xf4240"``), bools as bools
    (``"true"``), bytes and addresses as hex, ``bytesN`` padded — and the
    domain's type as eth_account builds it from the domain's keys. Raises
    ``Eip712Unsupported`` for what the firmware can't hash at all: an array of
    arrays, a missing value, a type it doesn't know."""
    try:
        types, domain = typed["types"], typed["domain"]
        message, primary = typed["message"], typed["primaryType"]
    except (KeyError, TypeError) as e:
        raise Eip712Unsupported(f"typed data without {e}") from e
    if not all(isinstance(x, Mapping) for x in (types, domain, message)):
        raise Eip712Unsupported("malformed typed data")
    out_types: dict[str, list[dict[str, str]]] = {
        DOMAIN: [{"name": n, "type": t} for n, t in DOMAIN_FIELDS if n in domain]}
    for name, fields in types.items():
        if name == DOMAIN:
            continue
        if _BUILTIN_TYPE.match(name):
            raise Eip712Unsupported(f"struct {name} is named like a built-in type")
        if not isinstance(fields, list) or not all(
                isinstance(f, Mapping) and isinstance(f.get("name"), str)
                and isinstance(f.get("type"), str) for f in fields):
            raise Eip712Unsupported(f"struct {name} has a malformed field")
        out_types[name] = [{"name": f["name"], "type": f["type"]} for f in fields]
    if primary == DOMAIN or primary not in out_types:
        raise Eip712Unsupported(f"primary type {primary!r} isn't defined")

    def struct(name: str, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise Eip712Unsupported(f"{name} isn't a struct")
        return {f["name"]: field(f["name"], f["type"], value.get(f["name"]))
                for f in out_types[name]}

    def field(name: str, type_: str, value: Any) -> Any:
        if value is None:
            raise Eip712Unsupported(f"no value for {name}")
        if (m := _ARRAY.match(type_)) is not None:
            inner, size = m.group(1), m.group(2)
            if _ARRAY.match(inner):
                raise Eip712Unsupported(
                    f"{name} is an array of arrays, which Trezor's firmware can't hash")
            if (not isinstance(value, list) or len(value) > _MAX_TREZOR_ARRAY
                    or (size and len(value) != int(size))):
                raise Eip712Unsupported(f"{name} isn't an array of {inner}")
            return [field(name, inner, v) for v in value]
        if type_ in out_types:
            return struct(type_, value)
        if type_ == "address":
            return "0x" + read_address(name, value).hex()
        if type_ == "bool":
            return read_bool(value)
        if type_ == "string":
            if not isinstance(value, str):
                raise Eip712Unsupported(f"{name} isn't a string")
            return value
        if type_ == "bytes":
            return "0x" + read_bytes(name, value).hex()
        if (m := _INT_TYPE.match(type_)) is not None and int(m.group(2)) in range(8, 257, 8):
            n, bits = read_int(name, value), int(m.group(2))
            low, high = ((0, 1 << bits) if m.group(1)
                         else (-(1 << (bits - 1)), 1 << (bits - 1)))
            if not low <= n < high:
                raise Eip712Unsupported(f"{name} is out of range for {type_}")
            return n
        if (m := _BYTES_TYPE.match(type_)) is not None and 1 <= int(m.group(1)) <= 32:
            data, size = read_bytes(name, value), int(m.group(1))
            if len(data) > size:
                raise Eip712Unsupported(f"{name} is longer than {type_}")
            return "0x" + data.ljust(size, b"\x00").hex()
        raise Eip712Unsupported(f"{name} has type {type_}")

    return {"types": out_types, "primaryType": primary,
            "domain": struct(DOMAIN, domain), "message": struct(primary, message)}


def _signature_v27(signature: bytes) -> bytes:
    """personal_sign / EIP-712 convention: v as 27/28."""
    if len(signature) != 65:
        raise SignerError(f"expected a 65-byte signature, got {len(signature)}")
    v = signature[64]
    return signature[:64] + bytes([v + 27 if v < 27 else v])


class TrezorSigner(Signer):
    """Signs for one Trezor account record (``address`` + derivation ``path``),
    driving ``ui`` for the device's prompts. Transactions, ``personal_sign`` and
    EIP-712 all check first that the connected device holds the address."""

    def __init__(self, account: dict[str, Any], ui: SignerInteraction | None = None,
                 *, timeout: float = DEFAULT_DEVICE_TIMEOUT_S) -> None:
        self._account = account
        self._ui = ui
        self._timeout = timeout

    def can_sign(self, address: str) -> bool:
        return address.lower() == str(self._account.get("address", "")).lower()

    def _path(self) -> str:
        path = self._account.get("path")
        if not path:
            raise SignerError(
                f"Account {self._account.get('address')} has no derivation path on file")
        return str(path)

    def _run(self, fn: Callable[[Any], T]) -> T:
        return run_trezor_job(fn, self._ui, timeout=self._timeout)

    def sign(self, req: SigningRequest, chain: Chain) -> bytes:
        if req.gas is None or req.nonce is None:
            raise SignerError("gas and nonce must be set before signing")
        path = self._path()
        nonce, gas, value = req.nonce, req.gas, req.value_wei
        data = to_bytes(hexstr=req.data or "0x")
        # Checksummed, as the device shows it (qeth keeps addresses lowercased);
        # "" for a contract creation.
        to = to_checksum_address(req.to_addr) if req.to_addr else ""

        if chain.eip1559:
            if req.max_fee_per_gas is None or req.max_priority_fee_per_gas is None:
                raise SignerError(
                    "EIP-1559 fees missing — finalise gas suggestion first")
            max_fee, tip = req.max_fee_per_gas, req.max_priority_fee_per_gas

            def sign_1559(session: Any) -> tuple[int, bytes, bytes]:
                ethereum = _ethereum()
                _require_holds(session, req.from_addr, path)
                return _sign_with_definitions(lambda source: ethereum.sign_tx_eip1559(
                    session, _address_n(path), nonce=nonce, gas_limit=gas,
                    to=to, value=value, data=data,
                    chain_id=chain.chain_id, max_gas_fee=max_fee,
                    max_priority_fee=tip, supports_definition_request=True,
                    definition_source=source))

            v, r, s = self._run(sign_1559)
            return signed_eip1559_tx(req, chain.chain_id, v,
                                     int.from_bytes(r, "big"), int.from_bytes(s, "big"))

        if req.gas_price is None:
            raise SignerError("Legacy gas_price missing — finalise gas suggestion first")
        gas_price = req.gas_price

        def sign_legacy(session: Any) -> tuple[int, bytes, bytes]:
            ethereum = _ethereum()
            _require_holds(session, req.from_addr, path)
            # trezorlib returns v with the EIP-155 chain offset already applied.
            return _sign_with_definitions(lambda source: ethereum.sign_tx(
                session, _address_n(path), nonce=nonce, gas_price=gas_price,
                gas_limit=gas, to=to, value=value, data=data,
                chain_id=chain.chain_id, supports_definition_request=True,
                definition_source=source))

        v, r, s = self._run(sign_legacy)
        return signed_legacy_tx(req, v, int.from_bytes(r, "big"), int.from_bytes(s, "big"))

    def sign_message(self, req: MessageSigningRequest) -> bytes:
        """personal_sign — the device applies the EIP-191 prefix and shows the
        message for review."""
        path = self._path()

        def job(session: Any) -> bytes:
            ethereum = _ethereum()
            _require_holds(session, req.from_addr, path)
            return bytes(ethereum.sign_message(
                session, _address_n(path), req.raw).signature)

        return _signature_v27(self._run(job))

    def sign_typed_data(self, req: TypedDataSigningRequest) -> bytes:
        """EIP-712 v4. Trezor T / Safe models walk the typed data and show its
        fields — sent as ``trezor_typed_data`` reads it, so they hash what qeth
        hashes; a message they can't hash (an array of arrays) is refused
        before the device shows anything, since only a Trezor One can sign
        the domain + message hashes instead. Either way the signature must
        recover to the account over eth_account's hash."""
        path = self._path()
        domain_hash, message_hash = typed_data_hashes(req.typed_data)
        try:
            typed: dict[str, Any] | None = trezor_typed_data(req.typed_data)
            unsupported = ""
        except Eip712Unsupported as e:
            typed, unsupported = None, str(e)

        def job(session: Any) -> bytes:
            ethereum = _ethereum()
            _require_holds(session, req.from_addr, path)
            if session.client.features.model == "1":
                return bytes(ethereum.sign_typed_data_hash(
                    session, _address_n(path), domain_hash, message_hash).signature)
            if typed is None:
                raise SignerError(
                    f"This Trezor can't sign this typed message: {unsupported}. "
                    "Its firmware has no way to sign a typed message by its "
                    "hashes (only the Trezor One has).")
            return bytes(ethereum.sign_typed_data(
                session, _address_n(path), typed, metamask_v4_compat=True).signature)

        signature = self._run(job)
        if len(signature) != 65:
            raise SignerError(f"expected a 65-byte signature, got {len(signature)}")
        r, s = signature[:32], signature[32:64]
        try:
            parity = recover_parity(keccak(b"\x19\x01" + domain_hash + message_hash),
                                    int.from_bytes(r, "big"), int.from_bytes(s, "big"),
                                    req.from_addr)
        except LedgerError as e:
            raise SignerError(
                "The Trezor's signature doesn't match this message as qeth hashes "
                "it — refused, nothing was returned to the site.") from e
        return r + s + bytes([27 + parity])

    def sign_tron(self, req: TronSigningRequest) -> bytes:
        """A Tron transaction, via the firmware's Tron app. The device shows
        and signs ITS OWN re-encoding of the fields it's sent, so the fields
        are taken from our raw_data and the device's encoding is checked
        against it first: a field the firmware would drop (``call_value``
        before 2.12.5) is refused here instead of producing a signature over
        some other transaction."""
        path = self._path()
        raw = req.tx.raw_data()

        def job(session: Any) -> bytes:
            tron = _tron()
            _require_tron_support(session)
            _require_holds_tron(session, req.from_addr, path)
            tx, contract = tron.from_raw_data(raw)
            if _trezor_tron_encoding(tx, contract) != raw:
                raise SignerError(
                    "This Trezor can't sign this Tron transaction exactly as "
                    "built (a field its firmware doesn't support).")
            return bytes(tron.sign_tx(
                session, tx, contract, _address_n(path)).signature)

        return signature_v27(self._run(job))


# --- account discovery -------------------------------------------------------


class TrezorWorker(QThread):
    """Enumerates Trezor accounts for a derivation scheme in the background —
    the Trezor analogue of ``LedgerWorker``, emitting the same
    ``DiscoveredAccount`` so the add dialog is shared.

    BIP44 / Legacy export ONE public node and derive addresses on the host;
    Ledger Live asks the device for each batch. Each scan starts a fresh
    session, so a passphrase device asks which wallet to read — the one the user
    means to import, not whichever was open. ``count == 0`` scans until
    ``AUTO_STOP_CONSECUTIVE_ZEROS`` consecutive unused (nonce-0) accounts.
    ``fingerprint`` carries the wallet's root fingerprint before any account."""

    discovered = Signal(object)   # DiscoveredAccount
    fingerprint = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, scheme: str, count: int, chain: Chain | None = None,
                 ui: SignerInteraction | None = None, parent=None) -> None:
        super().__init__(parent)
        self._scheme = scheme
        self._count = count
        self._chain = chain
        self._ui = ui

    def run(self) -> None:
        template = PATH_SCHEMES.get(self._scheme) or TRON_PATH_SCHEMES.get(self._scheme)
        if template is None:
            self.failed.emit(f"Unknown derivation scheme: {self._scheme}")
            return
        try:
            addresses_for = self._address_source(template)
            self._scan(template, addresses_for)
        except SignerError as e:
            self.failed.emit(str(e))
            return
        self.finished_ok.emit()

    @property
    def _is_tron(self) -> bool:
        return self._scheme in TRON_PATH_SCHEMES

    def _address_source(self, template: str) -> Callable[[list[int]], list[str]]:
        """Read the fingerprint (and the shared node, when the scheme has one)
        in one device job; return how to get the addresses for some indices."""
        if self._is_tron:
            def tron_fingerprint(session: Any) -> str:
                _require_tron_support(session)
                return _fingerprint(session)

            self.fingerprint.emit(
                run_trezor_job(tron_fingerprint, self._ui, fresh_session=True))

            def tron_addresses(indices: list[int]) -> list[str]:
                def read(session: Any) -> list[str]:
                    tron = _tron()
                    out = []
                    for i in indices:
                        t = tron.get_address(session, _address_n(template.format(i=i)))
                        addr = tron_to_hex(t)
                        if addr is None:
                            raise SignerError(f"The Trezor returned a bad Tron address {t!r}")
                        out.append(addr)
                    return out
                return run_trezor_job(read, self._ui)

            return tron_addresses

        parent = _SHARED_PARENT.get(self._scheme)
        if parent is not None:
            def read_node(session: Any) -> tuple[str, bytes, bytes]:
                ethereum = _ethereum()
                node = ethereum.get_public_node(session, _address_n(parent)).node
                return _fingerprint(session), bytes(node.public_key), bytes(node.chain_code)

            xfp, pubkey, chain_code = run_trezor_job(
                read_node, self._ui, fresh_session=True)
            self.fingerprint.emit(xfp)
            return lambda indices: [
                derive_address(pubkey, chain_code, [i]) for i in indices]

        self.fingerprint.emit(
            run_trezor_job(_fingerprint, self._ui, fresh_session=True))

        def on_device(indices: list[int]) -> list[str]:
            def read_addresses(session: Any) -> list[str]:
                ethereum = _ethereum()
                return [ethereum.get_address(session, _address_n(template.format(i=i)))
                        for i in indices]
            return run_trezor_job(read_addresses, self._ui)

        return on_device

    def _scan(self, template: str,
              addresses_for: Callable[[list[int]], list[str]]) -> None:
        if self._is_tron:
            used = self._tron_used_probe()
        else:
            client = EthClient(self._chain) if self._chain is not None else None

            def used(address: str) -> int:
                return self._nonce(client, address)
        family = TRON if self._is_tron else EVM
        auto = self._count == 0
        cap = AUTO_DETECT_HARD_CAP if auto else self._count
        unused = 0
        start = 0
        while start < cap:
            indices = list(range(start, min(start + AUTO_DETECT_BATCH_SIZE, cap)))
            for index, address in zip(indices, addresses_for(indices)):
                nonce = used(address)
                self.discovered.emit(DiscoveredAccount(
                    address=address, path=template.format(i=index),
                    index=index, nonce=nonce, family=family))
                if auto:
                    unused = unused + 1 if nonce == 0 else 0
                    if unused >= AUTO_STOP_CONSECUTIVE_ZEROS:
                        return
            start += len(indices)

    def _tron_used_probe(self) -> Callable[[str], int]:
        """Tron has no nonce: an account counts as used once it's activated
        (has an on-chain record). Needs a Tron chain to ask; without one every
        row reads unused rather than failing the scan."""
        chain = self._chain
        if chain is None or chain.family != TRON:
            return lambda address: 0
        client = TronClient(chain)

        def used(address: str) -> int:
            try:
                return 1 if client.get_account(address) else 0
            except TronError:
                return 0   # a lookup hiccup shouldn't drop the row
        return used

    @staticmethod
    def _nonce(client: EthClient | None, address: str) -> int:
        """Latest-block sent-tx count — 0 = never used from this address."""
        if client is None:
            return 0
        try:
            return client.get_transaction_count(address, "latest")
        except Exception:
            return 0   # a lookup hiccup shouldn't drop the row
