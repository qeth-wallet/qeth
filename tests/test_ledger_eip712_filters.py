"""Ledger's EIP-712 filters: the shipped snapshot and the filtered V1 flow
(``qeth.ledger_eip712.filtered_v1_commands``, ``ledger_metadata.Eip712FilterSet``).

The golden commands come from Ledger's own reference client (app-ethereum
1.24.0 ``client/``, ``InputData.process_data`` with filters), its signatures
answered from the snapshot, so they pin the wire byte-for-byte: the
definitions sorted by name, filtering on, the message info after the domain,
each filter right before its value, and the discarded filters of an empty
array.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import random
from pathlib import Path

import pytest

from qeth import ledger_metadata
from qeth.ledger_app import apdu
from qeth.ledger_eip712 import (
    BYTES_DYN, BYTES_FIX, INT, STRUCT, UINT, Eip712Field, Eip712Unsupported,
    MessageFilters, check_filters, domain_binding, filter_token_addresses,
    filtered_v1_commands, parse_typed_data, schema_hash, type_name,
)
from qeth.ledger_metadata import EIP712_FILTERS_FILE, Eip712FilterSet

DATA = Path(__file__).parent / "data"
GOLDEN = json.loads((DATA / "ledger_eip712_filter_golden.json").read_text())["messages"]
SNAPSHOT_SHA256 = "63b8427d7ee4f5ac5ccbc1c22e8a93eeb4e96f88a128f28db0e7bef8943f9c67"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
PATH = "44'/60'/0'/0/0"


def _filters(typed: dict) -> MessageFilters:
    msg = parse_typed_data(typed)
    binding = domain_binding(msg)
    assert binding is not None
    found = Eip712FilterSet(EIP712_FILTERS_FILE).lookup(*binding, schema_hash(msg))
    assert found is not None
    return found


# --- the snapshot ---------------------------------------------------------------


def test_the_snapshot_is_ledgers_file_unmodified():
    digest = hashlib.sha256(EIP712_FILTERS_FILE.read_bytes()).hexdigest()
    assert digest == SNAPSHOT_SHA256
    source = EIP712_FILTERS_FILE.with_name("ledger_eip712_filters.SOURCE.txt")
    assert SNAPSHOT_SHA256 in source.read_text()
    assert EIP712_FILTERS_FILE.with_name("ledger_eip712_filters.LICENSE.txt").exists()


def test_every_snapshot_entry_parses_and_verifies():
    """All of it is usable: each entry well-formed, every signature Ledger's."""
    entries = json.loads(EIP712_FILTERS_FILE.read_text())
    assert len(entries) == 154
    filters = Eip712FilterSet(EIP712_FILTERS_FILE)
    for key in entries:
        chain, contract, digest = key.split(":")
        assert filters.lookup(int(chain), contract, bytes.fromhex(digest)) is not None, key


def _write_snapshot(tmp_path, entries: dict) -> Eip712FilterSet:
    path = tmp_path / "filters.json"
    path.write_text(json.dumps(entries))
    return Eip712FilterSet(path)


def _real_entry(key: str) -> dict:
    return copy.deepcopy(json.loads(EIP712_FILTERS_FILE.read_text())[key])


PERMIT2_KEY = ("1:0x000000000022d473030f116ddee9f6b43ac78ba3:"
               "4d593149e876e739220f3b5ede1b38a0213d76c4705b1547c4323df3")


def _lookup(filters: Eip712FilterSet, key: str = PERMIT2_KEY):
    chain, contract, digest = key.split(":")
    return filters.lookup(int(chain), contract, bytes.fromhex(digest))


@pytest.mark.parametrize("tamper", [
    lambda e: e["fields"][2].update(label="Approve to anyone"),
    lambda e: e["fields"][1].update(label="Amount allowance!"),
    lambda e: e["fields"][1].update(coin_ref=1),
    lambda e: e["fields"][2].update(path="details.nonce"),
    lambda e: e["contractName"].update(label="Permit3"),
    lambda e: e["fields"].pop(),                    # the count is signed too
    lambda e: e["fields"][0].update(signature=e["fields"][1]["signature"]),
])
def test_a_tampered_entry_is_dropped(tmp_path, tamper):
    entry = _real_entry(PERMIT2_KEY)
    tamper(entry)
    assert _lookup(_write_snapshot(tmp_path, {PERMIT2_KEY: entry})) is None


def test_an_entry_signed_for_another_contract_is_dropped(tmp_path):
    other = PERMIT2_KEY.replace("0x000000000022d4", "0x000000000022d5")
    filters = _write_snapshot(tmp_path, {other: _real_entry(PERMIT2_KEY)})
    assert _lookup(filters, other) is None


@pytest.mark.parametrize("tamper", [
    lambda e: e.pop("contractName"),
    lambda e: e["fields"][0].pop("format"),
    lambda e: e["fields"][0].update(format="trusted_name"),
    lambda e: e["fields"][0].update(coin_ref=None),
    lambda e: e["fields"][0].update(coin_ref=256),
    lambda e: e["fields"][0].update(signature="zz"),
    lambda e: e["fields"][0].update(label=7),
    lambda e: e.update(fields="nope"),
])
def test_a_malformed_entry_is_dropped(tmp_path, tamper):
    entry = _real_entry(PERMIT2_KEY)
    tamper(entry)
    assert _lookup(_write_snapshot(tmp_path, {PERMIT2_KEY: entry})) is None


def test_an_unreadable_snapshot_finds_nothing(tmp_path):
    (tmp_path / "bad.json").write_text("{not json")
    assert _lookup(Eip712FilterSet(tmp_path / "bad.json")) is None
    assert _lookup(Eip712FilterSet(tmp_path / "missing.json")) is None
    assert _lookup(Eip712FilterSet(EIP712_FILTERS_FILE),
                   PERMIT2_KEY.replace("1:", "5:", 1)) is None


