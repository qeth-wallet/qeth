from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QThread, Signal

from .chain import EthClient
from .chains import Chain
from .signing import (
    MessageSigningRequest, Signer, SignerError, SigningRequest,
    TypedDataSigningRequest,
)


def is_ledger_available() -> tuple[bool, Optional[str]]:
    """Probe the Ledger device. Returns ``(True, None)`` when the
    dongle is connected, unlocked, and has the Ethereum app open;
    otherwise ``(False, reason)`` where ``reason`` is a short
    human-readable explanation from ledgereth.

    Side effect: a successful probe leaves ledgereth's module-level
    dongle cache empty, so the next real sign / discovery call
    starts from a fresh ``init_dongle`` (consistent with
    LedgerSigner.sign's "fresh handle per call" invariant — see
    qeth/ledger.py)."""
    try:
        from ledgereth.comms import init_dongle
        from ledgereth import comms as _comms
    except ImportError as e:
        return False, f"ledgereth not installed: {e}"
    try:
        dongle = init_dongle()
    except Exception as e:
        return False, _explain_ledger_error(e)
    # Drop the cache + close the probe handle. ledgereth keeps the
    # Dongle in a module-level slot across calls; reusing it for
    # the next real sign is unreliable (see the dongle-cache
    # discussion in LedgerSigner.sign).
    _comms.DONGLE_CACHE = None
    _comms.DONGLE_CONFIG_CACHE = None
    try:
        dongle.close()
    except Exception:
        pass
    return True, None


