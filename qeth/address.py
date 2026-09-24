"""Address codecs — how each chain family writes an address as text.

Internally qeth keeps ONE form for every account and contract: ``0x`` + the
20-byte body (the store, every cache key, calldata). That body is the same
for an EVM chain and for Tron — a Tron address is ``0x41`` + those 20 bytes,
base58check-encoded as a ``T…`` string — so the hex form stays valid on both,
and ABI calldata, event topics and cache keys need no per-family handling.
Converting at the EDGES keeps it that way:

- ``parse(text)`` turns what the user typed (or an API returned) into the
  internal hex, or None if it isn't an address of that family;
- ``display(addr)`` turns the internal hex into what the user sees and copies
  (EIP-55 on EVM, ``T…`` on Tron).

base58 is case-sensitive, so a ``T…`` string must never be ``.lower()``-ed or
used as a key — which is why it never enters the core.
"""

from __future__ import annotations

import hashlib
import re

from eth_utils import to_checksum_address

from .chains import EVM, TRON, Chain

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}
# Every Tron mainnet (and testnet) account address starts with this byte,
# which is what makes the base58 form read "T…".
TRON_PREFIX = 0x41

_HEX_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _checksum4(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58_ALPHABET[r] + out
    # Each leading zero byte is a literal "1".
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def b58decode(text: str) -> bytes | None:
    """The bytes ``text`` encodes, or None if it has a non-base58 character."""
    n = 0
    for c in text:
        i = _B58_INDEX.get(c)
        if i is None:
            return None
        n = n * 58 + i
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body


def tron_from_hex(addr: str) -> str:
    """``0x`` + 20-byte body (any case) → the ``T…`` base58check string."""
    body = bytes.fromhex(addr[2:] if addr[:2].lower() == "0x" else addr)
    if len(body) == 21 and body[0] == TRON_PREFIX:
        body = body[1:]     # already carries the 0x41 prefix ("41…" API form)
    if len(body) != 20:
        raise ValueError(f"not a 20-byte address: {addr!r}")
    payload = bytes([TRON_PREFIX]) + body
    return b58encode(payload + _checksum4(payload))


def tron_to_hex(text: str) -> str | None:
    """A ``T…`` base58check string → EIP-55 ``0x`` + 20-byte body, or None
    if it isn't a valid Tron address (bad character, length, prefix or
    checksum)."""
    raw = b58decode(text.strip())
    if raw is None or len(raw) != 25:
        return None
    payload, check = raw[:21], raw[21:]
    if payload[0] != TRON_PREFIX or _checksum4(payload) != check:
        return None
    return to_checksum_address("0x" + payload[1:].hex())


def tron_hex41(addr: str) -> str:
    """The ``41`` + 40-hex form TronGrid's HTTP API takes without
    ``visible=true`` — and what protobuf address fields hold."""
    return "41" + addr[2:].lower()


class AddressCodec:
    """The text form of addresses on one chain family (EVM by default)."""

    family = EVM
    # Shown in empty address fields.
    placeholder = "0x…"
    # A widest-case address, for sizing fields.
    sample = "0x" + "F" * 40

    def parse(self, text: str) -> str | None:
        """The internal (EIP-55 hex) address ``text`` denotes, or None."""
        text = text.strip()
        if not _HEX_ADDR.match(text):
            return None
        return to_checksum_address(text)

    def display(self, addr: str) -> str:
        """The form the user sees and copies for the internal ``addr``. A
        malformed stored value (a hand-edited config) is shown as-is rather
        than taking the view down with it."""
        if not _HEX_ADDR.match(addr):
            return addr
        return to_checksum_address(addr)

    def short(self, addr: str) -> str:
        """A compact form for tight spaces: 0x1234…abcd."""
        s = self.display(addr)
        return f"{s[:6]}…{s[-4:]}"


class TronAddressCodec(AddressCodec):
    family = TRON
    placeholder = "T…"
    sample = "T" + "W" * 33

    def parse(self, text: str) -> str | None:
        return tron_to_hex(text)

    def display(self, addr: str) -> str:
        if not _HEX_ADDR.match(addr):
            return addr
        return tron_from_hex(addr)

    def short(self, addr: str) -> str:
        s = self.display(addr)
        return f"{s[:5]}…{s[-4:]}"


_CODECS: dict[str, AddressCodec] = {EVM: AddressCodec(), TRON: TronAddressCodec()}


def codec_for(family: str | Chain) -> AddressCodec:
    """The codec for a family name, or for a ``Chain``'s family. Unknown
    families read as EVM (the only kind a dapp or the user can add)."""
    fam = family.family if isinstance(family, Chain) else family
    return _CODECS.get(fam, _CODECS[EVM])


def display_address(addr: str, chain: Chain | None) -> str:
    """``addr`` as the user should see it on ``chain`` (EVM when None)."""
    return codec_for(chain.family if chain is not None else EVM).display(addr)


def parse_any(text: str) -> tuple[str, str] | None:
    """``(family, internal hex)`` for text that is an address of ANY family —
    for inputs that decide the family from the address itself (adding a
    watch-only account). None if it's no address at all."""
    for fam, codec in _CODECS.items():
        addr = codec.parse(text)
        if addr is not None:
            return fam, addr
    return None
