"""EIP-712 typed data as the Ledger Ethereum app's full-message commands (M5
of docs/ledger-clear-signing.md).

With these the device hashes the message itself and lists its fields, instead
of signing two opaque hashes (the V0 mode, ``EthApp.sign_eip712_hashes``,
which stays for the Nano S and old apps). ``parse_typed_data`` turns the
dapp's JSON into one normalised message — the struct schema plus a value tree
whose leaves are already the device's byte encodings — and both wire formats
are built from it:

- **V1** (app 1.9.19+): one command per struct, field and value. The review
  streams: the device shows each value as it arrives and answers only once
  the user has scrolled past it.
- **V2** (app 1.24+): the schema and the values as two TLV payloads. The
  device checks the whole message before it shows anything.

The values follow eth_account's reading of the JSON (``encode_typed_data``),
which gives the hash the signature is recovered against, so a message the
device hashed differently is refused, never returned as signed. What that
reading accepts but the device can't carry (a missing value, an unsized
``uint``, a non-ASCII field name, an array of more than 255 entries) raises
``Eip712Unsupported``, and the caller signs the hashes instead.

Qt-free; encodings from app-ethereum ``doc/apdu.md``, ``doc/eip712.md`` and
``doc/tlv_structs.md`` (1.24.0).
"""

from __future__ import annotations

import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

from .ledger_app import MAX_APDU_DATA, apdu, pack_path

INS_EIP712_SEND_STRUCT_DEF = 0x1A
INS_EIP712_SEND_STRUCT_IMPL = 0x1C
INS_EIP712_SIGN = 0x0C

# SIGN ETH EIP 712's P2: the version of the flow.
P2_V1 = 0x01
P2_V2 = 0x02
# V1 sub-commands (P2 of the definition / implementation commands).
_V1_STRUCT_NAME = 0x00
_V1_ARRAY = 0x0F
_V1_FIELD = 0xFF
_V1_COMPLETE = 0x00
_V1_PARTIAL = 0x01
# V2 payload chunks (P1).
_V2_FIRST_CHUNK = 0x01
_V2_NEXT_CHUNK = 0x00

# Solidity base types, as both versions number them.
STRUCT, INT, UINT, ADDRESS, BOOL, STRING, BYTES_FIX, BYTES_DYN = range(8)

DOMAIN = "EIP712Domain"
# eth_account builds the domain's type from the keys the domain has, in this
# order, whatever ``types.EIP712Domain`` says — so the device gets the same.
_DOMAIN_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "string"), ("version", "string"), ("chainId", "uint256"),
    ("verifyingContract", "address"), ("salt", "bytes32"),
)

# Device limits (doc/eip712.md "Implementation limits"): V1 sends an array
# length and a fixed dimension in one byte, V2 caps both at 255 too.
_MAX_ARRAY = 255
_MAX_DIMS = 8
_MAX_DEPTH = 16
_MAX_NAME = 255
_MAX_LEAF = 0xFFFF       # V1's value length is 2 bytes
_MAX_TLV = 0xFFFF        # V2's payload length is 2 bytes

_ARRAY_SUFFIX = re.compile(r"^(.*)\[(\d*)\]$")
_INT_TYPE = re.compile(r"^(u?)int(\d+)$")
_BYTES_TYPE = re.compile(r"^bytes(\d+)$")
_HEX = re.compile(r"^0x[0-9a-fA-F]*$")
_ADDRESS = re.compile(r"^0[xX][0-9a-fA-F]{40}$")


class Eip712Unsupported(ValueError):
    """The message can't go to the device whole; sign its hashes instead."""


@dataclass(frozen=True)
class Eip712Field:
    name: str
    type: int
    size: int | None = None              # bytes: intN / uintN → N/8, bytesN → N
    dims: tuple[int | None, ...] = ()     # as written, innermost first; None = dynamic
    struct: str | None = None             # the referenced struct, for STRUCT


# A leaf is the device's encoding of a scalar; a list is a struct instance
# (one entry per field, in schema order) or an array (one per element).
Value: TypeAlias = "bytes | list[Value]"


@dataclass(frozen=True)
class Eip712Message:
    structs: dict[str, tuple[Eip712Field, ...]]   # EIP712Domain first
    primary_type: str
    domain: list[Value]
    message: list[Value]


# --- JSON → message ----------------------------------------------------------