def test_the_process_wide_snapshot_is_the_shipped_file():
    assert _lookup(ledger_metadata.default_eip712_filters()) is not None


# --- the wire -------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_filtered_commands_match_ledgers_reference_client(key):
    case = GOLDEN[key]
    msg = parse_typed_data(case["typed_data"])
    filters = _filters(case["typed_data"])
    assert filters.name == case["filters"]
    assert filtered_v1_commands(msg, case["path"], filters) == \
        [bytes.fromhex(c) for c in case["v1_filtered"]]


def test_token_info_goes_right_after_filtering_is_on():
    case = GOLDEN["permit2_single"]
    msg = parse_typed_data(case["typed_data"])
    golden = [bytes.fromhex(c) for c in case["v1_filtered"]]
    commands = filtered_v1_commands(msg, PATH, _filters(case["typed_data"]),
                                    [b"one", b"two"])
    at = golden.index(apdu(0x1E, 0, 0x00)) + 1
    assert commands == golden[:at] + [apdu(0x0A, 0, 0, b"one"),
                                      apdu(0x0A, 0, 0, b"two")] + golden[at:]


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_the_schema_hash_ignores_the_dapps_type_order(key):
    typed = copy.deepcopy(GOLDEN[key]["typed_data"])
    expected = schema_hash(parse_typed_data(typed))
    names = list(typed["types"])
    rng = random.Random(key)
    for _ in range(5):
        rng.shuffle(names)
        typed["types"] = {n: typed["types"][n] for n in names}
        assert schema_hash(parse_typed_data(typed)) == expected


@pytest.mark.parametrize(("field", "name"), [
    (Eip712Field("a", UINT, 20), "uint160"),
    (Eip712Field("a", INT, 1), "int8"),
    (Eip712Field("a", BYTES_FIX, 4), "bytes4"),
    (Eip712Field("a", BYTES_DYN), "bytes"),
    (Eip712Field("a", UINT, 2, (2, None)), "uint16[2][]"),
    (Eip712Field("a", STRUCT, None, (None,), "DutchOutput"), "DutchOutput[]"),
])
def test_type_names_as_encode_type_writes_them(field, name):
    assert type_name(field) == name


@pytest.mark.parametrize(("key", "tokens"), [
    ("permit2_single", [USDC]),
    ("uniswapx_two_outputs", [USDC]),      # permitted + input, once; outputs are raw
    ("uniswapx_no_outputs", [USDC]),
    ("usdc_permit", [USDC]),               # the amount joins the verifying contract
])
def test_the_tokens_the_device_needs(key, tokens):
    typed = GOLDEN[key]["typed_data"]
    assert filter_token_addresses(parse_typed_data(typed), _filters(typed)) == tokens


def test_a_domain_without_chain_or_contract_binds_nothing():
    typed = copy.deepcopy(GOLDEN["permit2_single"]["typed_data"])
    del typed["domain"]["verifyingContract"]
    assert domain_binding(parse_typed_data(typed)) is None
    typed = copy.deepcopy(GOLDEN["permit2_single"]["typed_data"])
    typed["domain"]["chainId"] = 0
    assert domain_binding(parse_typed_data(typed)) is None


# --- filters that don't fit the message are never sent ---------------------------


def _permit2():
    typed = GOLDEN["permit2_single"]["typed_data"]
    return parse_typed_data(typed), _filters(typed)


def _with_field(filters: MessageFilters, i: int, **changes) -> MessageFilters:
    fields = list(filters.fields)
    fields[i] = dataclasses.replace(fields[i], **changes)
    return dataclasses.replace(filters, fields=tuple(fields))


@pytest.mark.parametrize("change", [
    {"path": "details.missing"},
    {"path": "details"},                      # a struct, not a value
    {"path": "spender.[]"},                   # not an array
    {"path": "details.token.x"},
    {"format": "token", "path": "details.amount"},
    {"format": "amount", "path": "spender", "coin_ref": 0},
    {"format": "datetime", "path": "spender"},
    {"format": "token", "coin_ref": 0xFF},     # the verifying contract is no token field
    {"format": "trusted_name"},
    {"label": "Montant autorisé"},
    {"label": ""},
    {"label": "x" * 200},
    {"path": "details.amount"},               # two filters for one field
])
def test_a_filter_the_device_would_refuse_is_never_sent(change):
    msg, filters = _permit2()
    bad = _with_field(filters, 0, **change)
    with pytest.raises(Eip712Unsupported):
        check_filters(msg, bad)
    with pytest.raises(Eip712Unsupported):
        filtered_v1_commands(msg, PATH, bad)


def test_an_array_filter_must_name_each_level():
    typed = GOLDEN["uniswapx_two_outputs"]["typed_data"]
    msg, filters = parse_typed_data(typed), _filters(typed)
    i = next(i for i, f in enumerate(filters.fields) if "[]" in f.path)
    with pytest.raises(Eip712Unsupported, match="array level"):
        check_filters(msg, _with_field(
            filters, i, path=filters.fields[i].path.replace(".[]", "")))


@pytest.mark.parametrize("change", [
    lambda t: t["domain"].update(chainId=10),
    lambda t: t["domain"].update(verifyingContract="0x" + "11" * 20),
    lambda t: t["types"]["PermitDetails"].append({"name": "extra", "type": "bool"})
    or t["message"]["details"].update(extra=True),
])
def test_filters_of_another_message_are_never_sent(change):
    typed = copy.deepcopy(GOLDEN["permit2_single"]["typed_data"])
    filters = _filters(typed)
    change(typed)
    with pytest.raises(Eip712Unsupported):
        check_filters(parse_typed_data(typed), filters)
