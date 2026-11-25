"""qeth.ledger_eip712 — typed data as the Ledger app's full-message commands.

Two independent checks pin the encoder:

- **Golden vectors** from Ledger's own reference client (app-ethereum 1.24.0
  ``client/``: ``InputData.process_data`` for V1, ``json_to_tlv`` for V2, its
  ``CommandBuilder`` for the framing), byte for byte.
- **A re-hash**: EIP-712 computed from the parsed message the way the device
  computes it (from the schema and the leaf bytes alone) must equal
  eth_account's hash of the original JSON — the hash the signature is
  recovered against — over hand-picked and generated values.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from eth_account.messages import encode_typed_data
from eth_utils import keccak
from hypothesis import given, settings
from hypothesis import strategies as st

from qeth.ledger_eip712 import (
    ADDRESS, BOOL, BYTES_DYN, BYTES_FIX, DOMAIN, INT, STRING, STRUCT, UINT,
    Eip712Field, Eip712Message, Eip712Unsupported, parse_typed_data,
    schema_tlv, v1_commands, v2_commands, values_tlv,
)

GOLDEN = json.loads(
    (Path(__file__).parent / "data" / "ledger_eip712_golden.json").read_text())
MESSAGES = GOLDEN["messages"]
PATH = "44'/60'/0'/0/0"


@pytest.mark.parametrize("key", sorted(MESSAGES))
def test_v1_commands_match_ledgers_reference_client(key):
    case = MESSAGES[key]
    msg = parse_typed_data(case["typed_data"])
    assert [c.hex() for c in v1_commands(msg, case["path"])] == case["v1"]


@pytest.mark.parametrize("key", sorted(MESSAGES))
def test_v2_commands_match_ledgers_reference_client(key):
    case = MESSAGES[key]
    msg = parse_typed_data(case["typed_data"])
    assert [c.hex() for c in v2_commands(msg, case["path"])] == case["v2"]


# --- the device's hash, recomputed from the parsed message -------------------

_SOLIDITY = {INT: "int", UINT: "uint"}


def _type_string(f: Eip712Field) -> str:
    if f.type == STRUCT:
        base = f.struct
    elif f.type in _SOLIDITY:
        base = f"{_SOLIDITY[f.type]}{f.size * 8}"
    elif f.type == BYTES_FIX:
        base = f"bytes{f.size}"
    else:
        base = {ADDRESS: "address", BOOL: "bool", STRING: "string",
                BYTES_DYN: "bytes"}[f.type]
    return base + "".join("[]" if d is None else f"[{d}]" for d in f.dims)


def _deps(msg: Eip712Message, name: str, seen: set[str]) -> None:
    for f in msg.structs[name]:
        if f.struct is not None and f.struct not in seen:
            seen.add(f.struct)
            _deps(msg, f.struct, seen)


def _encode_type(msg: Eip712Message, name: str) -> str:
    deps: set[str] = set()
    _deps(msg, name, deps)
    deps.discard(name)
    return "".join(
        f"{n}({','.join(f'{_type_string(f)} {f.name}' for f in msg.structs[n])})"
        for n in [name, *sorted(deps)])


def _word(msg: Eip712Message, f: Eip712Field, value, levels: int) -> bytes:
    if levels:
        return keccak(b"".join(_word(msg, f, v, levels - 1) for v in value))
    if f.type == STRUCT:
        return _hash_struct(msg, f.struct, value)
    if f.type in (STRING, BYTES_DYN):
        return keccak(value)
    if f.type == BYTES_FIX:
        return value.ljust(32, b"\x00")
    if f.type == INT and len(value) == f.size and value[0] & 0x80:
        return int.from_bytes(value, "big", signed=True).to_bytes(32, "big", signed=True)
    return value.rjust(32, b"\x00")         # uint, non-negative int, address, bool


def _hash_struct(msg: Eip712Message, name: str, values) -> bytes:
    return keccak(keccak(_encode_type(msg, name).encode()) + b"".join(
        _word(msg, f, v, len(f.dims)) for f, v in zip(msg.structs[name], values)))


def _device_hashes(msg: Eip712Message) -> tuple[bytes, bytes]:
    return (_hash_struct(msg, DOMAIN, msg.domain),
            _hash_struct(msg, msg.primary_type, msg.message))


def _eth_account_hashes(typed: dict) -> tuple[bytes, bytes]:
    signable = encode_typed_data(full_message=typed)
    return signable.header, signable.body


@pytest.mark.parametrize("key", sorted(MESSAGES))
def test_parsed_message_hashes_like_eth_account(key):
    typed = MESSAGES[key]["typed_data"]
    assert _device_hashes(parse_typed_data(typed)) == _eth_account_hashes(typed)


def test_the_rehash_catches_a_wrong_leaf():
    # Teeth: a value encoded one bit off no longer hashes like eth_account.
    msg = parse_typed_data(MESSAGES["permit"]["typed_data"])
    msg.message[2] = (int.from_bytes(msg.message[2], "big") + 1).to_bytes(3, "big")
    assert _device_hashes(msg)[1] != _eth_account_hashes(
        MESSAGES["permit"]["typed_data"])[1]


def _one_field(type_: str, value, domain=None) -> dict:
    return {
        "types": {"EIP712Domain": [{"name": "name", "type": "string"}],
                  "Probe": [{"name": "v", "type": type_}]},
        "primaryType": "Probe",
        "domain": domain or {"name": "probe"},
        "message": {"v": value},
    }


def _same_hashes(typed: dict) -> None:
    assert _device_hashes(parse_typed_data(typed)) == _eth_account_hashes(typed)


@settings(max_examples=150, deadline=None)
@given(st.data())
def test_generated_integers_hash_like_eth_account(data):
    bits = data.draw(st.integers(1, 32)) * 8
    signed = data.draw(st.booleans())
    low, high = ((-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed
                 else (0, (1 << bits) - 1))
    n = data.draw(st.integers(low, high))
    form = data.draw(st.sampled_from(["int", "dec", "hex"]))
    value = {"int": n, "dec": str(n),
             "hex": hex(n) if n >= 0 else str(n)}[form]
    _same_hashes(_one_field(f"{'int' if signed else 'uint'}{bits}", value))


@settings(max_examples=100, deadline=None)
@given(st.integers(1, 32), st.binary(max_size=32), st.booleans())
def test_generated_fixed_bytes_hash_like_eth_account(size, raw, as_hex):
    raw = raw[:size]
    _same_hashes(_one_field(f"bytes{size}", "0x" + raw.hex() if as_hex else raw))


@settings(max_examples=100, deadline=None)
@given(st.one_of(st.binary(max_size=600).map(lambda b: "0x" + b.hex()),
                 st.text(max_size=300)))
def test_generated_dynamic_bytes_hash_like_eth_account(value):
    _same_hashes(_one_field("bytes", value))


@settings(max_examples=100, deadline=None)
@given(st.text(max_size=400))
def test_generated_strings_hash_like_eth_account(text):
    _same_hashes(_one_field("string", text))


@pytest.mark.parametrize("value", [True, False, "true", "false", "False", "0",
                                   "1", 0, 1, ""])
def test_bools_read_like_eth_account(value):
    _same_hashes(_one_field("bool", value))


@pytest.mark.parametrize("value", ["0x", "0xabc", "0X00", "plain text", 7, 0,
                                   -3, True])
def test_odd_bytes_values_read_like_eth_account(value):
    _same_hashes(_one_field("bytes", value))


def test_nested_arrays_hash_like_eth_account():
    typed = _one_field("uint8[2][][3]", [[[1, 2]], [], [[3, 4], [5, 6]]])
    _same_hashes(typed)
    msg = parse_typed_data(typed)
    assert msg.structs["Probe"][0].dims == (2, None, 3)


def test_the_domain_takes_eth_accounts_field_order():
    """eth_account orders the domain's fields itself, whatever order
    ``types.EIP712Domain`` lists them in — so does the device's copy."""
    typed = copy.deepcopy(MESSAGES["permit"]["typed_data"])
    typed["types"]["EIP712Domain"].reverse()
    msg = parse_typed_data(typed)
    assert [f.name for f in msg.structs[DOMAIN]] == [
        "name", "version", "chainId", "verifyingContract"]
    assert next(iter(msg.structs)) == DOMAIN
    _same_hashes(typed)


