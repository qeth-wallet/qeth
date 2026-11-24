"""Ledger hardware-wallet signing + account discovery, on qeth's own
Ethereum-app client (``ledger_app``).

Every device call runs on the single-thread ``ledger_hid`` service
(``run_ledger_hid_job``) rather than on the transient Qt worker threads that
drive signing/discovery — hidapi's macOS backend is only valid on the thread
that opened the handle. Each job opens the device, does its work, and closes
it, so no USB handle carries between operations.

Signatures are never trusted on the device's word: the y-parity is resolved by
recovering the signer from the hash qeth computed, which must be the account
being signed for (``ledger_app.recover_parity``). For a typed message the
device hashes it itself (``ledger_eip712``), so that recovery is also what
proves it hashed the message qeth showed.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from eth_utils import keccak
from PySide6.QtCore import QThread, Signal

from .chain import EthClient
from .chains import Chain
from .ledger_app import (
    AppConfig, EthApp, LedgerDevice, LedgerError, LedgerNotFound,
    LedgerStatusError, RawSignature, connect, list_devices, recover_parity,
)
from .ledger_eip712 import (
    INS_PROVIDE_ERC20, Eip712Message, Eip712Unsupported, MessageFilters,
    check_filters, domain_binding, filter_token_addresses, filtered_v1_commands,
    parse_typed_data, schema_hash, v1_commands, v2_commands,
)
from .ledger_hid import DEFAULT_LEDGER_HID_TIMEOUT_S, run_ledger_hid_job
from .ledger_metadata import (
    TokenDescriptor, default_eip712_filters, default_mirror, erc20_call_kind,
)
from .signing import (
    MessageSigningRequest, Signer, SignerError, SigningRequest,
    TypedDataSigningRequest, signed_eip1559_tx, signed_legacy_tx,
    unsigned_eip1559_tx, unsigned_legacy_tx,
)

T = TypeVar("T")

log = logging.getLogger(__name__)

# The Ethereum app's legacy (type-0) parser keeps the EIP-155 chain id in 32
# bits; ledgereth refused larger ids for the same reason (its issue #41).
_LEGACY_MAX_CHAIN_ID = 0xFFFFFFFF

_SW_INVALID_DATA = 0x6A80
_SW_REJECTED = 0x6985

# A typed message goes to the device whole, for it to hash and list: the V1
# commands since app 1.9.19, V2 since 1.24.0 (checked whole before the review,
# and on a Nano it lists every field — V1 lists the domain, then shows the
# message hash unless the app's "Raw messages" setting is on).
_EIP712_V1_APP = (1, 9, 19)
_EIP712_V2_APP = (1, 24, 0)
# An app without either (the Nano S's) refuses the first command so, showing
# nothing: the hashes follow.
_SW_NO_FULL_EIP712 = (0x6D00, 0x6B00)
# A message Ledger describes (its signed filters, in qeth's snapshot) goes as
# V1 with them: reviewed with Ledger's labels, not blind signing. 1.12 has
# every filter kind the snapshot uses, and filters inside empty arrays.
_EIP712_FILTER_APP = (1, 12, 0)
# A message is a long review on a Nano, one field per screen.
_TYPED_DATA_TIMEOUT_S = 600.0

_TYPED_DATA_BLIND_OFF = (
    "Blind signing is off in your Ledger's Ethereum app. The app counts any "
    "typed message Ledger hasn't described as blind signing, even when it "
    "lists the fields. Turn on \"Blind signing\" in the app's settings, then "
    "try again.")


def is_ledger_available() -> tuple[bool, str | None]:
    """Probe the connected Ledger(s). Returns ``(True, None)`` when one is
    connected, unlocked, and has the Ethereum app open; otherwise
    ``(False, reason)`` with a short, actionable explanation."""
    try:
        run_ledger_hid_job(_probe_ledger_available)
    except SignerError as e:
        return False, str(e)
    except Exception as e:
        return False, _explain_ledger_error(e)
    return True, None


def _probe_ledger_available() -> None:
    """Succeeds if ANY connected Ledger answers with the Ethereum app open."""
    devices = list_devices()
    if not devices:
        raise LedgerNotFound()
    first_error: Exception | None = None
    for device in devices:
        try:
            with connect(device) as transport:
                EthApp(transport).get_configuration()
            return
        except LedgerError as e:
            first_error = first_error or e
    assert first_error is not None
    raise first_error


def _explain_ledger_error(e: Exception) -> str:
    """One sentence for the user. ``LedgerError``s already carry one; anything
    else (a missing hidapi, an unexpected crash) keeps its own text."""
    if isinstance(e, LedgerError):
        return str(e)
    if isinstance(e, ImportError):
        return f"Ledger support is unavailable: {e}"
    return f"Ledger error: {e}"


LEDGER_LIVE = "44'/60'/{i}'/0/0"
LEGACY = "44'/60'/0'/{i}"
BIP44 = "44'/60'/0'/0/{i}"

PATH_SCHEMES: dict[str, str] = {
    "Ledger Live": LEDGER_LIVE,
    "Legacy": LEGACY,
    "BIP44 Standard": BIP44,
}

AUTO_STOP_CONSECUTIVE_ZEROS = 3
AUTO_DETECT_HARD_CAP = 100
# Derive this many paths per HID job. One ``init_dongle`` is amortised over a
# batch (cheaper + less USB churn) while keeping all HID work on the service
# thread; nonce lookups (RPC, not HID) happen between batches on the worker.
AUTO_DETECT_BATCH_SIZE = 5


@dataclass
class DiscoveredAccount:
    address: str
    path: str
    index: int
    # On-chain transaction count for ``address`` at the "latest"
    # block — i.e. how many txs this wallet has *sent*. We use this
    # rather than the native balance to identify used accounts:
    # a wallet that received funds but never signed anything still
    # has nonce 0, and from the user's perspective is effectively
    # uncreated (they've never controlled it from this device).
    # Tron has no nonce: there it's 1 for an activated account, else 0.
    nonce: int = 0
    # The chain family the account was derived for (qeth.chains) — Tron
    # accounts come from coin type 195 and are stored with ``family``.
    family: str = "evm"


class LedgerWorker(QThread):
    """Enumerates Ledger accounts in a background thread.

    If `count` is 0, scans until `AUTO_STOP_CONSECUTIVE_ZEROS` consecutive
    accounts with nonce 0 (up to `AUTO_DETECT_HARD_CAP`). Nonces are
    fetched from `chain` if provided. Path derivation (HID) is delegated to
    the ledger_hid service in batches; nonce lookups stay on this thread.
    """

    discovered = Signal(object)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, scheme: str, count: int, chain: Chain | None = None, parent=None):
        super().__init__(parent)
        self.scheme = scheme
        self.count = count
        self.client = EthClient(chain) if chain is not None else None

    def run(self) -> None:
        template = PATH_SCHEMES.get(self.scheme)
        if template is None:
            self.failed.emit(f"Unknown derivation scheme: {self.scheme}")
            return

        auto = self.count == 0
        max_scan = AUTO_DETECT_HARD_CAP if auto else self.count

        try:
            if auto:
                self._scan_auto(template, max_scan)
            else:
                paths = [template.format(i=i) for i in range(max_scan)]
                derived = run_ledger_hid_job(
                    lambda: _derive_ledger_paths(paths),
                )
                for i, (path, address) in enumerate(derived):
                    nonce = self._nonce(address) if self.client else 0
                    self.discovered.emit(DiscoveredAccount(
                        address=address, path=path, index=i, nonce=nonce,
                    ))
            self.finished_ok.emit()
        except (SignerError, LedgerError) as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(_explain_ledger_error(e))

    def _scan_auto(self, template: str, max_scan: int) -> None:
        """Derive in batches of ``AUTO_DETECT_BATCH_SIZE`` until
        ``AUTO_STOP_CONSECUTIVE_ZEROS`` consecutive nonce-0 accounts."""
        consecutive_zero = 0
        i = 0
        while i < max_scan:
            paths = [
                template.format(i=j)
                for j in range(i, min(i + AUTO_DETECT_BATCH_SIZE, max_scan))
            ]
            # Synchronous (run_ledger_hid_job blocks on the result), so the
            # current ``paths`` is what runs — no late-binding capture needed.
            derived = run_ledger_hid_job(lambda: _derive_ledger_paths(paths))
            for offset, (path, address) in enumerate(derived):
                nonce = self._nonce(address) if self.client else 0
                self.discovered.emit(DiscoveredAccount(
                    address=address, path=path, index=i + offset, nonce=nonce,
                ))
                if nonce == 0:
                    consecutive_zero += 1
                    if consecutive_zero >= AUTO_STOP_CONSECUTIVE_ZEROS:
                        return
                else:
                    consecutive_zero = 0
            i += len(paths)

    def _nonce(self, address: str) -> int:
        """Latest-block sent-tx count for ``address``. We ask for
        "latest", not "pending", because we want the canonical
        on-chain identity of the wallet — a pending tx in the
        mempool from another client could otherwise tip a fresh
        address into looking used."""
        try:
            assert self.client is not None  # only called once a chain is set
            return self.client.get_transaction_count(address, "latest")
        except Exception:
            return 0


def _single_device() -> LedgerDevice:
    """The one connected Ledger, for discovery: with several plugged in it
    would be ambiguous which seed the scanned accounts belong to."""
    devices = list_devices()
    if not devices:
        raise LedgerNotFound()
    if len(devices) > 1:
        raise SignerError(
            "More than one Ledger is connected. Unplug all but the one whose "
            "accounts you want to add, then scan again.")
    return devices[0]


def _derive_ledger_paths(paths: list[str]) -> list[tuple[str, str]]:
    """Derive ``(path, address)`` for each path in one session. Runs on the
    HID service thread."""
    with connect(_single_device()) as transport:
        app = EthApp(transport)
        return [(path, app.get_address(path)) for path in paths]


def _on_device_holding(address: str, path: str, fn: Callable[[EthApp], T]) -> T:
    """Run ``fn`` on the connected Ledger that derives ``address`` at ``path``
    (runs inside one HID job).

    qeth records Ledger accounts by address + path with no device or seed
    identifier, so a *different* Ledger would otherwise sign at this path as a
    DIFFERENT address — a valid signature for the wrong account. With several
    Ledgers connected, the one holding the account is used; a locked or
    app-less device is skipped while looking. ``get_address`` is a silent read
    (no on-device confirmation)."""
    devices = list_devices()
    if not devices:
        raise LedgerNotFound()
    derived_elsewhere: list[str] = []
    first_error: LedgerError | None = None
    for device in devices:
        try:
            with connect(device) as transport:
                app = EthApp(transport)
                derived = app.get_address(path)
                if derived.lower() == address.lower():
                    return fn(app)
                derived_elsewhere.append(derived)
        except LedgerStatusError as e:
            if len(devices) == 1:
                raise
            first_error = first_error or e
    if not derived_elsewhere and first_error is not None:
        raise first_error
    if len(devices) == 1:
        raise SignerError(
            f"This Ledger doesn't hold {address} — it derives "
            f"{derived_elsewhere[0]} at {path}. Connect the device/seed that "
            f"owns {address} and try again.")
    raise SignerError(
        f"None of the connected Ledgers holds {address} at {path}. Connect "
        "the device/seed that owns it and try again.")


def _run_ledger_job(fn: Callable[[], T], *, timeout: float) -> T:
    """Run ``fn`` on the HID service thread, mapping device failures to the
    uniform ``SignerError`` the RPC/UI layers already understand."""
    try:
        return run_ledger_hid_job(fn, timeout=timeout)
    except SignerError:
        raise
    except Exception as e:
        raise SignerError(_explain_ledger_error(e)) from e


class LedgerSigner(Signer):
    """``Signer`` implementation backed by a Ledger hardware wallet.

    Looks up the right derivation path for ``req.from_addr`` in the
    store's account list (where each Ledger-discovered account is
    recorded with its ``path``) and asks the device to sign — the
    user has to confirm on the device, so signing takes a few
    seconds and runs on the HID service thread. The device-holds check
    and the sign share one session inside a single HID job."""

    def __init__(self, store, *, hid_timeout: float = DEFAULT_LEDGER_HID_TIMEOUT_S):
        self._store = store
        self._hid_timeout = hid_timeout

    def can_sign(self, address: str) -> bool:
        return self._lookup(address) is not None

    def _lookup(self, address: str) -> dict | None:
        addr = address.lower()
        for a in self._store.accounts:
            if (a.get("source") == "ledger"
                    and a["address"].lower() == addr):
                return a
        return None

    def _require_path(self, address: str) -> str:
        acct = self._lookup(address)
        if acct is None:
            raise SignerError(
                f"No Ledger account known for {address}"
            )
        path = acct.get("path")
        if not path:
            raise SignerError(
                f"Account {address} has no derivation path on file"
            )
        return path

    def sign(self, req: SigningRequest, chain: Chain) -> bytes:
        path = self._require_path(req.from_addr)
        if req.gas is None or req.nonce is None:
            raise SignerError("gas and nonce must be set before signing")

        if chain.eip1559:
            if (req.max_fee_per_gas is None
                    or req.max_priority_fee_per_gas is None):
                raise SignerError(
                    "EIP-1559 fees missing — finalise gas suggestion first"
                )
            unsigned = unsigned_eip1559_tx(req, chain.chain_id)
        else:
            if req.gas_price is None:
                raise SignerError(
                    "Legacy gas_price missing — finalise gas suggestion first"
                )
            if chain.chain_id > _LEGACY_MAX_CHAIN_ID:
                raise SignerError(
                    f"The Ledger can't sign a legacy transaction for chain id "
                    f"{chain.chain_id} (above 32 bits).")
            unsigned = unsigned_legacy_tx(req, chain.chain_id)

        has_data = bool(req.data and req.data not in ("0x", "0X"))
        # Local lookup only (docs/ledger-clear-signing.md): the mirror was
        # filled on a schedule, so nothing about this tx leaves the machine.
        token = (default_mirror().lookup(chain.chain_id, req.to_addr)
                 if has_data else None)

        def job(app: EthApp):
            config = app.get_configuration()
            if token is not None and config.wants_token_info:
                _provide_token(app, token)
            try:
                return app.sign_transaction(path, unsigned)
            except LedgerStatusError as e:
                if (e.sw == _SW_INVALID_DATA and has_data
                        and not config.blind_signing_enabled):
                    raise SignerError(
                        "Blind signing is off in your Ledger's Ethereum app. "
                        "Turn it on in the app's settings to sign this "
                        "contract call, then try again.") from e
                raise

        raw = _run_ledger_job(
            lambda: _on_device_holding(req.from_addr, path, job),
            timeout=self._hid_timeout,
        )
        parity = self._parity(keccak(unsigned), raw.r, raw.s, req.from_addr)
        if chain.eip1559:
            return signed_eip1559_tx(req, chain.chain_id, parity, raw.r, raw.s)
        return signed_legacy_tx(
            req, chain.chain_id * 2 + 35 + parity, raw.r, raw.s)

    @staticmethod
    def _parity(msg_hash: bytes, r: int, s: int, address: str) -> int:
        try:
            return recover_parity(msg_hash, r, s, address)
        except LedgerError as e:
            raise SignerError(str(e)) from e

    def sign_message(self, req: MessageSigningRequest) -> bytes:
        """personal_sign on the Ledger. The device applies the EIP-191 prefix,
        shows the message (truncated on screen) and asks to confirm."""
        path = self._require_path(req.from_addr)
        raw = _run_ledger_job(
            lambda: _on_device_holding(
                req.from_addr, path,
                lambda app: app.sign_personal_message(path, req.raw)),
            timeout=self._hid_timeout,
        )
        prefix = b"\x19Ethereum Signed Message:\n" + str(len(req.raw)).encode()
        parity = self._parity(keccak(prefix + req.raw), raw.r, raw.s,
                              req.from_addr)
        return _rsv(raw.r, raw.s, 27 + parity)

    def sign_typed_data(self, req: TypedDataSigningRequest) -> bytes:
        """EIP-712 v4. The whole message goes to the device, which hashes it
        and lists the fields — through Ledger's labels when Ledger describes
        it (its filters, from qeth's snapshot), which needs no blind signing;
        where the app can't take it (a Nano S, an app before 1.9.19, a message
        it can't carry) the device signs the domain and message hashes instead
        (the V0 mode), showing those."""
        path = self._require_path(req.from_addr)
        domain_hash, message_hash = _typed_data_hashes(req.typed_data)
        try:
            message: Eip712Message | None = parse_typed_data(req.typed_data)
        except Eip712Unsupported as e:
            log.info("typed message goes to the Ledger as hashes: %s", e)
            message = None
        # Local lookups only: the snapshot ships with qeth, the token mirror
        # was filled on a schedule.
        described = _described(message) if message is not None else None
        raw = _run_ledger_job(
            lambda: _on_device_holding(
                req.from_addr, path,
                lambda app: _sign_typed(app, path, message, domain_hash,
                                        message_hash, described)),
            timeout=max(self._hid_timeout, _TYPED_DATA_TIMEOUT_S),
        )
        parity = self._parity(keccak(b"\x19\x01" + domain_hash + message_hash),
                              raw.r, raw.s, req.from_addr)
        return _rsv(raw.r, raw.s, 27 + parity)


def _typed_data_hashes(typed_data: dict) -> tuple[bytes, bytes]:
    """eth_account's domain and message hashes — what qeth shows, and what
    the signature must recover against."""
    try:
        from eth_account.messages import encode_typed_data
        signable = encode_typed_data(full_message=typed_data)
    except Exception as e:
        raise SignerError(f"failed to hash typed data: {e}") from e
    domain_hash, message_hash = signable.header, signable.body
    if len(domain_hash) != 32 or len(message_hash) != 32:
        raise SignerError(
            "encode_typed_data produced unexpected shape: "
            f"header={len(domain_hash)} body={len(message_hash)}")
    return domain_hash, message_hash


@dataclass(frozen=True)
class _Described:
    """Ledger's filters for a message, and the info of its tokens."""

    filters: MessageFilters
    tokens: tuple[TokenDescriptor, ...]


def _ledger_filters(message: Eip712Message) -> MessageFilters | None:
    """Ledger's filters for ``message`` from the snapshot, if it has some the
    device can apply."""
    binding = domain_binding(message)
    if binding is None:
        return None
    filters = default_eip712_filters().lookup(*binding, schema_hash(message))
    if filters is None:
        return None
    try:
        check_filters(message, filters)
    except Eip712Unsupported as e:
        log.info("Ledger's filters for %s don't fit the message: %s",
                 filters.name, e)
        return None
    return filters


def _described(message: Eip712Message) -> _Described | None:
    filters = _ledger_filters(message)
    if filters is None:
        return None
    mirror = default_mirror()
    tokens = [mirror.lookup(filters.chain_id, a)
              for a in filter_token_addresses(message, filters)]
    return _Described(filters, tuple(t for t in tokens if t is not None))


def _sign_typed(app: EthApp, path: str, message: Eip712Message | None,
                domain_hash: bytes, message_hash: bytes,
                described: _Described | None = None) -> RawSignature:
    """Inside the HID job: the whole message where the app takes it (with
    Ledger's filters when it describes it), else the hashes."""
    config = app.get_configuration()
    try:
        if message is not None and config.version >= _EIP712_V1_APP:
            raw = None
            if described is not None and config.version >= _EIP712_FILTER_APP:
                raw = _sign_described_eip712(app, config, path, message,
                                             described)
            if raw is None:
                raw = _sign_full_eip712(app, config, path, message)
            if raw is not None:
                return raw
        return app.sign_eip712_hashes(path, domain_hash, message_hash)
    except LedgerStatusError as e:
        if e.sw == _SW_INVALID_DATA and not config.blind_signing_enabled:
            raise SignerError(_TYPED_DATA_BLIND_OFF) from e
        raise


def _sign_described_eip712(app: EthApp, config: AppConfig, path: str,
                           message: Eip712Message,
                           described: _Described) -> RawSignature | None:
    """The message as V1 with Ledger's filters; ``None`` when the device
    refused them before the signature request, so it can go undescribed. The
    app shows a filtered review only at that request, and dismisses whatever
    a refused command left on screen."""
    tokens = ([t.apdu_payload() for t in described.tokens]
              if config.wants_token_info else [])
    try:
        commands = filtered_v1_commands(message, path, described.filters, tokens)
    except Eip712Unsupported as e:
        log.info("typed message goes to the Ledger undescribed: %s", e)
        return None
    *setup, sign = commands
    for command in setup:
        try:
            app.send(command)
        except LedgerStatusError as e:
            if command[1] == INS_PROVIDE_ERC20:
                # Costs only the token's name and decimals on screen.
                log.info("Ledger refused token info for a typed message: %s", e)
                continue
            if e.sw == _SW_REJECTED:
                raise
            log.info("Ledger refused its filters for %s (0x%04x); sending the "
                     "message undescribed", described.filters.name, e.sw)
            return None
    return RawSignature.parse(app.send(sign))


def _sign_full_eip712(app: EthApp, config: AppConfig, path: str,
                      message: Eip712Message) -> RawSignature | None:
    """The message for the device to hash and list; ``None`` when it refused
    it before showing anything, so the hashes can follow."""
    v2 = config.version >= _EIP712_V2_APP
    try:
        commands = v2_commands(message, path) if v2 else v1_commands(message, path)
    except Eip712Unsupported as e:
        log.info("typed message goes to the Ledger as hashes: %s", e)
        return None
    *setup, sign = commands
    for i, command in enumerate(setup):
        try:
            app.send(command)
        except LedgerStatusError as e:
            # V2's schema and values show nothing; V1 reviews as it goes, so
            # only a refusal of its first command leaves the screen untouched.
            if v2 or (i == 0 and e.sw in _SW_NO_FULL_EIP712):
                log.info("Ledger refused the whole typed message (0x%04x); "
                         "signing its hashes", e.sw)
                return None
            raise
    try:
        return RawSignature.parse(app.send(sign))
    except LedgerStatusError as e:
        # V2 refuses a review it can't build with 0x6A80 and shows nothing.
        # With blind signing off the same word means that setting, and the
        # hashes would be refused too.
        if v2 and e.sw == _SW_INVALID_DATA and config.blind_signing_enabled:
            log.info("Ledger couldn't review the whole typed message; "
                     "signing its hashes")
            return None
        raise


def _provide_token(app: EthApp, token: TokenDescriptor) -> None:
    """Hand the device the token's signed ticker/decimals before the sign. A
    refusal (an app too old for it, a descriptor it rejects) only costs the
    clear display — the signature itself proceeds as before."""
    try:
        app.provide_erc20_token_info(token.apdu_payload())
    except LedgerStatusError as e:
        log.info("Ledger refused token info for %s on %s: %s",
                 token.address, token.chain_id, e)


def device_review_text(req: SigningRequest, chain: Chain) -> str:
    """What the user will see on the Ledger for ``req``, known before the
    device is touched — shown while the signature is pending. Local lookup
    only."""
    if not req.data or req.data in ("0x", "0X"):
        return "Confirm on your Ledger device…"
    kind = erc20_call_kind(req.data)
    token = default_mirror().lookup(chain.chain_id, req.to_addr) if kind else None
    if token is not None:
        verb = "transfer" if kind == "transfer" else "approval"
        return (f"Confirm on your Ledger device — it shows this as a "
                f"{token.ticker} {verb}…")
    return ("Confirm on your Ledger device — it can't decode this contract "
            "call, so it asks you to blind sign (turn on \"Blind signing\" "
            "in the Ethereum app's settings if it refuses)…")


def typed_data_review_text(typed_data: dict) -> str:
    """The spinner label for a typed message: what Ledger calls it when it
    describes it, and the hashes, for the screens that show them instead of
    fields (a Nano S's, an older app's) — in the device's own format."""
    try:
        domain_hash, message_hash = _typed_data_hashes(typed_data)
    except SignerError:
        return "Confirm on your Ledger device…"
    try:
        filters = _ledger_filters(parse_typed_data(typed_data))
    except Eip712Unsupported:
        filters = None
    lead = "Confirm on your Ledger device…"
    if filters is not None:
        lead = (f"Confirm on your Ledger device — Ledger describes this "
                f"message as \u201c{filters.name}\u201d, so a Nano X or Nano S "
                f"Plus lists it with Ledger's labels…")
    return (f"{lead}\n\n"
            "Where it shows hashes instead of fields, they must read:\n"
            f"Domain hash: 0x{domain_hash.hex().upper()}\n"
            f"Message hash: 0x{message_hash.hex().upper()}")


def _rsv(r: int, s: int, v: int) -> bytes:
    """The 65-byte ``r ‖ s ‖ v`` signature dapps expect."""
    return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v])
