from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QThread, Signal

from .chain import EthClient
from .chains import Chain
from .signing import Signer, SignerError, SigningRequest


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
            self.failed.emit(
                "Could not open Ledger. Make sure the device is connected, "
                f"unlocked, and the Ethereum app is open.\n\n{e}"
            )
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
            self.failed.emit(f"Error reading account: {e}")

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
            # the RPC handler sees a uniform shape.
            raise SignerError(f"Ledger signing failed: {e}") from e
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

