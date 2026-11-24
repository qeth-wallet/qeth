"""eth_account's reading of the values in an EIP-712 message.

qeth hashes a typed message with ``eth_account.messages.encode_typed_data``,
and every hardware signer checks the device's signature against that hash. So
what a device is sent has to read the dapp's JSON the same way: hex strings
and decimal strings as integers, "false" / "0" as false, ``0x`` text as hex
bytes and other text as UTF-8, the domain's type built from the keys the
domain has. Used by ``ledger_eip712`` (Ledger's commands) and ``trezor``
(trezorlib's input). Qt-free.
"""

from __future__ import annotations

import re
from typing import Any

DOMAIN = "EIP712Domain"
# eth_account builds the domain's type from the keys the domain has, in this
# order, whatever ``types.EIP712Domain`` says — so devices get the same.
DOMAIN_FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "string"), ("version", "string"), ("chainId", "uint256"),
    ("verifyingContract", "address"), ("salt", "bytes32"),
)

_HEX = re.compile(r"^0x[0-9a-fA-F]*$")
_ADDRESS = re.compile(r"^0[xX][0-9a-fA-F]{40}$")


class Eip712Unsupported(ValueError):
    """The message can't go to the device as it is."""


def read_int(name: str, value: Any) -> int:
    """An ``intN`` / ``uintN`` value: an integer, a ``0x`` hex string or a
    decimal string (not a bool)."""
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


def read_bytes(name: str, value: Any) -> bytes:
    """A ``bytes`` / ``bytesN`` value: hex when ``0x``-prefixed, else the
    text's UTF-8; an integer as big-endian."""
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


def read_bool(value: Any) -> bool:
    """A ``bool`` value: false when falsy or "False" / "false" / "0"."""
    return not (not value or value in ("False", "false", "0"))


def read_address(name: str, value: Any) -> bytes:
    """An ``address`` value: 20 bytes, or ``0x`` and 40 hex digits."""
    if isinstance(value, bytes) and len(value) == 20:
        return value
    if isinstance(value, str) and _ADDRESS.match(value):
        return bytes.fromhex(value[2:])
    raise Eip712Unsupported(f"{name} isn't an address")
