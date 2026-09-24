"""Address codecs: the internal 0x form ↔ each family's text form."""

import pytest

from qeth.address import (
    b58decode, b58encode, codec_for, display_address, parse_any,
    tron_from_hex, tron_hex41, tron_to_hex,
)
from qeth.chains import EVM, TRON, Chain

# USDT on Tron: a well-known pair of forms (tronscan shows both).
USDT_T = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_HEX = "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c"


def test_tron_round_trip():
    assert tron_from_hex(USDT_HEX) == USDT_T
    assert tron_to_hex(USDT_T).lower() == USDT_HEX


def test_tron_from_hex_accepts_the_41_api_form():
    assert tron_from_hex("41" + USDT_HEX[2:]) == USDT_T
    assert tron_hex41(USDT_HEX.upper().replace("0X", "0x")) == "41" + USDT_HEX[2:]


@pytest.mark.parametrize("bad", [
    "",
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u",     # checksum off by one char
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6",      # truncated
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj60",     # '0' is not base58
    USDT_HEX,                                 # hex is not a Tron address
    b58encode(bytes([0x42]) + bytes(20) + bytes(4)),   # wrong prefix
])
def test_tron_to_hex_rejects(bad):
    assert tron_to_hex(bad) is None


def test_b58_keeps_leading_zero_bytes():
    data = b"\0\0\x01\x02"
    assert b58encode(data).startswith("11")
    assert b58decode(b58encode(data)) == data


def test_evm_codec_parses_and_checksums():
    c = codec_for(EVM)
    assert c.parse(" " + USDT_HEX + " ") == "0xa614f803B6FD780986A42c78Ec9c7f77e6DeD13C"
    assert c.parse(USDT_T) is None
    assert c.parse("0x1234") is None


def test_tron_codec_display_and_short():
    c = codec_for(TRON)
    assert c.display(USDT_HEX) == USDT_T
    assert c.short(USDT_HEX) == "TR7NH…Lj6t"
    assert c.parse(USDT_HEX) is None


def test_codec_for_a_chain_and_unknown_family():
    tron = Chain("Tron", 728126428, "", family=TRON)
    assert codec_for(tron).family == TRON
    assert codec_for("cosmos").family == EVM
    assert display_address(USDT_HEX, tron) == USDT_T
    assert display_address(USDT_HEX, None).startswith("0x")


def test_parse_any_detects_the_family():
    assert parse_any(USDT_T) == (TRON, "0xa614f803B6FD780986A42c78Ec9c7f77e6DeD13C")
    assert parse_any(USDT_HEX)[0] == EVM
    assert parse_any("hello") is None
