"""The digests a Tron dapp asks a wallet to sign besides transactions —
TronWeb's message signatures and TIP-712 typed data — computed HERE from the
request's content, never taken from the page (a wallet must not sign a digest
it can't show the preimage of).

- ``trx.signMessageV2(message)`` (TIP-191): keccak256 of
  ``"\\x19TRON Signed Message:\\n" + len(message) + message``, where a string
  message is its UTF-8 bytes. Version 2.
- ``trx.sign(hexString)`` (the legacy message form): keccak256 of the FIXED
  header ``"\\x19TRON Signed Message:\\n32"`` + the bytes, whatever their
  length. Version 1. (TronWeb's ``useTronHeader=false`` variant swaps in
  Ethereum's header — i.e. an Ethereum ``personal_sign`` with the same key —
  and is refused before it gets here.)
- ``trx._signTypedData(domain, types, value)`` (TIP-712): EIP-712 with Tron
  addresses (``T…`` / ``41…`` / ``0x…``, all hashed as the 20-byte body) and a
  ``trcToken`` type that encodes as ``uint256`` but keeps its own name in the
  type hash. A port of TronWeb 6's ``TypedDataEncoder`` (ethers v6 semantics).

The typed-data domain MUST carry the Tron chain's id: without one, or with an
Ethereum chain id, the same key's signature would be a valid EIP-712 signature
for the user's EVM account (hot wallets hold one key for both families) —
e.g. a Permit on mainnet. ``check_domain_chain`` enforces that.
"""

from __future__ import annotations

import re
from typing import Any

from eth_utils import keccak

from ..address import tron_to_hex

TRON_MESSAGE_PREFIX = b"\x19TRON Signed Message:\n"
# The legacy (v1) header: "32" is baked in regardless of the message length.
V1_HEADER = TRON_MESSAGE_PREFIX + b"32"

_UINT256_MAX = 2 ** 256 - 1


def message_digest(raw: bytes, version: int) -> bytes:
    """The 32 bytes a TronWeb message signature is over."""
    if version == 2:
        return keccak(TRON_MESSAGE_PREFIX + str(len(raw)).encode() + raw)
    if version == 1:
        return keccak(V1_HEADER + raw)
    raise ValueError(f"unknown Tron message version {version!r}")


# --- TIP-712 ------------------------------------------------------------------

_DOMAIN_FIELD_TYPES = {
    "name": "string",
    "version": "string",
    "chainId": "uint256",
    "verifyingContract": "address",
    "salt": "bytes32",
}
_DOMAIN_ORDER = list(_DOMAIN_FIELD_TYPES)

_INT_RE = re.compile(r"^(u?)int(\d*)$")
_BYTES_RE = re.compile(r"^bytes(\d+)$")
_ARRAY_RE = re.compile(r"^(.*)\[(\d*)\]$")


class TypedDataError(ValueError):
    """The typed data is malformed or unsafe to sign."""


def address20(value: Any) -> bytes:
    """A Tron address in any form TronWeb accepts — ``T…`` base58check,
    ``41`` + 40 hex, ``0x`` + 40 hex — as its 20-byte body."""
    if not isinstance(value, str):
        raise TypedDataError(f"not an address: {value!r}")
    text = value.strip()
    if text.startswith("T"):
        hexed = tron_to_hex(text)
        if hexed is None:
            raise TypedDataError(f"not a Tron address: {value!r}")
        return bytes.fromhex(hexed[2:])
    body = text[2:] if text[:2].lower() in ("0x", "41") else text
    if len(text) == 42 and re.fullmatch(r"[0-9a-fA-F]{40}", body):
        return bytes.fromhex(body)
    raise TypedDataError(f"not an address: {value!r}")