def test_a_struct_named_like_a_builtin_is_the_struct():
    typed = {
        "types": {"EIP712Domain": [{"name": "name", "type": "string"}],
                  "bytes7": [{"name": "x", "type": "uint8"}],
                  "Probe": [{"name": "v", "type": "bytes7"}]},
        "primaryType": "Probe",
        "domain": {"name": "probe"},
        "message": {"v": {"x": 1}},
    }
    msg = parse_typed_data(typed)
    assert msg.structs["Probe"][0].type == STRUCT


# --- what the device can't carry ---------------------------------------------


@pytest.mark.parametrize("typed", [
    _one_field("uint", 5),                          # no size: eth_abi's uint256
    _one_field("uint7", 5),
    _one_field("bytes33", "0x00"),
    _one_field("string", None),                     # eth_account: empty bytes32
    _one_field("uint8[]", list(range(256))),        # > 255 entries
    _one_field("uint8[3]", [1, 2]),                 # fixed size mismatch
    _one_field("uint8[300]", [0] * 300),            # fixed size past 255
    _one_field("uint8" + "[]" * 9, []),             # > 8 dimensions
    _one_field("bytes4", "0x0102030405"),           # longer than bytes4
    _one_field("string", 5),
    _one_field("uint8", True),
    _one_field("uint8", 256),
    _one_field("Unknown", 1),
])
def test_unsupported_messages_raise(typed):
    with pytest.raises(Eip712Unsupported):
        parse_typed_data(typed)


