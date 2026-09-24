"""Tron transactions, encoded locally.

A Tron transaction is a protobuf ``Transaction.raw`` holding exactly one
contract, a TaPoS reference to a recent block and an expiration. Its id is
``sha256(raw_data)`` and the signature is secp256k1 over that id. qeth builds
the bytes itself rather than taking ``raw_data_hex`` / ``txID`` from a node's
``createtransaction``: a node that answers with a different transaction would
otherwise get it signed blindly. What the user reviews is decoded from the
very bytes that get signed.

Only what qeth sends is modelled — ``TransferContract`` (TRX) and
``TriggerSmartContract`` (TRC-20 and other contract calls). Encoding follows
proto3 as java-tron serializes it: fields in number order, zero / empty
scalars omitted. Field numbers are java-tron's ``Tron.proto``,
``balance_contract.proto`` and ``smart_contract.proto``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import ClassVar

from ..address import TRON_PREFIX

# --- minimal proto3 wire format ------------------------------------------------

_VARINT, _I64, _LEN, _I32 = 0, 1, 2, 5


def _varint(n: int) -> bytes:
    if n < 0:
        raise ValueError(f"negative varint {n}")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint(field << 3 | wire)


def _int_field(field: int, n: int) -> bytes:
    """A varint field — omitted when zero, as proto3 does."""
    return _key(field, _VARINT) + _varint(n) if n else b""


def _bytes_field(field: int, data: bytes) -> bytes:
    """A length-delimited field — omitted when empty, as proto3 does."""
    return _key(field, _LEN) + _varint(len(data)) + data if data else b""


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def read_fields(buf: bytes) -> list[tuple[int, int, int | bytes]]:
    """Every ``(field, wire type, value)`` in one message, in wire order —
    ints for varint / fixed fields, bytes for length-delimited ones."""
    out: list[tuple[int, int, int | bytes]] = []
    i = 0
    while i < len(buf):
        k, i = _read_varint(buf, i)
        field, wire = k >> 3, k & 7
        val: int | bytes
        if wire == _VARINT:
            val, i = _read_varint(buf, i)
        elif wire == _LEN:
            n, i = _read_varint(buf, i)
            if i + n > len(buf):
                raise ValueError("truncated field")
            val, i = buf[i:i + n], i + n
        elif wire == _I64:
            val, i = int.from_bytes(buf[i:i + 8], "little"), i + 8
        elif wire == _I32:
            val, i = int.from_bytes(buf[i:i + 4], "little"), i + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.append((field, wire, val))
    return out


# --- addresses on the wire ------------------------------------------------------

def _addr21(addr: str) -> bytes:
    """Internal ``0x`` + 20-byte address → the 21-byte ``41…`` wire form."""
    body = bytes.fromhex(addr[2:])
    if len(body) != 20:
        raise ValueError(f"not a 20-byte address: {addr!r}")
    return bytes([TRON_PREFIX]) + body


def _from_addr21(raw: bytes) -> str:
    if len(raw) != 21 or raw[0] != TRON_PREFIX:
        raise ValueError(f"not a Tron address: {raw.hex()}")
    return "0x" + raw[1:].hex()


# --- contracts ------------------------------------------------------------------

TYPE_URL_PREFIX = "type.googleapis.com/protocol."


@dataclass(frozen=True)
class TransferContract:
    """Send ``amount`` sun of TRX from ``owner`` to ``to``."""
    owner: str
    to: str
    amount: int

    TYPE: ClassVar[int] = 1
    NAME: ClassVar[str] = "TransferContract"

    def encode(self) -> bytes:
        return (_bytes_field(1, _addr21(self.owner))
                + _bytes_field(2, _addr21(self.to))
                + _int_field(3, self.amount))

    @classmethod
    def decode(cls, value: bytes) -> TransferContract:
        f = {n: v for n, _, v in read_fields(value)}
        owner, to = f.get(1), f.get(2)
        if not isinstance(owner, bytes) or not isinstance(to, bytes):
            raise ValueError("TransferContract without owner / to")
        amount = f.get(3, 0)
        assert isinstance(amount, int)
        return cls(_from_addr21(owner), _from_addr21(to), amount)


@dataclass(frozen=True)
class TriggerSmartContract:
    """Call ``contract`` with ABI calldata ``data`` from ``owner``, sending
    ``call_value`` sun of TRX along."""
    owner: str
    contract: str
    data: bytes
    call_value: int = 0

    TYPE: ClassVar[int] = 31
    NAME: ClassVar[str] = "TriggerSmartContract"

    def encode(self) -> bytes:
        return (_bytes_field(1, _addr21(self.owner))
                + _bytes_field(2, _addr21(self.contract))
                + _int_field(3, self.call_value)
                + _bytes_field(4, self.data))

    @classmethod
    def decode(cls, value: bytes) -> TriggerSmartContract:
        fields = read_fields(value)
        f = {n: v for n, _, v in fields}
        if any(n in (5, 6) for n, _, _ in fields):
            raise ValueError("TRC-10 token calls are not supported")
        owner, contract = f.get(1), f.get(2)
        if not isinstance(owner, bytes) or not isinstance(contract, bytes):
            raise ValueError("TriggerSmartContract without owner / contract")
        data = f.get(4, b"")
        call_value = f.get(3, 0)
        assert isinstance(data, bytes) and isinstance(call_value, int)
        return cls(_from_addr21(owner), _from_addr21(contract), data, call_value)


Contract = TransferContract | TriggerSmartContract
_CONTRACTS: dict[int, type[TransferContract] | type[TriggerSmartContract]] = {
    TransferContract.TYPE: TransferContract,
    TriggerSmartContract.TYPE: TriggerSmartContract,
}


# --- the transaction -------------------------------------------------------------

@dataclass(frozen=True)
class TronTx:
    """An unsigned Tron transaction. ``expiration`` / ``timestamp`` are unix
    milliseconds; ``fee_limit`` (sun) caps the energy a contract call may
    burn and is unused by a plain transfer; ``memo`` is the ``data`` field
    (costs an extra 1 TRX when set)."""
    contract: Contract
    ref_block_bytes: bytes
    ref_block_hash: bytes
    expiration: int
    timestamp: int
    fee_limit: int = 0
    memo: bytes = b""

    @property
    def owner(self) -> str:
        return self.contract.owner

    def raw_data(self) -> bytes:
        c = self.contract
        any_msg = (_bytes_field(1, (TYPE_URL_PREFIX + c.NAME).encode())
                   + _bytes_field(2, c.encode()))
        contract_msg = _int_field(1, c.TYPE) + _bytes_field(2, any_msg)
        return (_bytes_field(1, self.ref_block_bytes)
                + _bytes_field(4, self.ref_block_hash)
                + _int_field(8, self.expiration)
                + _bytes_field(10, self.memo)
                + _bytes_field(11, contract_msg)
                + _int_field(14, self.timestamp)
                + _int_field(18, self.fee_limit))

    def txid(self) -> bytes:
        return txid(self.raw_data())


def txid(raw_data: bytes) -> bytes:
    """A Tron transaction id: sha256 of the raw_data bytes."""
    return hashlib.sha256(raw_data).digest()


def decode_raw(raw: bytes) -> TronTx:
    """Parse raw_data bytes back into a ``TronTx`` — the inverse of
    ``TronTx.raw_data`` for what qeth builds. Raises ValueError on anything
    else (several contracts, a contract type we don't model, multisig
    fields), so a review screen never shows a partial reading of a
    transaction."""
    fields = read_fields(raw)
    unknown = {n for n, _, _ in fields} - {1, 3, 4, 8, 10, 11, 14, 18}
    if unknown:
        raise ValueError(f"unsupported raw_data fields {sorted(unknown)}")
    contracts = [v for n, _, v in fields if n == 11]
    if len(contracts) != 1 or not isinstance(contracts[0], bytes):
        raise ValueError("a Tron transaction must carry exactly one contract")
    cf = read_fields(contracts[0])
    if {n for n, _, _ in cf} - {1, 2}:
        raise ValueError("permissioned (multisig) contracts are not supported")
    cmap = {n: v for n, _, v in cf}
    ctype, any_msg = cmap.get(1, 0), cmap.get(2)
    if not isinstance(ctype, int) or not isinstance(any_msg, bytes):
        raise ValueError("malformed contract")
    cls = _CONTRACTS.get(ctype)
    if cls is None:
        raise ValueError(f"unsupported contract type {ctype}")
    amap = {n: v for n, _, v in read_fields(any_msg)}
    type_url, value = amap.get(1, b""), amap.get(2, b"")
    assert isinstance(type_url, bytes) and isinstance(value, bytes)
    if type_url.decode() != TYPE_URL_PREFIX + cls.NAME:
        raise ValueError(f"type_url {type_url!r} doesn't match contract type {ctype}")
    f = {n: v for n, _, v in fields}

    def _b(n: int) -> bytes:
        v = f.get(n, b"")
        assert isinstance(v, bytes)
        return v

    def _i(n: int) -> int:
        v = f.get(n, 0)
        assert isinstance(v, int)
        return v

    return TronTx(
        contract=cls.decode(value),
        ref_block_bytes=_b(1), ref_block_hash=_b(4),
        expiration=_i(8), timestamp=_i(14), fee_limit=_i(18), memo=_b(10))


def signed_transaction(raw_data: bytes, signatures: list[bytes]) -> bytes:
    """The broadcastable ``Transaction`` message: raw_data (1) + signatures
    (2, repeated) — what ``/wallet/broadcasthex`` takes, hex-encoded."""
    out = _bytes_field(1, raw_data)
    for sig in signatures:
        out += _bytes_field(2, sig)
    return out


def ref_block(number: int, block_id: str) -> tuple[bytes, bytes]:
    """TaPoS fields for referencing a block: ``ref_block_bytes`` = bytes
    [6:8] of the 8-byte big-endian block number, ``ref_block_hash`` = bytes
    [8:16] of the 32-byte block id (whose first 8 bytes are the number)."""
    bid = bytes.fromhex(block_id)
    if len(bid) != 32:
        raise ValueError(f"not a 32-byte block id: {block_id!r}")
    return number.to_bytes(8, "big")[6:8], bid[8:16]


# --- calldata ------------------------------------------------------------------

def strip_address_prefixes(calldata: str) -> str:
    """Calldata (``0x`` hex) with Tron's ``41`` address prefix cleared from
    every 32-byte argument word that holds one. Some Tron wallets ABI-encode
    an address as its 21-byte ``41…`` form; the TVM (and old TRC-20s like
    USDT) mask it back to 20 bytes, but a strict ABI decoder rejects the
    dirty word — and then a colliding 4-byte signature "wins" the decode
    (a9059cbb as ``workMyDirefulOwner(uint256,uint256)``). A word of exactly
    ``00×11 41`` + 20 bytes is a Tron address, not a plausible amount."""
    if not calldata.startswith("0x") or len(calldata) < 10:
        return calldata
    head, body = calldata[:10], calldata[10:]
    words = [body[i:i + 64] for i in range(0, len(body), 64)]
    prefix = "00" * 11 + "41"
    return head + "".join(
        "00" * 12 + w[24:] if len(w) == 64 and w.lower().startswith(prefix) else w
        for w in words)


# --- signatures ------------------------------------------------------------------

def signature_v27(signature: bytes) -> bytes:
    """A 65-byte ``r‖s‖v`` signature with v as 27/28 — TronWeb's and Trezor's
    convention (java-tron accepts 0/1 too; one form keeps comparisons sane)."""
    if len(signature) != 65:
        raise ValueError(f"expected a 65-byte signature, got {len(signature)}")
    v = signature[64]
    return signature[:64] + bytes([v + 27 if v < 27 else v])


def recover_signer(tx_id: bytes, signature: bytes) -> str:
    """The internal ``0x`` address whose key made ``signature`` over
    ``tx_id``. Used to check every signature before broadcast — a device or
    wallet that signed with the wrong key (or other bytes) is caught here,
    not by a node rejection after the fact."""
    from eth_keys import keys
    v = signature[64]
    sig = keys.Signature(signature[:64] + bytes([v - 27 if v >= 27 else v]))
    return "0x" + sig.recover_public_key_from_msg_hash(tx_id).to_canonical_address().hex()
