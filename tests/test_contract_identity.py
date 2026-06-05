"""Tests for qeth.contract_identity — the cache, the Etherscan-v2 source
(mocked transport), and the describe_identity badge logic."""

from __future__ import annotations

import json

import pytest

from qeth.contract_identity import (
    ContractIdentity,
    ContractIdentityCache,
    ContractIdentitySource,
    describe_identity,
)

ADDR = "0x" + "ab" * 20
DEPLOYER = "0x" + "cd" * 20


# --- cache ---------------------------------------------------------------

def test_cache_roundtrip(tmp_path):
    c = ContractIdentityCache(root=tmp_path)
    assert c.load(1, ADDR) is None
    c.save(1, ContractIdentity(ADDR, True, name="Foo", verified=True,
                               deployer=DEPLOYER, deployed_at=1675209600))
    got = c.load(1, ADDR)
    assert got is not None
    assert got.name == "Foo" and got.verified
    assert got.deployed_date == "2023-02-01"      # UTC, stable
    assert got.deployer == DEPLOYER


def test_cache_eoa_sentinel(tmp_path):
    c = ContractIdentityCache(root=tmp_path)
    c.save(1, ContractIdentity(ADDR, False))
    got = c.load(1, ADDR)
    assert got is not None and got.is_contract is False


def test_cache_corrupt_file_is_a_miss(tmp_path):
    c = ContractIdentityCache(root=tmp_path)
    p = c._path(1, ADDR)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    assert c.load(1, ADDR) is None


def test_deployer_contract_count(tmp_path):
    c = ContractIdentityCache(root=tmp_path)
    for k in range(3):
        c.save(1, ContractIdentity("0x" + f"{k:02x}" * 20, True, deployer=DEPLOYER))
    c.save(1, ContractIdentity("0x" + "ee" * 20, True, deployer="0x" + "ff" * 20))
    assert c.deployer_contract_count(1, DEPLOYER) == 3
    assert c.deployer_contract_count(1, DEPLOYER.upper()) == 3   # case-insensitive
    assert c.deployer_contract_count(1, "0x" + "ff" * 20) == 1
    assert c.deployer_contract_count(1, "0x" + "00" * 20) == 0
    assert c.deployer_contract_count(2, DEPLOYER) == 0           # per-chain


# --- source (mocked transport) -------------------------------------------

def _transport_for(creation: dict, source: dict):
    def transport(url: str, timeout: float) -> bytes:
        payload = creation if "getcontractcreation" in url else source
        return json.dumps(payload).encode()
    return transport


def test_source_verified_contract():
    creation = {"status": "1", "result": [{
        "contractAddress": ADDR, "contractCreator": DEPLOYER,
        "txHash": "0x" + "11" * 32, "blockNumber": "123",
        "timestamp": "1675209600"}]}
    source = {"status": "1", "result": [{
        "ContractName": "RewardClaimHelper", "ABI": "[]"}]}
    src = ContractIdentitySource(lambda: "KEY",
                                 transport=_transport_for(creation, source))
    idy = src.fetch(1, ADDR)
    assert idy is not None
    assert idy.is_contract and idy.verified
    assert idy.name == "RewardClaimHelper"
    assert idy.deployer == DEPLOYER and idy.deployed_at == 1675209600


def test_source_eoa_has_no_creation_record():
    # getcontractcreation with an empty result → externally-owned account.
    src = ContractIdentitySource(
        lambda: "KEY",
        transport=_transport_for({"status": "0", "result": []}, {}))
    idy = src.fetch(1, ADDR)
    assert idy is not None and idy.is_contract is False


def test_source_unverified_keeps_provenance():
    # A contract with a creation record but no published source: still
    # valuable (deployer + date), just unnamed/unverified.
    creation = {"status": "1", "result": [{
        "contractAddress": ADDR, "contractCreator": DEPLOYER,
        "timestamp": "1675209600"}]}
    source = {"status": "1", "result": [{
        "ContractName": "", "ABI": "Contract source code not verified"}]}
    src = ContractIdentitySource(lambda: "KEY",
                                 transport=_transport_for(creation, source))
    idy = src.fetch(1, ADDR)
    assert idy is not None
    assert idy.is_contract and not idy.verified and idy.name is None
    assert idy.deployer == DEPLOYER          # provenance survives


def test_source_unsupported_chain_or_no_key():
    assert ContractIdentitySource(lambda: "KEY").fetch(987654, ADDR) is None
    assert ContractIdentitySource(lambda: None).supports(1) is False


# --- describe_identity badge logic ---------------------------------------

NOW = 1_700_000_000
OLD = NOW - 400 * 86400      # ~13 months ago
RECENT = NOW - 5 * 86400     # 5 days ago


def test_describe_verified_old_is_ok():
    idy = ContractIdentity(ADDR, True, name="Vault", verified=True,
                           deployer=DEPLOYER, deployed_at=OLD)
    b = describe_identity(idy, my_addresses=[], deployer_count=1, now_ts=NOW)
    assert b.level == "ok"
    assert "Vault" in b.text and "deployed" in b.text


def test_describe_new_contract_is_caution():
    idy = ContractIdentity(ADDR, True, name="Vault", verified=True,
                           deployer=DEPLOYER, deployed_at=RECENT)
    b = describe_identity(idy, my_addresses=[], now_ts=NOW)
    assert b.level == "caution" and "(new)" in b.text


def test_describe_unverified_is_warn():
    idy = ContractIdentity(ADDR, True, deployer=DEPLOYER, deployed_at=OLD)
    b = describe_identity(idy, my_addresses=[], now_ts=NOW)
    assert b.level == "warn" and "Unverified" in b.text


def test_describe_self_deployed():
    idy = ContractIdentity(ADDR, True, name="MyVault", verified=True,
                           deployer="0xME", deployed_at=OLD)
    b = describe_identity(idy, my_addresses=["0xme"], now_ts=NOW)
    assert "deployed by you" in b.text


def test_describe_deployer_cluster():
    idy = ContractIdentity(ADDR, True, name="Router", verified=True,
                           deployer=DEPLOYER, deployed_at=OLD)
    b = describe_identity(idy, my_addresses=[], deployer_count=5, now_ts=NOW)
    assert "same deployer as 4 of your contracts" in b.text


def test_describe_eoa_is_info():
    b = describe_identity(ContractIdentity(ADDR, False),
                          my_addresses=[], now_ts=NOW)
    assert b.level == "info" and "not a contract" in b.text.lower()