def _big_int(value: Any) -> int:
    """ethers' getBigInt: an int, a decimal string, or a ``0x`` hex string."""
    if isinstance(value, bool):
        raise TypedDataError(f"not a number: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        neg = text.startswith("-")
        body = text[1:] if neg else text
        try:
            n = int(body, 16) if body[:2].lower() == "0x" else int(body, 10)
        except ValueError:
            raise TypedDataError(f"not a number: {value!r}") from None
        return -n if neg else n
    raise TypedDataError(f"not a number: {value!r}")


def _hex_bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str) and value[:2].lower() == "0x":
        body = value[2:]
        if len(body) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]*", body):
            return bytes.fromhex(body)
    raise TypedDataError(f"not hex bytes: {value!r}")


def _encode_base(type_: str, value: Any) -> bytes | None:
    """The 32-byte word for an atomic / dynamic base type, or None if
    ``type_`` isn't one (a struct or an array)."""
    m = _INT_RE.match(type_)
    if m:
        signed = m.group(1) == ""
        width = int(m.group(2) or "256")
        if width % 8 or not 0 < width <= 256 or (m.group(2) and m.group(2) != str(width)):
            raise TypedDataError(f"invalid numeric width {type_!r}")
        n = _big_int(value)
        lo, hi = (-(2 ** (width - 1)), 2 ** (width - 1) - 1) if signed else (0, 2 ** width - 1)
        if not lo <= n <= hi:
            raise TypedDataError(f"value out of bounds for {type_}: {n}")
        return (n & _UINT256_MAX).to_bytes(32, "big")
    m = _BYTES_RE.match(type_)
    if m:
        width = int(m.group(1))
        if not 0 < width <= 32 or m.group(1) != str(width):
            raise TypedDataError(f"invalid bytes width {type_!r}")
        raw = _hex_bytes(value)
        if len(raw) != width:
            raise TypedDataError(f"invalid length for {type_}")
        return raw.ljust(32, b"\0")
    if type_ == "trcToken":
        return _encode_base("uint256", value)
    if type_ == "address":
        return address20(value).rjust(32, b"\0")
    if type_ == "bool":
        return (1 if value else 0).to_bytes(32, "big")
    if type_ == "bytes":
        return keccak(_hex_bytes(value))
    if type_ == "string":
        if not isinstance(value, str):
            raise TypedDataError(f"invalid string {value!r}")
        return keccak(value.encode("utf-8"))
    return None


