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
- **V1 with Ledger's filters** (app 1.12+, M5b): for a message Ledger
  describes, its signed name and field labels go along, and the app reviews it
  through them instead of as blind signing (``filtered_v1_commands``).

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

import hashlib
import json
import re
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeAlias

from .eip712_values import (
    DOMAIN, DOMAIN_FIELDS, Eip712Unsupported, read_address, read_bool,
    read_bytes, read_int,
)
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
                      for name, type_ in DOMAIN_FIELDS if name in domain)}
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
        n = read_int(field.name, value)
        bits = field.size * 8
        low, high = ((-(1 << (bits - 1)), 1 << (bits - 1)) if t == INT
                     else (0, 1 << bits))
        if not low <= n < high:
            raise Eip712Unsupported(f"{field.name} is out of range")
        if n < 0:
            return n.to_bytes(field.size, "big", signed=True)
        return n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")
    if t == ADDRESS:
        return read_address(field.name, value)
    if t == BOOL:
        return b"\x01" if read_bool(value) else b"\x00"
    if t == STRING:
        if not isinstance(value, str):
            raise Eip712Unsupported(f"{field.name} isn't a string")
        return value.encode()
    data = read_bytes(field.name, value)
    if t == BYTES_FIX:
        assert field.size is not None
        if len(data) > field.size:
            raise Eip712Unsupported(f"{field.name} is longer than bytes{field.size}")
        return data.ljust(field.size, b"\x00")     # as eth_abi pads bytesN
    return data


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


def _v1_definitions(msg: Eip712Message, *, by_name: bool = False) -> list[bytes]:
    """The struct definitions, in the message's order or (``by_name``, as
    Ledger Live sends them for a filtered message) sorted by name."""
    out = []
    structs = sorted(msg.structs.items()) if by_name else msg.structs.items()
    for name, fields in structs:
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


# A value's place in the message as Ledger's filters name it: the field names
# from the primary type down, "[]" for each array level ("details.token",
# "witness.outputs.[].token").
_Path: TypeAlias = "tuple[str, ...]"


def _v1_struct(out: list[bytes], msg: Eip712Message, name: str,
               values: list[Value], path: _Path = (),
               filters: Mapping[str, FieldFilter] | None = None) -> None:
    for f, v in zip(msg.structs[name], values, strict=True):
        _v1_value(out, msg, f, v, len(f.dims), (*path, f.name), filters)


def _v1_value(out: list[bytes], msg: Eip712Message, field: Eip712Field,
              value: Value, levels: int, path: _Path,
              filters: Mapping[str, FieldFilter] | None) -> None:
    if levels:
        assert isinstance(value, list)
        out.append(apdu(INS_EIP712_SEND_STRUCT_IMPL, _V1_COMPLETE, _V1_ARRAY,
                        bytes([len(value)])))
        if filters and not value:
            _v1_discard(out, path, filters)
        for v in value:
            _v1_value(out, msg, field, v, levels - 1, (*path, "[]"), filters)
    elif field.type == STRUCT:
        assert isinstance(value, list) and field.struct is not None
        _v1_struct(out, msg, field.struct, value, path, filters)
    else:
        assert isinstance(value, bytes)
        # A field's filter comes right before its value (never between the
        # chunks of one).
        f = filters.get(".".join(path)) if filters else None
        if f is not None:
            out.append(_filter_command(f))
        data = struct.pack(">H", len(value)) + value
        chunks = [data[i:i + MAX_APDU_DATA]
                  for i in range(0, len(data), MAX_APDU_DATA)]
        for i, chunk in enumerate(chunks):
            p1 = _V1_PARTIAL if i < len(chunks) - 1 else _V1_COMPLETE
            out.append(apdu(INS_EIP712_SEND_STRUCT_IMPL, p1, _V1_FIELD, chunk))


def _v1_discard(out: list[bytes], path: _Path,
                filters: Mapping[str, FieldFilter]) -> None:
    """An empty array: the filters of the fields inside it, each announced as
    discarded (app 1.12+), so the device still counts every filter."""
    inside = ".".join((*path, "[]"))
    for p, f in filters.items():
        if p == inside or p.startswith(inside + "."):
            data = p.encode()
            out.append(apdu(INS_EIP712_FILTERING, 0, _FILTER_DISCARDED_PATH,
                            bytes([len(data)]) + data))
            out.append(_filter_command(f, discarded=True))


