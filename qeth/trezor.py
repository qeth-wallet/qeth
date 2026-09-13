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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

import requests
from eth_utils import to_bytes, to_checksum_address

from PySide6.QtCore import QThread, Signal

from . import USER_AGENT
from .chain import EthClient
from .device_thread import DEFAULT_DEVICE_TIMEOUT_S, DeviceJobService
from .ledger import (
    AUTO_DETECT_BATCH_SIZE,
    AUTO_DETECT_HARD_CAP,
    AUTO_STOP_CONSECUTIVE_ZEROS,
    BIP44,
    LEDGER_LIVE,
    LEGACY,
    DiscoveredAccount,
)
from .qr.derive import derive_address
from .signing import (
    MessageSigningRequest,
    Signer,
    SignerError,
    SigningRequest,
    TypedDataSigningRequest,
    signed_eip1559_tx,
    signed_legacy_tx,
)

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


# --- signed network / token definitions -------------------------------------

_DEFINITIONS: dict[str, bytes | None] = {}
_DEFINITION_TIMEOUT_S = 10.0


def _fetch_definition(url: str) -> bytes | None:
    r = requests.get(url, timeout=_DEFINITION_TIMEOUT_S,
                     headers={"User-Agent": USER_AGENT})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


def _definition_source() -> Any:
    """Trezor's signed chain / token / clear-signing descriptions from
    data.trezor.io, fetched when the firmware asks, so the device can show
    "Ethereum" / "USDC" instead of a raw chain id or contract. Memoized; a
    failed fetch answers "none" (the device falls back to raw values) rather
    than aborting the signature."""
    from trezorlib.definitions import DEFS_BASE_URL, Source

    class _Tolerant(Source):
        def fetch_path(self, *components: str) -> bytes | None:
            url = DEFS_BASE_URL + "/".join(components)
            if url not in _DEFINITIONS:
                try:
                    _DEFINITIONS[url] = _fetch_definition(url)
                except Exception as e:   # noqa: BLE001 — never fail the sign on it
                    log.info("Trezor definition %s unavailable: %s", url, e)
                    return None
            return _DEFINITIONS[url]

    return _Tolerant()


# --- device operations (each runs inside a job) ------------------------------


def _ethereum() -> Any:
    """``trezorlib.ethereum``, typed ``Any``: its ``@workflow`` decorator's
    ParamSpec typing makes mypy read every parameter as ``Never``."""
    from trezorlib import ethereum
    return ethereum


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
                return ethereum.sign_tx_eip1559(
                    session, _address_n(path), nonce=nonce, gas_limit=gas,
                    to=to, value=value, data=data,
                    chain_id=chain.chain_id, max_gas_fee=max_fee,
                    max_priority_fee=tip, supports_definition_request=True,
                    definition_source=_definition_source())

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
            return ethereum.sign_tx(
                session, _address_n(path), nonce=nonce, gas_price=gas_price,
                gas_limit=gas, to=to, value=value, data=data,
                chain_id=chain.chain_id, supports_definition_request=True,
                definition_source=_definition_source())

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
        fields; a Trezor One can only sign the domain + message hashes."""
        path = self._path()

        def job(session: Any) -> bytes:
            ethereum = _ethereum()
            _require_holds(session, req.from_addr, path)
            if session.client.features.model == "1":
                from eth_account.messages import encode_typed_data
                signable = encode_typed_data(full_message=req.typed_data)
                return bytes(ethereum.sign_typed_data_hash(
                    session, _address_n(path), signable.header,
                    signable.body).signature)
            return bytes(ethereum.sign_typed_data(
                session, _address_n(path), req.typed_data,
                metamask_v4_compat=True).signature)

        return _signature_v27(self._run(job))


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
        template = PATH_SCHEMES.get(self._scheme)
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

    def _address_source(self, template: str) -> Callable[[list[int]], list[str]]:
        """Read the fingerprint (and the shared node, when the scheme has one)
        in one device job; return how to get the addresses for some indices."""
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
        client = EthClient(self._chain) if self._chain is not None else None
        auto = self._count == 0
        cap = AUTO_DETECT_HARD_CAP if auto else self._count
        unused = 0
        start = 0
        while start < cap:
            indices = list(range(start, min(start + AUTO_DETECT_BATCH_SIZE, cap)))
            for index, address in zip(indices, addresses_for(indices)):
                nonce = self._nonce(client, address)
                self.discovered.emit(DiscoveredAccount(
                    address=address, path=template.format(i=index),
                    index=index, nonce=nonce))
                if auto:
                    unused = unused + 1 if nonce == 0 else 0
                    if unused >= AUTO_STOP_CONSECUTIVE_ZEROS:
                        return
            start += len(indices)

    @staticmethod
    def _nonce(client: EthClient | None, address: str) -> int:
        """Latest-block sent-tx count — 0 = never used from this address."""
        if client is None:
            return 0
        try:
            return client.get_transaction_count(address, "latest")
        except Exception:
            return 0   # a lookup hiccup shouldn't drop the row