@pytest.mark.parametrize("name", ["bad (name)", "comma,s", "ünïcode", "tab\tname",
                                  " ", "x" * 256])
def test_names_the_device_cant_show_raise(name):
    typed = _one_field("uint8", 1)
    typed["types"]["Probe"][0]["name"] = name
    typed["message"] = {name: 1}
    with pytest.raises(Eip712Unsupported):
        parse_typed_data(typed)


@pytest.mark.parametrize("name", ["Has Space", "Arr[]", "Tup(le)"])
def test_struct_names_with_type_delimiters_raise(name):
    typed = _one_field("uint8", 1)
    typed["types"][name] = typed["types"].pop("Probe")
    typed["primaryType"] = name
    with pytest.raises(Eip712Unsupported):
        parse_typed_data(typed)


def test_a_field_name_may_hold_spaces_and_brackets():
    typed = _one_field("uint8", 1)
    typed["types"]["Probe"][0]["name"] = "my [weird] name"
    typed["message"] = {"my [weird] name": 1}
    assert parse_typed_data(typed).structs["Probe"][0].name == "my [weird] name"


def test_a_message_too_large_for_v2_raises_on_building_its_commands():
    big = parse_typed_data(_one_field("string[]", ["x" * 40000, "y" * 40000]))
    v1_commands(big, PATH)                    # V1 streams it: fine
    with pytest.raises(Eip712Unsupported):
        v2_commands(big, PATH)


def test_a_leaf_longer_than_two_length_bytes_raises():
    with pytest.raises(Eip712Unsupported):
        parse_typed_data(_one_field("string", "x" * 70000))


def test_missing_primary_type_raises():
    typed = _one_field("uint8", 1)
    del typed["primaryType"]
    with pytest.raises(Eip712Unsupported):
        parse_typed_data(typed)


# --- wire details -------------------------------------------------------------


def test_v2_tlv_lengths_use_der_long_form():
    msg = parse_typed_data(_one_field("string", "z" * 300))
    values = values_tlv(msg, PATH)
    # The 300-byte leaf: tag 00, length 82 01 2c.
    assert bytes.fromhex("0082012c") + b"z" * 300 in values
    assert schema_tlv(msg).startswith(bytes.fromhex("000101"))


def test_v2_payload_chunks_carry_the_total_length_once():
    msg = parse_typed_data(_one_field("string", "z" * 600))
    commands = v2_commands(msg, PATH)
    values = [c for c in commands if c[1] == 0x1C]
    tlv = values_tlv(msg, PATH)
    assert values[0][2] == 0x01 and all(c[2] == 0x00 for c in values[1:])
    assert b"".join(c[5:] for c in values) == len(tlv).to_bytes(2, "big") + tlv
    assert commands[-1] == bytes.fromhex("e00c000200")


def test_v1_long_values_stream_in_partial_chunks():
    msg = parse_typed_data(_one_field("string", "q" * 600))
    fields = [c for c in v1_commands(msg, PATH) if c[1] == 0x1C and c[3] == 0xFF]
    leaf = fields[-3:]                       # 2 + 600 bytes → three chunks
    assert [c[2] for c in leaf] == [0x01, 0x01, 0x00]
    assert b"".join(c[5:] for c in leaf) == (600).to_bytes(2, "big") + b"q" * 600