def parse_typed_data(typed: Mapping[str, Any]) -> Eip712Message:
    """The dapp's ``eth_signTypedData_v4`` JSON as the device will hash it.
    Call it on a message eth_account has already hashed (it doesn't repeat
    eth_account's validation, only what the device needs on top)."""
    try:
        types = typed["types"]
        domain = typed["domain"]
        message = typed["message"]
        primary = typed["primaryType"]
    except (KeyError, TypeError) as e:
        raise Eip712Unsupported(f"typed data without {e}") from e
    if not isinstance(types, Mapping) or not isinstance(domain, Mapping):
        raise Eip712Unsupported("malformed typed data")
    names = {name for name in types if name != DOMAIN}
    structs: dict[str, tuple[Eip712Field, ...]] = {
        DOMAIN: tuple(_field(name, type_, names)
                      for name, type_ in _DOMAIN_FIELDS if name in domain)}
    for name, fields in types.items():
        if name == DOMAIN:
            continue
        _check_name(name, struct=True)
        if not isinstance(fields, list):
            raise Eip712Unsupported(f"struct {name} isn't a list of fields")
        parsed = tuple(_field(f.get("name"), f.get("type"), names)
                       for f in fields if isinstance(f, Mapping))
        if len(parsed) != len(fields):
            raise Eip712Unsupported(f"struct {name} has a malformed field")
        if len({f.name for f in parsed}) != len(parsed):
            raise Eip712Unsupported(f"struct {name} repeats a field name")
        structs[name] = parsed
    if primary not in names:
        raise Eip712Unsupported(f"primary type {primary!r} isn't defined")
    if not isinstance(message, Mapping):
        raise Eip712Unsupported("the message isn't a struct")
    return Eip712Message(
        structs=structs, primary_type=primary,
        domain=_struct_value(structs, DOMAIN, domain, 0),
        message=_struct_value(structs, primary, message, 0))


def _check_name(name: object, *, struct: bool) -> None:
    """Names the device accepts and can show: printable ASCII with a visible
    character, none of the characters that delimit ``encodeType``."""
    if not isinstance(name, str) or not name.strip() or len(name) > _MAX_NAME:
        raise Eip712Unsupported(f"name {name!r} can't be shown on the device")
    if any(not " " <= c <= "~" for c in name):
        raise Eip712Unsupported(f"name {name!r} isn't printable ASCII")
    if any(c in name for c in (" ()[]," if struct else "(),")):
        raise Eip712Unsupported(f"name {name!r} holds a type delimiter")


def _field(name: object, type_: object, structs: set[str]) -> Eip712Field:
    _check_name(name, struct=False)
    assert isinstance(name, str)
    if not isinstance(type_, str):
        raise Eip712Unsupported(f"field {name} has no type")
    base = type_
    dims: list[int | None] = []
    while (m := _ARRAY_SUFFIX.match(base)) is not None:
        base = m.group(1)
        dims.insert(0, int(m.group(2)) if m.group(2) else None)
    if len(dims) > _MAX_DIMS or any(d is not None and d > _MAX_ARRAY for d in dims):
        raise Eip712Unsupported(f"field {name} has arrays the device can't hold")
    kind, size, ref = _base_type(name, base, structs)
    return Eip712Field(name=name, type=kind, size=size, dims=tuple(dims),
                       struct=ref)


def _base_type(name: str, base: str,
               structs: set[str]) -> tuple[int, int | None, str | None]:
    # A struct name wins over a built-in name, as in eth_account.
    if base in structs:
        return STRUCT, None, base
    simple = {"address": ADDRESS, "bool": BOOL, "string": STRING,
              "bytes": BYTES_DYN}.get(base)
    if simple is not None:
        return simple, None, None
    if (m := _INT_TYPE.match(base)) is not None:
        bits = int(m.group(2))
        if bits % 8 == 0 and 8 <= bits <= 256:
            return (UINT if m.group(1) else INT), bits // 8, None
    elif (m := _BYTES_TYPE.match(base)) is not None and 1 <= int(m.group(1)) <= 32:
        return BYTES_FIX, int(m.group(1)), None
    raise Eip712Unsupported(f"field {name} has type {base}")


def _struct_value(structs: dict[str, tuple[Eip712Field, ...]], name: str,
                  value: Mapping[str, Any], depth: int) -> list[Value]:
    if depth > _MAX_DEPTH:
        raise Eip712Unsupported("the message nests deeper than the device goes")
    return [_value(structs, f, value.get(f.name), len(f.dims), depth + 1)
            for f in structs[name]]