def _explain_ledger_error(e: Exception) -> str:
    """Map a ledgereth exception to a single, action-oriented
    sentence that tells the user what's wrong and what to do.
    Fallback for unknown error shapes is to surface the raw text
    along with the SW code when we have one — so future
    "UNKNOWN" cases stay diagnosable instead of getting hidden."""
    try:
        from ledgereth.exceptions import (
            CommException, LedgerAppNotOpened, LedgerCancel, LedgerError,
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
    fetched from `chain` if provided.
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
        try:
            from ledgereth.accounts import get_account_by_path
            from ledgereth.comms import init_dongle
        except ImportError as e:
            self.failed.emit(f"ledgereth not installed: {e}")
            return

        try:
            dongle = init_dongle()
        except Exception as e:
            self.failed.emit(_explain_ledger_error(e))
            return

        template = PATH_SCHEMES.get(self.scheme)
        if template is None:
            self.failed.emit(f"Unknown derivation scheme: {self.scheme}")
            return

        auto = self.count == 0
        max_scan = AUTO_DETECT_HARD_CAP if auto else self.count
        consecutive_zero = 0

        try:
            for i in range(max_scan):
                path = template.format(i=i)
                acct = get_account_by_path(path, dongle=dongle)
                nonce = self._nonce(acct.address) if self.client else 0
                self.discovered.emit(DiscoveredAccount(
                    address=acct.address, path=path, index=i, nonce=nonce
                ))
                if auto:
                    if nonce == 0:
                        consecutive_zero += 1
                        if consecutive_zero >= AUTO_STOP_CONSECUTIVE_ZEROS:
                            break
                    else:
                        consecutive_zero = 0
            self.finished_ok.emit()
        except Exception as e:
            # Same decoder as the init_dongle failure above —
            # surfaces friendly messages for known errors and the
            # hex SW for unknown ones, instead of the raw ledgereth
            # "Unexpected error: 0x???? UNKNOWN" leaking through.
            self.failed.emit(_explain_ledger_error(e))

    def _nonce(self, address: str) -> int:
        """Latest-block sent-tx count for ``address``. We ask for
        "latest", not "pending", because we want the canonical
        on-chain identity of the wallet — a pending tx in the
        mempool from another client could otherwise tip a fresh
        address into looking used."""
        try:
            return self.client.get_transaction_count(address, "latest")
        except Exception:
            return 0


class LedgerSigner(Signer):
    """``Signer`` implementation backed by a Ledger hardware wallet.

    Looks up the right derivation path for ``req.from_addr`` in the
    store's account list (where each Ledger-discovered account is
    recorded with its ``path``) and asks the dongle to sign — the
    user has to confirm on the device, so signing takes a few
    seconds and must be done off the Qt main thread."""

    def __init__(self, store):
        self._store = store

    def can_sign(self, address: str) -> bool:
        return self._lookup(address) is not None

    def _lookup(self, address: str) -> Optional[dict]:
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
        try:
            # ledgereth.transactions does its own dongle init when
            # no Dongle is passed; we don't reuse a handle here
            # because USB connects are cheap and signing isn't a
            # hot path.
            from ledgereth.transactions import create_transaction
        except ImportError as e:
            raise SignerError(f"ledgereth not installed: {e}") from e

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

        try:
            signed = create_transaction(**kwargs)
        except Exception as e:
            # Anything from a user rejecting on the device to USB
            # comms hiccups lands here — surface as SignerError so
            # the RPC handler sees a uniform shape. _explain_ledger_
            # error maps the ledgereth typed exceptions to friendly
            # sentences, and falls back to the raw status word for
            # codes ledgereth doesn't have a typed exception for.
            raise SignerError(_explain_ledger_error(e)) from e
        finally:
            # ledgereth caches the Dongle handle in a module-level
            # slot across calls. Reusing it between signs is
            # unreliable: the USB-HID session sometimes goes stale
            # (device briefly drops at the firmware layer; the
            # cached pyhidapi handle then yields "device not
            # connected" on the next exchange without ever waking
            # the device screen). Close + clear so the next sign
            # starts from a fresh init_dongle.
            from ledgereth import comms as _comms
            cached = _comms.DONGLE_CACHE
            _comms.DONGLE_CACHE = None
            _comms.DONGLE_CONFIG_CACHE = None
            if cached is not None:
                try:
                    cached.close()
                except Exception:
                    pass
        # ledgereth exposes the encoded signed tx in two shapes that
        # both have the name ``raw_transaction``-ish: an attribute
        # (``rawTransaction`` — a hex-string property) and a method
        # (``raw_transaction()``) on different versions. Don't grab
        # the method object as if it were the payload (web3 chokes
        # on the bound-method as transaction data with a generic
        # "expected … bytes" TypeError).
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

    def sign_message(self, req: MessageSigningRequest) -> bytes:
        """personal_sign on the Ledger. The device prompts the user
        to review the message (truncated on screen) and confirm —
        same UX as MetaMask's "Sign" popup."""
        path = self._require_path(req.from_addr)
        try:
            from ledgereth.messages import sign_message
        except ImportError as e:
            raise SignerError(f"ledgereth not installed: {e}") from e
        try:
            signed = sign_message(req.raw, sender_path=path)
        except Exception as e:
            raise SignerError(_explain_ledger_error(e)) from e
        finally:
            _clear_dongle_cache()
        return _extract_ledger_signature(signed)

    def sign_typed_data(self, req: TypedDataSigningRequest) -> bytes:
        """EIP-712 v4 — the Ledger Ethereum app does the full
        struct-hash walk on-device when the data is small enough,
        else falls back to a "blind sign" prompt the user must
        enable in the device settings."""
        path = self._require_path(req.from_addr)
        try:
            from ledgereth.messages import sign_typed_data_draft
        except ImportError as e:
            raise SignerError(f"ledgereth not installed: {e}") from e
        # ledgereth's draft signer expects the pre-computed domain
        # separator + message hash. Compute via eth_account so the
        # hashing matches what the dapp expects.
        try:
            from eth_account.messages import _hash_eip191_message, encode_typed_data
        except ImportError as e:
            raise SignerError(f"eth_account missing: {e}") from e
        try:
            signable = encode_typed_data(full_message=req.typed_data)
            # signable.body is the 64-byte (domain_hash || message_hash)
            # blob that the EIP-191 v0x01 prefix wraps. Split into
            # the two 32-byte halves for ledgereth.
            body = signable.body
            domain_hash = body[:32]
            message_hash = body[32:64]
        except Exception as e:
            raise SignerError(f"failed to hash typed data: {e}") from e
        try:
            signed = sign_typed_data_draft(
                domain_hash, message_hash, sender_path=path,
            )
        except Exception as e:
            raise SignerError(_explain_ledger_error(e)) from e
        finally:
            _clear_dongle_cache()
        return _extract_ledger_signature(signed)

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


def _clear_dongle_cache() -> None:
    """Mirror of the post-sign cleanup in LedgerSigner.sign — the
    USB-HID handle ledgereth caches gets stale between calls."""
    try:
        from ledgereth import comms as _comms
    except ImportError:
        return
    cached = _comms.DONGLE_CACHE
    _comms.DONGLE_CACHE = None
    _comms.DONGLE_CONFIG_CACHE = None
    if cached is not None:
        try:
            cached.close()
        except Exception:
            pass


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

