"""Ledger hardware-wallet signing + account discovery.

Every ledgereth/hidapi call here is funnelled through the single-thread
``ledger_hid`` service (``run_ledger_hid_job``) rather than touching HID from
the transient Qt worker threads that drive signing/discovery — hidapi's macOS
backend is only valid on the thread that opened the handle. The service also
clears ledgereth's dongle cache after every job, so a stale USB-HID handle
never carries between operations.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from PySide6.QtCore import QThread, Signal

from .chain import EthClient
from .chains import Chain
from .ledger_hid import DEFAULT_LEDGER_HID_TIMEOUT_S, run_ledger_hid_job
from .signing import (
    MessageSigningRequest, Signer, SignerError, SigningRequest,
    TypedDataSigningRequest,
)

T = TypeVar("T")


def is_ledger_available() -> tuple[bool, str | None]:
    """Probe the Ledger device. Returns ``(True, None)`` when the
    dongle is connected, unlocked, and has the Ethereum app open;
    otherwise ``(False, reason)`` where ``reason`` is a short
    human-readable explanation from ledgereth.

    The probe runs on the HID service thread, which clears ledgereth's
    module-level dongle cache afterwards — so the next real sign /
    discovery call starts from a fresh ``init_dongle``."""
    try:
        run_ledger_hid_job(_probe_ledger_available)
    except ImportError as e:
        return False, f"ledgereth not installed: {e}"
    except SignerError as e:
        return False, str(e)
    except Exception as e:
        return False, _explain_ledger_error(e)
    return True, None


def _probe_ledger_available() -> None:
    from ledgereth.comms import init_dongle

    init_dongle()


def _explain_ledger_error(e: Exception) -> str:
    """Map a ledgereth exception to a single, action-oriented
    sentence that tells the user what's wrong and what to do.
    Fallback for unknown error shapes is to surface the raw text
    along with the SW code when we have one — so future
    "UNKNOWN" cases stay diagnosable instead of getting hidden."""
    try:
        from ledgereth.exceptions import (
            CommException, LedgerAppNotOpened, LedgerCancel,
            LedgerErrorCodes, LedgerInvalid, LedgerInvalidADPU,
            LedgerLocked, LedgerNotFound,
        )
    except ImportError:
        return f"Ledger error: {e}"

    if isinstance(e, LedgerCancel):
        return "Transaction rejected on the Ledger device."
    if isinstance(e, LedgerLocked):
        return (
            "Your Ledger is locked. Unlock it (enter your PIN) and "
            "try again."
        )
    if isinstance(e, LedgerAppNotOpened):
        # Covers APP_SLEEP (0x6804), APP_NOT_STARTED (0x6d00),
        # APP_NOT_FOUND (0x6d02) — i.e. the screensaver dimmed the
        # device, the user is on the dashboard, or Ethereum isn't
        # installed.
        return (
            "The Ethereum app isn't open on your Ledger. Wake the "
            "device and open the Ethereum app, then try again."
        )
    if isinstance(e, LedgerNotFound):
        return (
            "Ledger device not detected. Make sure it's connected "
            "via USB and unlocked."
        )
    if isinstance(e, LedgerInvalid):
        return (
            "Ledger rejected the transaction data as invalid. This "
            "is usually a bug in the wallet — open an issue with "
            "the tx details."
        )
    if isinstance(e, LedgerInvalidADPU):
        return (
            "Ledger communication error (APDU size mismatch). This "
            "is usually a transport-layer bug."
        )
    # Find the status word. ledgereth's comms layer catches
    # CommException and re-raises a generic LedgerError("Unexpected
    # error: 0x???? UNKNOWN") from err whenever the SW isn't in
    # ERROR_CODE_EXCEPTIONS — so by the time the exception reaches
    # us it's a LedgerError, and the CommException carrying the
    # sw attribute sits on __cause__. Check both.
    sw = getattr(e, "sw", None)
    if sw is None:
        cause = getattr(e, "__cause__", None)
        if isinstance(cause, CommException):
            sw = getattr(cause, "sw", None)
    if sw is not None:
        name = LedgerErrorCodes.get_by_value(sw)
        if name is not None:
            return f"Ledger error: {name} (0x{sw:04x})"
        # 0x55xx isn't in any public Ledger SW list but the device
        # emits it reproducibly when the screensaver turns off
        # (USB still attached). Steer the user there as the cause
        # instead of a generic "UNKNOWN".
        if (sw >> 8) == 0x55:
            return (
                "Your Ledger is asleep. Wake it up (press a "
                "button on the device), open the Ethereum app, "
                "and try again."
            )
        return f"Ledger error: unknown status word 0x{sw:04x}"
    # LedgerError without a more specific subclass and no SW we
    # could recover, or anything outside the ledgereth hierarchy
    # (USB layer, etc.).
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
    nonce: int = 0


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
        except ImportError as e:
            self.failed.emit(f"ledgereth not installed: {e}")
        except SignerError as e:
            self.failed.emit(str(e))
        except Exception as e:
            # Same decoder as the probe path — surfaces friendly messages for
            # known errors and the hex SW for unknown ones, instead of the raw
            # ledgereth "Unexpected error: 0x???? UNKNOWN" leaking through.
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


def _derive_ledger_paths(paths: list[str]) -> list[tuple[str, str]]:
    """Derive ``(path, address)`` for each path on one shared dongle.
    Runs on the HID service thread."""
    from ledgereth.accounts import get_account_by_path
    from ledgereth.comms import init_dongle

    dongle = init_dongle()
    return [
        (path, get_account_by_path(path, dongle=dongle).address)
        for path in paths
    ]


def _verify_device_holds_on_hid(address: str, path: str, dongle) -> None:
    """Refuse to sign unless the *connected* Ledger actually derives
    ``address`` at ``path`` (runs inside the signing HID job, on the shared
    dongle).

    qeth records Ledger accounts by address + path with no device or seed
    identifier, so a *different* Ledger plugged in would otherwise sign at
    this path as a DIFFERENT address — a valid signature for the wrong
    account. ``get_account_by_path`` is a silent getAddress read (no
    on-device confirmation)."""
    from ledgereth.accounts import get_account_by_path

    derived = get_account_by_path(path, dongle=dongle).address
    if derived.lower() != address.lower():
        raise SignerError(
            f"This Ledger doesn't hold {address} — it derives "
            f"{derived} at {path}. Connect the device/seed that owns "
            f"{address} and try again."
        )


def _run_ledger_job(fn: Callable[[], T], *, timeout: float) -> T:
    """Run ``fn`` on the HID service thread, mapping ledgereth failures to a
    uniform ``SignerError`` the RPC/UI layers already understand."""
    try:
        return run_ledger_hid_job(fn, timeout=timeout)
    except ImportError as e:
        raise SignerError(f"ledgereth not installed: {e}") from e
    except SignerError:
        raise
    except Exception as e:
        raise SignerError(_explain_ledger_error(e)) from e


def _raw_transaction_bytes(signed: Any) -> bytes:
    # ledgereth exposes the encoded signed tx in two shapes that both have
    # the name ``raw_transaction``-ish: an attribute (``rawTransaction`` — a
    # hex-string property) and a method (``raw_transaction()``) on different
    # versions. Don't grab the method object as if it were the payload (web3
    # chokes on the bound-method with a generic "expected … bytes" error).
    raw = getattr(signed, "rawTransaction", None)
    if raw is None:
        method = getattr(signed, "raw_transaction", None)
        if callable(method):
            raw = method()
    if raw is None:
        raise SignerError("Signed transaction has no raw payload")
    if isinstance(raw, str):
        raw = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
    return raw


class LedgerSigner(Signer):
    """``Signer`` implementation backed by a Ledger hardware wallet.

    Looks up the right derivation path for ``req.from_addr`` in the
    store's account list (where each Ledger-discovered account is
    recorded with its ``path``) and asks the dongle to sign — the
    user has to confirm on the device, so signing takes a few
    seconds and runs on the HID service thread. The device-holds check
    and the sign share one dongle inside a single HID job."""

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

    def sign(self, req: SigningRequest, chain: Chain) -> bytes:
        acct = self._lookup(req.from_addr)
        if acct is None:
            raise SignerError(
                f"No Ledger account known for {req.from_addr}"
            )
        path = acct.get("path")
        if not path:
            raise SignerError(
                f"Account {req.from_addr} has no derivation path on file"
            )

        if req.gas is None or req.nonce is None:
            raise SignerError("gas and nonce must be set before signing")

        kwargs: dict = {
            "destination": req.to_addr or "",
            "amount": req.value_wei,
            "gas": req.gas,
            "nonce": req.nonce,
            "data": req.data or "0x",
            "chain_id": chain.chain_id,
            "sender_path": path,
        }
        if chain.eip1559:
            if (req.max_fee_per_gas is None
                    or req.max_priority_fee_per_gas is None):
                raise SignerError(
                    "EIP-1559 fees missing — finalise gas suggestion first"
                )
            kwargs["max_fee_per_gas"] = req.max_fee_per_gas
            kwargs["max_priority_fee_per_gas"] = req.max_priority_fee_per_gas
        else:
            if req.gas_price is None:
                raise SignerError(
                    "Legacy gas_price missing — finalise gas suggestion first"
                )
            kwargs["gas_price"] = req.gas_price

        signed = _run_ledger_job(
            lambda: self._sign_transaction_on_hid(req.from_addr, path, kwargs),
            timeout=self._hid_timeout,
        )
        return _raw_transaction_bytes(signed)

    def _sign_transaction_on_hid(self, address: str, path: str, kwargs: dict):
        from ledgereth.comms import init_dongle
        from ledgereth.transactions import create_transaction

        dongle = init_dongle()
        _verify_device_holds_on_hid(address, path, dongle)
        return create_transaction(**kwargs, dongle=dongle)

    def sign_message(self, req: MessageSigningRequest) -> bytes:
        """personal_sign on the Ledger. The device prompts the user
        to review the message (truncated on screen) and confirm —
        same UX as MetaMask's "Sign" popup."""
        path = self._require_path(req.from_addr)
        signed = _run_ledger_job(
            lambda: self._sign_message_on_hid(req.from_addr, path, req.raw),
            timeout=self._hid_timeout,
        )
        return _extract_ledger_signature(signed)

    def _sign_message_on_hid(self, address: str, path: str, raw: bytes):
        from ledgereth.comms import init_dongle
        from ledgereth.messages import sign_message

        dongle = init_dongle()
        _verify_device_holds_on_hid(address, path, dongle)
        return sign_message(raw, sender_path=path, dongle=dongle)

    def sign_typed_data(self, req: TypedDataSigningRequest) -> bytes:
        """EIP-712 v4 — the Ledger Ethereum app does the full
        struct-hash walk on-device when the data is small enough,
        else falls back to a "blind sign" prompt the user must
        enable in the device settings."""
        path = self._require_path(req.from_addr)
        # ledgereth's draft signer expects the pre-computed domain
        # separator + message hash. eth_account's encode_typed_data
        # returns a SignableMessage where ``header`` is the 32-byte
        # domain separator and ``body`` is the 32-byte struct hash
        # (the two values the EIP-191 v0x01 prefix wraps). Earlier
        # versions of this code sliced ``body`` as if it carried both
        # halves — which the Ledger Ethereum app rejected.
        try:
            from eth_account.messages import encode_typed_data
        except ImportError as e:
            raise SignerError(f"eth_account missing: {e}") from e
        try:
            signable = encode_typed_data(full_message=req.typed_data)
            domain_hash = signable.header
            message_hash = signable.body
            if len(domain_hash) != 32 or len(message_hash) != 32:
                raise SignerError(
                    "encode_typed_data produced unexpected shape: "
                    f"header={len(domain_hash)} body={len(message_hash)}"
                )
        except SignerError:
            raise
        except Exception as e:
            raise SignerError(f"failed to hash typed data: {e}") from e
        signed = _run_ledger_job(
            lambda: self._sign_typed_data_on_hid(
                req.from_addr, path, domain_hash, message_hash,
            ),
            timeout=self._hid_timeout,
        )
        return _extract_ledger_signature(signed)

    def _sign_typed_data_on_hid(self, address: str, path: str,
                                domain_hash: bytes, message_hash: bytes):
        from ledgereth.comms import init_dongle
        from ledgereth.messages import sign_typed_data_draft

        dongle = init_dongle()
        _verify_device_holds_on_hid(address, path, dongle)
        return sign_typed_data_draft(
            domain_hash, message_hash, sender_path=path, dongle=dongle,
        )

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


def _extract_ledger_signature(signed) -> bytes:
    """ledgereth's SignedMessage exposes r/s/v as ints; some
    versions also have ``.signature``. Build the 65-byte
    r||s||v blob defensively."""
    sig = getattr(signed, "signature", None)
    if isinstance(sig, (bytes, bytearray)) and len(sig) == 65:
        return bytes(sig)
    if isinstance(sig, str):
        return bytes.fromhex(sig[2:] if sig.startswith("0x") else sig)
    r = int(signed.r).to_bytes(32, "big")
    s = int(signed.s).to_bytes(32, "big")
    v = int(signed.v)
    # Ledger returns v as 27/28 for legacy or chain-id-shifted for
    # EIP-155. For personal_sign / EIP-712 the conventional v is
    # 27/28; normalise.
    if v >= 27:
        v_byte = bytes([v % 256])
    else:
        v_byte = bytes([v + 27])
    return r + s + v_byte