def _value(structs: dict[str, tuple[Eip712Field, ...]], field: Eip712Field,
           value: Any, levels: int, depth: int) -> Value:
    if value is None:
        # eth_account hashes a missing struct as zeros and a missing string
        # as an empty bytes32: nothing the device could be sent.
        raise Eip712Unsupported(f"no value for {field.name}")
    if levels:
        size = field.dims[levels - 1]
        if (not isinstance(value, list) or len(value) > _MAX_ARRAY
                or (size is not None and len(value) != size)):
            raise Eip712Unsupported(f"{field.name} isn't an array the device takes")
        if depth > _MAX_DEPTH:
            raise Eip712Unsupported("the message nests deeper than the device goes")
        return [_value(structs, field, v, levels - 1, depth + 1) for v in value]
    if field.type == STRUCT:
        if not isinstance(value, Mapping):
            raise Eip712Unsupported(f"{field.name} isn't a struct")
        assert field.struct is not None
        return _struct_value(structs, field.struct, value, depth)
    leaf = _leaf(field, value)
    if len(leaf) > _MAX_LEAF:
        raise Eip712Unsupported(f"{field.name} is too long for the device")
    return leaf


def _leaf(field: Eip712Field, value: Any) -> bytes:
    """One scalar in the device's single encoding (doc/eip712.md "Leaf
    encoding"), read the way eth_account reads it."""
    t = field.type
    if t in (INT, UINT):
        assert field.size is not None
        n = _to_int(field.name, value)
        bits = field.size * 8
        low, high = ((-(1 << (bits - 1)), 1 << (bits - 1)) if t == INT
                     else (0, 1 << bits))
        if not low <= n < high:
            raise Eip712Unsupported(f"{field.name} is out of range")
        if n < 0:
            return n.to_bytes(field.size, "big", signed=True)
        return n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")
    if t == ADDRESS:
        if isinstance(value, bytes) and len(value) == 20:
            return value
        if isinstance(value, str) and _ADDRESS.match(value):
            return bytes.fromhex(value[2:])
        raise Eip712Unsupported(f"{field.name} isn't an address")
    if t == BOOL:
        return b"\x00" if not value or value in ("False", "false", "0") else b"\x01"
    if t == STRING:
        if not isinstance(value, str):
            raise Eip712Unsupported(f"{field.name} isn't a string")
        return value.encode()
    data = _to_bytes(field.name, value)
    if t == BYTES_FIX:
        assert field.size is not None
        if len(data) > field.size:
            raise Eip712Unsupported(f"{field.name} is longer than bytes{field.size}")
        return data.ljust(field.size, b"\x00")     # as eth_abi pads bytesN
    return data


def _to_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise Eip712Unsupported(f"{name} isn't an integer")
    if isinstance(value, int):
        return value
    try:
        if _HEX.match(value):
            return int(value[2:], 16)
        return int(value)
    except ValueError as e:
        raise Eip712Unsupported(f"{name} isn't an integer") from e