# --- V1 with Ledger's filters: the message reviewed with Ledger's labels -----
#
# Ledger signs, per (chain, verifying contract, schema), a name for the message
# and a label and format for each field to show ("filters", app-ethereum
# doc/apdu.md EIP712 FILTERING). With them the app reviews the message
# through those labels, hides the other fields, and doesn't count it as blind
# signing. qeth ships a frozen snapshot of Ledger's set
# (``ledger_metadata.Eip712FilterSet``) and looks the message up locally, so no
# server learns what is signed. Filters are V1-only: a V2 message is described
# by ERC-7730 descriptors Ledger doesn't publish.

INS_EIP712_FILTERING = 0x1E
INS_PROVIDE_ERC20 = 0x0A
_FILTER_ACTIVATE = 0x00
_FILTER_DISCARDED_PATH = 0x01
_FILTER_MESSAGE_INFO = 0x0F
_FILTER_DATETIME = 0xFC
_FILTER_AMOUNT_JOIN_TOKEN = 0xFD
_FILTER_AMOUNT_JOIN_VALUE = 0xFE
_FILTER_RAW = 0xFF
_FILTER_STANDARD, _FILTER_DISCARDED = 0x00, 0x01      # P1 of a filter

# The amount-join id that takes the token from the domain's verifyingContract
# (an ERC-2612 permit, signed by the token itself).
JOIN_VERIFYING_CONTRACT = 0xFF
FILTER_FORMATS = ("raw", "datetime", "token", "amount")

# The first byte of each signed payload, so a signature of one kind can't pass
# as another.
_MAGIC_MESSAGE_INFO = 183
_MAGIC_AMOUNT_JOIN_TOKEN = 11
_MAGIC_AMOUNT_JOIN_VALUE = 22
_MAGIC_DATETIME = 33
_MAGIC_RAW = 72


@dataclass(frozen=True)
class FieldFilter:
    """How the device shows one message field (a leaf): under ``label``, as
    the raw value, a date, the token of an amount-join, or the amount."""

    path: str
    label: str
    format: str                 # one of FILTER_FORMATS
    coin_ref: int | None        # the amount-join id, for "token" / "amount"
    signature: bytes            # Ledger's DER ECDSA


@dataclass(frozen=True)
class MessageFilters:
    """Ledger's filters for one message schema on one contract."""

    chain_id: int
    contract: str               # lowercase 0x-hex
    schema_hash: bytes          # ``schema_hash`` of the message, 28 bytes
    name: str                   # shown as the message's contract
    name_signature: bytes
    fields: tuple[FieldFilter, ...]

    def _prefix(self, magic: int) -> bytes:
        return (bytes([magic]) + self.chain_id.to_bytes(8, "big")
                + bytes.fromhex(self.contract[2:]) + self.schema_hash)

    def signed(self) -> list[tuple[bytes, bytes]]:
        """``(bytes Ledger signed, its signature)`` for the message info, then
        each field filter."""
        out = [(self._prefix(_MAGIC_MESSAGE_INFO) + bytes([len(self.fields)])
                + self.name.encode(), self.name_signature)]
        for f in self.fields:
            path, label = f.path.encode(), f.label.encode()
            if f.format == "raw":
                data = self._prefix(_MAGIC_RAW) + path + label
            elif f.format == "datetime":
                data = self._prefix(_MAGIC_DATETIME) + path + label
            elif f.format == "token":
                assert f.coin_ref is not None
                data = (self._prefix(_MAGIC_AMOUNT_JOIN_TOKEN) + path
                        + bytes([f.coin_ref]))
            else:
                assert f.coin_ref is not None
                data = (self._prefix(_MAGIC_AMOUNT_JOIN_VALUE) + path + label
                        + bytes([f.coin_ref]))
            out.append((data, f.signature))
        return out