class TypedDataEncoder:
    """``types`` (without EIP712Domain) → struct hashes, as TronWeb does."""

    def __init__(self, types: dict[str, list[dict]], *, domain: bool = False):
        # ``domain``: these are the EIP712Domain fields themselves; otherwise a
        # dapp-supplied EIP712Domain entry is dropped (TronWeb derives it from
        # the domain object — what ``domain_types`` does here).
        if not isinstance(types, dict):
            raise TypedDataError("types must be an object")
        self.types = {name: list(fields) for name, fields in types.items()
                      if domain or name != "EIP712Domain"}
        links: dict[str, set[str]] = {n: set() for n in self.types}
        parents: dict[str, set[str]] = {n: set() for n in self.types}
        for name, fields in self.types.items():
            seen: set[str] = set()
            for f in fields:
                if not isinstance(f, dict) or not isinstance(f.get("name"), str) \
                        or not isinstance(f.get("type"), str):
                    raise TypedDataError(f"invalid field in type {name!r}")
                if f["name"] in seen:
                    raise TypedDataError(f"duplicate field {f['name']!r} in {name!r}")
                seen.add(f["name"])
                base = _ARRAY_RE.sub(r"\1", f["type"])
                while _ARRAY_RE.match(base):
                    base = _ARRAY_RE.sub(r"\1", base)
                if _is_base_type(base):
                    continue
                if base not in self.types:
                    raise TypedDataError(f"unknown type {base!r}")
                parents[base].add(name)
                links[name].add(base)
        self._links = links
        for name in self.types:
            self._check_acyclic(name, [])
        roots = [n for n, p in parents.items() if not p]
        if len(roots) != 1:
            raise TypedDataError(
                "missing primary type" if not roots
                else f"ambiguous primary types: {', '.join(sorted(roots))}")
        self.primary_type = roots[0]

    def _check_acyclic(self, name: str, path: list[str]) -> None:
        if name in path:
            raise TypedDataError(f"circular type reference to {name!r}")
        for child in self._links[name]:
            self._check_acyclic(child, [*path, name])

    def _deps(self, name: str, out: set[str]) -> set[str]:
        if name in out:
            return out
        out.add(name)
        for child in self._links[name]:
            self._deps(child, out)
        return out

    def encode_type(self, name: str) -> str:
        deps = self._deps(name, set()) - {name}
        return "".join(
            f"{n}({','.join(f['type'] + ' ' + f['name'] for f in self.types[n])})"
            for n in [name, *sorted(deps)])

    def _encode_value(self, type_: str, value: Any) -> bytes:
        word = _encode_base(type_, value)
        if word is not None:
            return word
        m = _ARRAY_RE.match(type_)
        if m:
            if not isinstance(value, list):
                raise TypedDataError(f"expected an array for {type_}")
            if m.group(2) and len(value) != int(m.group(2)):
                raise TypedDataError(f"array length mismatch for {type_}")
            return keccak(b"".join(self._encode_value(m.group(1), v) for v in value))
        if type_ in self.types:
            return self.hash_struct(type_, value)
        raise TypedDataError(f"unknown type {type_!r}")

    def hash_struct(self, name: str, value: Any) -> bytes:
        if not isinstance(value, dict):
            raise TypedDataError(f"expected an object for {name}")
        data = keccak(self.encode_type(name).encode())
        for f in self.types[name]:
            if f["name"] not in value:
                raise TypedDataError(f"missing value for {name}.{f['name']}")
            data += self._encode_value(f["type"], value[f["name"]])
        return keccak(data)


def _is_base_type(type_: str) -> bool:
    """Is ``type_`` a base type (not a struct)? Checked by shape only."""
    return bool(_INT_RE.match(type_) or _BYTES_RE.match(type_)) or type_ in (
        "trcToken", "address", "bool", "bytes", "string")


def domain_types(domain: dict) -> list[dict]:
    """The EIP712Domain fields a domain object implies, in canonical order."""
    if not isinstance(domain, dict):
        raise TypedDataError("domain must be an object")
    fields = []
    for key, value in domain.items():
        if value is None:
            continue
        if key not in _DOMAIN_FIELD_TYPES:
            raise TypedDataError(f"invalid typed-data domain key {key!r}")
        fields.append({"name": key, "type": _DOMAIN_FIELD_TYPES[key]})
    fields.sort(key=lambda f: _DOMAIN_ORDER.index(f["name"]))
    return fields


def hash_domain(domain: dict) -> bytes:
    enc = TypedDataEncoder({"EIP712Domain": domain_types(domain)}, domain=True)
    return enc.hash_struct("EIP712Domain", domain)


def typed_data_digest(domain: dict, types: dict, message: dict) -> bytes:
    """The TIP-712 digest ``_signTypedData(domain, types, message)`` signs."""
    enc = TypedDataEncoder(types)
    return keccak(b"\x19\x01" + hash_domain(domain) + enc.hash_struct(enc.primary_type, message))


def check_domain_chain(domain: dict, chain_id: int) -> None:
    """Refuse a domain that doesn't pin this Tron chain (see the module doc:
    without it the signature is replayable as the EVM account's)."""
    if not isinstance(domain, dict) or domain.get("chainId") is None:
        raise TypedDataError(
            "the typed data names no chainId — refused, since the signature "
            "would be valid on any chain (including your Ethereum account)")
    cid = _big_int(domain["chainId"])
    if cid != chain_id:
        raise TypedDataError(
            f"the typed data is for chain {cid}, not Tron ({chain_id}) — refused: "
            "the same key signs your EVM account")