def _to_bytes(name: str, value: Any) -> bytes:
    """eth_account's reading of a ``bytes``/``bytesN`` value: hex when
    ``0x``-prefixed, else the text's UTF-8; an integer as big-endian."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        if _HEX.match(value):
            digits = value[2:]
            return bytes.fromhex("0" * (len(digits) % 2) + digits)
        return value.encode()
    if isinstance(value, bool):
        return b"\x01" if value else b"\x00"
    if isinstance(value, int):
        n = max(value, 0)
        return n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")
    raise Eip712Unsupported(f"{name} isn't bytes")


# --- V1: one command per struct, field and value ------------------------------


def v1_commands(msg: Eip712Message, path: str) -> list[bytes]:
    """Every V1 command, the signature request last."""
    out = [*_v1_definitions(msg)]
    for name, values in ((DOMAIN, msg.domain), (msg.primary_type, msg.message)):
        out.append(apdu(INS_EIP712_SEND_STRUCT_IMPL, _V1_COMPLETE,
                        _V1_STRUCT_NAME, name.encode()))
        _v1_struct(out, msg, name, values)
    out.append(apdu(INS_EIP712_SIGN, 0, P2_V1, pack_path(path)))
    return out


def _u8_text(text: str) -> bytes:
    data = text.encode("ascii")
    return bytes([len(data)]) + data


def _v1_definitions(msg: Eip712Message) -> list[bytes]:
    out = []
    for name, fields in msg.structs.items():
        out.append(apdu(INS_EIP712_SEND_STRUCT_DEF, 0, _V1_STRUCT_NAME,
                        name.encode()))
        for f in fields:
            desc = ((0x80 if f.dims else 0) | (0x40 if f.size is not None else 0)
                    | f.type)
            data = bytearray([desc])
            if f.struct is not None:
                data += _u8_text(f.struct)
            if f.size is not None:
                data.append(f.size)
            if f.dims:
                data.append(len(f.dims))
                for d in f.dims:
                    data += b"\x00" if d is None else bytes([1, d])
            data += _u8_text(f.name)
            if len(data) > MAX_APDU_DATA:
                raise Eip712Unsupported(f"field {f.name}'s definition is too long")
            out.append(apdu(INS_EIP712_SEND_STRUCT_DEF, 0, _V1_FIELD, bytes(data)))
    return out


def _v1_struct(out: list[bytes], msg: Eip712Message, name: str,
               values: list[Value]) -> None:
    for f, v in zip(msg.structs[name], values, strict=True):
        _v1_value(out, msg, f, v, len(f.dims))


def _v1_value(out: list[bytes], msg: Eip712Message, field: Eip712Field,
              value: Value, levels: int) -> None:
    if levels:
        assert isinstance(value, list)
        out.append(apdu(INS_EIP712_SEND_STRUCT_IMPL, _V1_COMPLETE, _V1_ARRAY,
                        bytes([len(value)])))
        for v in value:
            _v1_value(out, msg, field, v, levels - 1)
    elif field.type == STRUCT:
        assert isinstance(value, list) and field.struct is not None
        _v1_struct(out, msg, field.struct, value)
    else:
        assert isinstance(value, bytes)
        data = struct.pack(">H", len(value)) + value
        chunks = [data[i:i + MAX_APDU_DATA]
                  for i in range(0, len(data), MAX_APDU_DATA)]
        for i, chunk in enumerate(chunks):
            p1 = _V1_PARTIAL if i < len(chunks) - 1 else _V1_COMPLETE
            out.append(apdu(INS_EIP712_SEND_STRUCT_IMPL, p1, _V1_FIELD, chunk))


# --- V2: the schema and the values as two TLV payloads ------------------------


def _der(n: int) -> bytes:
    """A TLV tag or length: one byte below 0x80, else 0x80|count ‖ bytes."""
    data = n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")
    return data if n < 0x80 else bytes([0x80 | len(data)]) + data


def _tlv(tag: int, value: bytes) -> bytes:
    return _der(tag) + _der(len(value)) + value


def _uint(n: int) -> bytes:
    return n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")


_VERSION = _tlv(0x00, b"\x01")


def schema_tlv(msg: Eip712Message) -> bytes:
    """``EIP712_SCHEMA``: every struct, ``EIP712Domain`` first."""
    out = bytearray(_VERSION)
    for name, fields in msg.structs.items():
        body = bytearray(_VERSION + _tlv(0x01, name.encode()))
        for f in fields:
            fb = _VERSION + _tlv(0x01, f.name.encode()) + _tlv(0x02, bytes([f.type]))
            if f.size is not None:
                fb += _tlv(0x03, bytes([f.size]))
            for d in f.dims:
                fb += _tlv(0x04, b"" if d is None else _uint(d))
            if f.struct is not None:
                fb += _tlv(0x05, f.struct.encode())
            body += _tlv(0x02, fb)
        out += _tlv(0x01, bytes(body))
    return bytes(out)


def _seq(values: list[Value]) -> bytes:
    return b"".join(_tlv(0x01, _seq(v)) if isinstance(v, list) else _tlv(0x00, v)
                    for v in values)


def values_tlv(msg: Eip712Message, path: str) -> bytes:
    """``EIP712_VALUES``: the signing path, then the domain and the message."""
    return (_VERSION + _tlv(0x01, msg.primary_type.encode())
            + _tlv(0x02, pack_path(path)[1:])
            + _tlv(0x03, _seq(msg.domain)) + _tlv(0x04, _seq(msg.message)))


def _v2_payload(ins: int, tlv: bytes) -> list[bytes]:
    if len(tlv) > _MAX_TLV:
        raise Eip712Unsupported("the message is too large for the device")
    data = struct.pack(">H", len(tlv)) + tlv
    return [apdu(ins, _V2_FIRST_CHUNK if i == 0 else _V2_NEXT_CHUNK, P2_V2,
                 data[i:i + MAX_APDU_DATA])
            for i in range(0, len(data), MAX_APDU_DATA)]


def v2_commands(msg: Eip712Message, path: str) -> list[bytes]:
    """Every V2 command, the signature request (which starts the review) last."""
    return [*_v2_payload(INS_EIP712_SEND_STRUCT_DEF, schema_tlv(msg)),
            *_v2_payload(INS_EIP712_SEND_STRUCT_IMPL, values_tlv(msg, path)),
            apdu(INS_EIP712_SIGN, 0, P2_V2)]