def type_name(field: Eip712Field) -> str:
    """The field's type as ``encodeType`` writes it (``uint160``,
    ``DutchOutput[]``)."""
    size = field.size or 0
    base = {STRUCT: field.struct or "", INT: f"int{size * 8}",
            UINT: f"uint{size * 8}", ADDRESS: "address", BOOL: "bool",
            STRING: "string", BYTES_FIX: f"bytes{size}",
            BYTES_DYN: "bytes"}[field.type]
    return base + "".join("[]" if d is None else f"[{d}]" for d in field.dims)


def schema_hash(msg: Eip712Message) -> bytes:
    """sha224 of the message's types as compact JSON, structs sorted by name
    and each field ``{"name", "type"}`` — the key Ledger signs filters for,
    and what the device computes from the definitions it receives."""
    types = {name: [{"name": f.name, "type": type_name(f)} for f in fields]
             for name, fields in sorted(msg.structs.items())}
    return hashlib.sha224(json.dumps(
        types, separators=(",", ":"), ensure_ascii=False).encode()).digest()


def domain_binding(msg: Eip712Message) -> tuple[int, str] | None:
    """The domain's ``(chainId, verifyingContract)`` — what filters are signed
    for — or ``None`` when it lacks either."""
    values = dict(zip((f.name for f in msg.structs[DOMAIN]), msg.domain,
                      strict=True))
    chain, contract = values.get("chainId"), values.get("verifyingContract")
    if not isinstance(chain, bytes) or not isinstance(contract, bytes):
        return None
    chain_id = int.from_bytes(chain, "big")
    if not 0 < chain_id < 2**64:
        return None
    return chain_id, "0x" + contract.hex()


def check_filters(msg: Eip712Message, filters: MessageFilters) -> None:
    """Raise ``Eip712Unsupported`` unless ``filters`` are for this message
    and each one leads to a leaf the device can format that way — so the
    device never gets a filter it would refuse mid-review."""
    if domain_binding(msg) != (filters.chain_id, filters.contract):
        raise Eip712Unsupported("filters for another chain or contract")
    if schema_hash(msg) != filters.schema_hash:
        raise Eip712Unsupported("filters for another schema")
    _check_label(filters.name)
    if 3 + len(filters.name) + len(filters.name_signature) > MAX_APDU_DATA:
        raise Eip712Unsupported("the message info is too long")
    if len(filters.fields) > 0xFF:
        raise Eip712Unsupported("too many filters")
    if len({f.path for f in filters.fields}) != len(filters.fields):
        raise Eip712Unsupported("two filters for one field")
    for f in filters.fields:
        _check_label(f.label)
        leaf = _leaf_at(msg, f.path)
        ok = {"raw": True,
              "datetime": leaf.type in (INT, UINT),
              "token": leaf.type == ADDRESS
              and f.coin_ref is not None and f.coin_ref != JOIN_VERIFYING_CONTRACT,
              "amount": leaf.type in (INT, UINT) and f.coin_ref is not None,
              }.get(f.format, False)
        if not ok:
            raise Eip712Unsupported(
                f"filter {f.path!r} can't format a {type_name(leaf)} as {f.format}")
        if (len(f.path) > MAX_APDU_DATA - 1
                or 3 + len(f.label) + len(f.signature) > MAX_APDU_DATA):
            raise Eip712Unsupported(f"filter {f.path!r} is too long")


def _check_label(label: str) -> None:
    if not label or len(label) > _MAX_NAME or any(
            not " " <= c <= "~" for c in label):
        raise Eip712Unsupported(f"label {label!r} can't be shown on the device")


def _leaf_at(msg: Eip712Message, path: str) -> Eip712Field:
    """The scalar field ``path`` leads to, from the primary type."""
    struct_name: str | None = msg.primary_type
    field: Eip712Field | None = None
    levels = 0
    for part in path.split("."):
        if levels:
            if part != "[]":
                raise Eip712Unsupported(f"filter {path!r} skips an array level")
            levels -= 1
            continue
        if field is not None:
            struct_name = field.struct
        fields = {f.name: f for f in msg.structs.get(struct_name or "", ())}
        if part not in fields:
            raise Eip712Unsupported(f"filter {path!r} names no field")
        field = fields[part]
        levels = len(field.dims)
    if field is None or levels or field.type == STRUCT:
        raise Eip712Unsupported(f"filter {path!r} doesn't lead to a value")
    return field


