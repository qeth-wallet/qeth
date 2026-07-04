"""``QRSigner`` — signs through an air-gapped QR wallet (Keystone / Keycard
Shell) over BC-UR + EIP-4527. Runs on the signing WORKER: it builds the unsigned
tx, hands the ``ur:eth-sign-request`` to ``ui.exchange_qr`` (which shows it as a
QR and reads the device's ``ur:eth-signature`` back — marshaled to the main
thread by ``DialogInteraction``), then assembles the signed raw tx.

Step 3b of docs/signers-qr.md. First slice: EIP-1559 (typed) transactions;
message / typed-data / legacy-tx signing land in follow-ups.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import rlp

from .qr import eth, multipart
from .signing import (
    MessageSigningRequest,
    Signer,
    SignerError,
    SigningRequest,
    TypedDataSigningRequest,
)

if TYPE_CHECKING:
    from .chains import Chain
    from .signers.interaction import SignerInteraction


def _rlp_int(n: int) -> bytes:
    """A non-negative int as a minimal big-endian byte string (0 → empty)."""
    if n < 0:
        raise ValueError("cannot RLP-encode a negative integer")
    return b"" if n == 0 else n.to_bytes((n.bit_length() + 7) // 8, "big")


def _addr_bytes(addr: str | None) -> bytes:
    """A 0x address → 20 bytes; ``None`` / empty → ``b""`` (contract creation)."""
    if not addr:
        return b""
    return bytes.fromhex(addr[2:] if addr.startswith("0x") else addr)


def _data_bytes(data: str | None) -> bytes:
    if not data or data == "0x":
        return b""
    return bytes.fromhex(data[2:] if data.startswith("0x") else data)


def unsigned_eip1559(req: SigningRequest, chain_id: int) -> bytes:
    """The EIP-2718 unsigned serialization a device signs:
    ``0x02 ‖ rlp([chainId, nonce, maxPriorityFeePerGas, maxFeePerGas, gasLimit,
    to, value, data, accessList])`` (no access list in this first slice).
    Cross-checked against eth-account's ``hash()`` in the tests."""
    if (req.gas is None or req.nonce is None
            or req.max_fee_per_gas is None
            or req.max_priority_fee_per_gas is None):
        raise SignerError("gas, nonce and EIP-1559 fees must be set before signing")
    fields: list[Any] = [
        _rlp_int(chain_id),
        _rlp_int(req.nonce),
        _rlp_int(req.max_priority_fee_per_gas),
        _rlp_int(req.max_fee_per_gas),
        _rlp_int(req.gas),
        _addr_bytes(req.to_addr),
        _rlp_int(req.value_wei),
        _data_bytes(req.data),
        [],   # accessList
    ]
    return b"\x02" + rlp.encode(fields)


class QRSigner(Signer):
    """Signs for one air-gapped account. Built per-signing by ``QRSignerPlugin``
    with the account record (address / derivation path / master fingerprint) and
    the interaction host it drives from ``sign()``."""

    def __init__(self, account: dict[str, Any], ui: SignerInteraction) -> None:
        self._account = account
        self._ui = ui

    def can_sign(self, address: str) -> bool:
        return address.lower() == str(self._account.get("address", "")).lower()

    def _source_fingerprint(self) -> int:
        xfp = self._account.get("xfp", 0)
        if isinstance(xfp, str):
            return int(xfp, 16) if xfp.startswith("0x") else int(xfp)
        return int(xfp)

    def sign(self, req: SigningRequest, chain: Chain) -> bytes:
        if not getattr(chain, "eip1559", False):
            raise SignerError(
                "QR signing currently supports EIP-1559 chains only")
        unsigned = unsigned_eip1559(req, chain.chain_id)
        ur_type, payload, request_id = eth.encode_eth_sign_request(
            sign_data=unsigned,
            data_type=eth.DataType.TYPED_TRANSACTION,
            chain_id=chain.chain_id,
            path=self._account["path"],
            source_fingerprint=self._source_fingerprint(),
            address=_addr_bytes(req.from_addr) or None,
            origin=req.origin,
        )
        response = self._ui.exchange_qr(multipart.frame_source(ur_type, payload))
        if response is None:
            raise SignerError("QR signing cancelled")
        sig = eth.parse_eth_signature(response)
        if sig.request_id and sig.request_id != request_id:
            raise SignerError(
                "scanned a signature for a different request — try again")
        return self._assemble_eip1559(req, chain.chain_id, sig.signature)

    @staticmethod
    def _assemble_eip1559(
        req: SigningRequest, chain_id: int, signature: bytes,
    ) -> bytes:
        """Combine the unsigned tx fields with the device's (r, s, v) into the
        signed EIP-2718 raw tx. eth-account's encoder produces bytes identical to
        a locally-signed tx (verified in the probe)."""
        if len(signature) != 65:
            raise SignerError(
                f"expected a 65-byte signature, got {len(signature)}")
        r = int.from_bytes(signature[0:32], "big")
        s = int.from_bytes(signature[32:64], "big")
        v = signature[64]
        y_parity = v - 27 if v >= 27 else v   # normalise 27/28 → 0/1
        from eth_account.typed_transactions import TypedTransaction
        return TypedTransaction.from_dict({
            "type": 2,
            "chainId": chain_id,
            "nonce": req.nonce,
            "maxPriorityFeePerGas": req.max_priority_fee_per_gas,
            "maxFeePerGas": req.max_fee_per_gas,
            "gas": req.gas,
            "to": req.to_addr or "",
            "value": req.value_wei,
            "data": _data_bytes(req.data),
            "accessList": [],
            "v": y_parity, "r": r, "s": s,
        }).encode()

    def sign_message(self, req: MessageSigningRequest) -> bytes:
        raise SignerError("QR message signing lands in a follow-up")

    def sign_typed_data(self, req: TypedDataSigningRequest) -> bytes:
        raise SignerError("QR typed-data signing lands in a follow-up")
