"""``QRSigner`` — signs through an air-gapped QR wallet (Keystone / Keycard
Shell) over BC-UR + EIP-4527. Runs on the signing WORKER: it builds the unsigned
tx, hands the ``ur:eth-sign-request`` to ``ui.exchange_qr`` (which shows it as a
QR and reads the device's ``ur:eth-signature`` back — marshaled to the main
thread by ``DialogInteraction``), then assembles the signed raw tx.

Step 3b of docs/signers-qr.md. Signs EIP-1559 (typed) transactions, personal
messages (EIP-191), and EIP-712 typed data — all over the same eth-sign-request
exchange, differing only in the data-type + sign-data. Legacy (pre-2718) tx
signing lands in a follow-up.
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


def _recovery_v_27_28(signature: bytes) -> bytes:
    """Normalise a 65-byte signature's v to 27/28 — the personal_sign / EIP-712
    convention (eth-account emits it that way, and that's what verifiers/dapps
    expect). Some devices return the 0/1 recovery id; bump it so a QR-signed
    message verifies identically to a locally-signed one."""
    if len(signature) == 65 and signature[64] < 27:
        return signature[:64] + bytes([signature[64] + 27])
    return signature


def _typed_data_chain_id(typed_data: dict) -> int:
    """The EIP-712 domain's chainId as an int, default 1 — accepts int, decimal
    string, or 0x-hex, as dapps send it variously. The device hashes per this
    (from the JSON it parses); we mirror it in the request for consistency."""
    domain = typed_data.get("domain") if isinstance(typed_data, dict) else None
    cid = (domain or {}).get("chainId", 1)
    if isinstance(cid, str):
        return int(cid, 16) if cid.lower().startswith("0x") else int(cid)
    return int(cid)


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
        signature = self._request_signature(
            sign_data=unsigned,
            data_type=eth.DataType.TYPED_TRANSACTION,
            chain_id=chain.chain_id,
            from_addr=req.from_addr,
            origin=req.origin,
        )
        return self._assemble_eip1559(req, chain.chain_id, signature)

    def _request_signature(
        self, *, sign_data: bytes, data_type: int, chain_id: int,
        from_addr: str | None, origin: str | None,
    ) -> bytes:
        """Show the eth-sign-request QR (animated for a big payload), read the
        device's eth-signature back, and return its 65-byte ``r‖s‖v``. Shared by
        tx / message / typed-data signing — they differ only in ``sign_data`` and
        ``data_type``."""
        ur_type, payload, request_id = eth.encode_eth_sign_request(
            sign_data=sign_data,
            data_type=data_type,
            chain_id=chain_id,
            path=self._account["path"],
            source_fingerprint=self._source_fingerprint(),
            address=_addr_bytes(from_addr) or None,
            origin=origin,
        )
        response = self._ui.exchange_qr(multipart.frame_source(ur_type, payload))
        if response is None:
            raise SignerError("QR signing cancelled")
        sig = eth.parse_eth_signature(response)
        if sig.request_id and sig.request_id != request_id:
            raise SignerError(
                "scanned a signature for a different request — try again")
        return sig.signature

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
        """personal_sign — the device applies the EIP-191 prefix and hashes the
        raw message itself, so we hand it the bytes and return the 65-byte
        signature. Chain-agnostic; the request carries chain 1 as context."""
        signature = self._request_signature(
            sign_data=req.raw,
            data_type=eth.DataType.PERSONAL_MESSAGE,
            chain_id=1,
            from_addr=req.from_addr,
            origin=req.origin,
        )
        return _recovery_v_27_28(signature)

    def sign_typed_data(self, req: TypedDataSigningRequest) -> bytes:
        """EIP-712 — the device parses the typed-data JSON and builds the hash
        per its domain (incl. chainId). We serialize the full v4 object as the
        sign-data."""
        import json
        payload = json.dumps(req.typed_data, separators=(",", ":")).encode()
        signature = self._request_signature(
            sign_data=payload,
            data_type=eth.DataType.TYPED_DATA,
            chain_id=_typed_data_chain_id(req.typed_data),
            from_addr=req.from_addr,
            origin=req.origin,
        )
        return _recovery_v_27_28(signature)