def filter_token_addresses(msg: Eip712Message,
                           filters: MessageFilters) -> list[str]:
    """The tokens the device needs the info of to show the amounts: every
    address in a "token" field, and the verifying contract when an amount
    joins it — lowercase 0x-hex, in order, once each."""
    out: list[str] = []
    token_paths = {f.path for f in filters.fields if f.format == "token"}

    def walk(name: str, values: list[Value], path: _Path) -> None:
        for f, v in zip(msg.structs[name], values, strict=True):
            visit(f, v, len(f.dims), (*path, f.name))

    def visit(field: Eip712Field, value: Value, levels: int, path: _Path) -> None:
        if levels:
            assert isinstance(value, list)
            for v in value:
                visit(field, v, levels - 1, (*path, "[]"))
        elif field.type == STRUCT:
            assert isinstance(value, list) and field.struct is not None
            walk(field.struct, value, path)
        elif ".".join(path) in token_paths and isinstance(value, bytes):
            out.append("0x" + value.hex())

    walk(msg.primary_type, msg.message, ())
    if any(f.format == "amount" and f.coin_ref == JOIN_VERIFYING_CONTRACT
           for f in filters.fields):
        out.append(filters.contract)
    return list(dict.fromkeys(out))


def _filter_command(f: FieldFilter, *, discarded: bool = False) -> bytes:
    p1 = _FILTER_DISCARDED if discarded else _FILTER_STANDARD
    label = bytes([len(f.label)]) + f.label.encode()
    sig = bytes([len(f.signature)]) + f.signature
    if f.format == "raw":
        return apdu(INS_EIP712_FILTERING, p1, _FILTER_RAW, label + sig)
    if f.format == "datetime":
        return apdu(INS_EIP712_FILTERING, p1, _FILTER_DATETIME, label + sig)
    assert f.coin_ref is not None
    if f.format == "token":
        return apdu(INS_EIP712_FILTERING, p1, _FILTER_AMOUNT_JOIN_TOKEN,
                    bytes([f.coin_ref]) + sig)
    return apdu(INS_EIP712_FILTERING, p1, _FILTER_AMOUNT_JOIN_VALUE,
                label + bytes([f.coin_ref]) + sig)


def filtered_v1_commands(msg: Eip712Message, path: str, filters: MessageFilters,
                         tokens: Sequence[bytes] = ()) -> list[bytes]:
    """The V1 flow with Ledger's filters, the signature request last:
    definitions (by name), filtering on, the tokens' info (``tokens``: PROVIDE
    ERC 20 TOKEN INFORMATION payloads), the domain, the message info, then
    each message value after its filter."""
    check_filters(msg, filters)
    out = _v1_definitions(msg, by_name=True)
    out.append(apdu(INS_EIP712_FILTERING, 0, _FILTER_ACTIVATE))
    out += [apdu(INS_PROVIDE_ERC20, 0, 0, t) for t in tokens]
    out.append(apdu(INS_EIP712_SEND_STRUCT_IMPL, _V1_COMPLETE, _V1_STRUCT_NAME,
                    DOMAIN.encode()))
    _v1_struct(out, msg, DOMAIN, msg.domain)
    out.append(apdu(INS_EIP712_FILTERING, 0, _FILTER_MESSAGE_INFO,
                    bytes([len(filters.name)]) + filters.name.encode()
                    + bytes([len(filters.fields)])
                    + bytes([len(filters.name_signature)]) + filters.name_signature))
    out.append(apdu(INS_EIP712_SEND_STRUCT_IMPL, _V1_COMPLETE, _V1_STRUCT_NAME,
                    msg.primary_type.encode()))
    _v1_struct(out, msg, msg.primary_type, msg.message, (),
               {f.path: f for f in filters.fields})
    out.append(apdu(INS_EIP712_SIGN, 0, P2_V1, pack_path(path)))
    return out


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
