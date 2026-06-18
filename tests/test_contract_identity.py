"""Tests for qeth.contract_identity — the cache, the Etherscan-v2 source
(mocked transport), and the describe_identity badge logic."""

from __future__ import annotations

import json


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


def test_cache_old_schema_is_a_miss(tmp_path):
    # A pre-v2 entry (valid identity, but no version / no name-tags) must
    # read as a miss so it gets re-fetched with the current shape.
    c = ContractIdentityCache(root=tmp_path)
    p = c._path(1, ADDR)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"address": ADDR, "is_contract": True, "name": "Old"}))
    assert c.load(1, ADDR) is None
    # And a freshly-saved entry round-trips (it carries the version).
    c.save(1, ContractIdentity(ADDR, True, name="New"))
    assert c.load(1, ADDR) is not None


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


def test_keyless_eoa_resolves_without_api_key():
    # No Etherscan key: an EOA recipient is still identified via eth_getCode
    # (keyless) + the keyless Blockscout label. This is the fresh-machine
    # case — the recipient row must resolve so familiarity can show.
    label_payload = {"addresses": {ADDR: {"tags": [
        {"tagType": "name", "name": "Binance: Hot Wallet", "ordinal": 5},
    ]}}}

    def transport(url, timeout):  # only LABELS_BASE is hit on the keyless path
        return json.dumps(label_payload).encode()
    src = ContractIdentitySource(
        lambda: None, transport=transport,
        get_code=lambda cid, addr: "0x")          # EOA → no bytecode
    idy = src.fetch(1, ADDR)
    assert idy is not None
    assert idy.is_contract is False
    assert idy.name_tag == "Binance: Hot Wallet"


def test_keyless_contract_stays_bare():
    # Without a key we can't usefully identify a CONTRACT (no name/verified/
    # deployer), so leave the row bare rather than cache a half-identity.
    src = ContractIdentitySource(
        lambda: None,
        get_code=lambda cid, addr: "0x60806040")   # bytecode → a contract
    assert src.fetch(1, ADDR) is None


def test_keyless_without_get_code_is_none():
    # No key and no eth_getCode probe wired → nothing to go on.
    assert ContractIdentitySource(lambda: None).fetch(1, ADDR) is None


def test_keyless_get_code_failure_is_none():
    def boom(cid, addr):
        raise RuntimeError("rpc down")
    src = ContractIdentitySource(lambda: None, get_code=boom)
    assert src.fetch(1, ADDR) is None


def test_source_unsupported_chain_or_no_key():
    # Unsupported chain + no get_code wired → still None (keyless can't probe).
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


# --- interaction count (familiarity) -------------------------------------

def _tx(from_addr, to_addr, h, input_data="0x"):
    from qeth.transactions import Transaction
    return Transaction(
        chain_id=1, hash=h, block_number=1, timestamp=0, nonce=0,
        from_addr=from_addr.lower(),
        to_addr=to_addr.lower() if to_addr else None,
        value_wei=0, gas_used=0, gas_price_wei=0,
        method_id=input_data[:10], input_data=input_data, success=True)


def _transfer_calldata(to):
    # transfer(address,uint256): selector + 32B-padded addr + 32B amount
    return "0xa9059cbb" + "00" * 12 + to[2:].lower() + "0" * 64


def test_cache_interaction_count(tmp_path):
    from qeth.transactions_cache import TransactionCache
    cache = TransactionCache(root=tmp_path)
    me1, me2 = "0x" + "11" * 20, "0x" + "22" * 20
    contract, other = "0x" + "cc" * 20, "0x" + "99" * 20
    cache.save(1, me1, [_tx(me1, contract, "0xa"), _tx(me1, contract, "0xb"),
                        _tx(me1, other, "0xc")])
    cache.save(1, me2, [_tx(me2, contract, "0xd")])
    assert cache.interaction_count(1, contract, [me1, me2]) == 3   # across accounts
    assert cache.interaction_count(1, contract, [me1]) == 2
    assert cache.interaction_count(1, other, [me1, me2]) == 1
    assert cache.interaction_count(1, "0x" + "00" * 20, [me1, me2]) == 0


def test_sent_to_count_includes_token_transfers(tmp_path):
    # The Send-dialog case: a token send's on-chain `to` is the token
    # contract, and the destination is in the transfer calldata.
    from qeth.transactions_cache import TransactionCache
    cache = TransactionCache(root=tmp_path)
    me = "0x" + "11" * 20
    token = "0x" + "dd" * 20       # e.g. USDT
    recip = "0x" + "cc" * 20
    cache.save(1, me, [
        _tx(me, token, "0xt1", _transfer_calldata(recip)),   # token send → recip
        _tx(me, token, "0xt2", _transfer_calldata(recip)),   # another
        _tx(me, recip, "0xd1"),                              # native send → recip
        _tx(me, token, "0xt3", _transfer_calldata("0x" + "ee" * 20)),  # elsewhere
    ])
    # Direct-only count sees just the native send — the old, wrong answer
    # for token sends.
    assert cache.interaction_count(1, recip, [me]) == 1
    # sent_to_count adds the two token transfers → 3.
    assert cache.sent_to_count(1, recip, [me]) == 3
    # Interacting with the token contract itself = the 3 transfer calls.
    assert cache.interaction_count(1, token, [me]) == 3


def test_cache_interaction_count_dedups_by_hash(tmp_path):
    from qeth.transactions_cache import TransactionCache
    cache = TransactionCache(root=tmp_path)
    me1, me2 = "0x" + "11" * 20, "0x" + "22" * 20
    contract = "0x" + "cc" * 20
    # Same tx cached under two of my accounts → counted once.
    cache.save(1, me1, [_tx(me1, contract, "0xsame")])
    cache.save(1, me2, [_tx(me1, contract, "0xsame")])
    assert cache.interaction_count(1, contract, [me1, me2]) == 1


def test_describe_interaction_familiar():
    idy = ContractIdentity(ADDR, True, name="Router", verified=True,
                           deployer=DEPLOYER, deployed_at=OLD)
    b = describe_identity(idy, my_addresses=[], interaction_count=1213, now_ts=NOW)
    assert "you've interacted 1,213×" in b.text and b.level == "ok"


def test_describe_first_interaction_is_caution():
    idy = ContractIdentity(ADDR, True, name="Router", verified=True,
                           deployer=DEPLOYER, deployed_at=OLD)
    b = describe_identity(idy, my_addresses=[], interaction_count=0, now_ts=NOW)
    assert "first interaction" in b.text and b.level == "caution"


def test_describe_eoa_interaction():
    eoa = ContractIdentity(ADDR, False)
    seen = describe_identity(eoa, my_addresses=[], interaction_count=5, now_ts=NOW)
    assert "sent here 5× before" in seen.text
    first = describe_identity(eoa, my_addresses=[], interaction_count=0, now_ts=NOW)
    assert "first time sending here" in first.text and first.level == "caution"


# --- public name-tags (Blockscout OLI metadata) --------------------------

def test_fetch_labels_parses_name_tag():
    # Service echoes a checksummed key; we lowercase it. "name" tags beat
    # "generic" ones, highest ordinal wins.
    checksummed = "0x07Da2d30E26802ED65a52859A50872cfA615bD0A"
    payload = {"addresses": {checksummed: {"tags": [
        {"tagType": "name", "name": "AladdinDAO: Deployer", "ordinal": 10},
        {"tagType": "generic", "name": "Contract Deployer", "ordinal": 0},
    ]}}}

    def transport(url, timeout):
        return json.dumps(payload).encode()
    src = ContractIdentitySource(lambda: "KEY", transport=transport)
    labels = src.fetch_labels(1, [checksummed.lower()])
    assert labels[checksummed.lower()] == "AladdinDAO: Deployer"


def test_describe_uses_deployer_label():
    idy = ContractIdentity(ADDR, True, name="RewardClaimHelper", verified=True,
                           deployer=DEPLOYER, deployed_at=OLD,
                           deployer_label="AladdinDAO: Deployer")
    b = describe_identity(idy, my_addresses=[], deployer_count=11, now_ts=NOW)
    assert "by AladdinDAO: Deployer" in b.text and "+10 of your contracts" in b.text


def test_describe_name_tag_is_headline():
    idy = ContractIdentity(ADDR, True, name="Vyper_contract", verified=True,
                           deployer=DEPLOYER, deployed_at=OLD,
                           name_tag="Curve.fi: 3pool")
    b = describe_identity(idy, my_addresses=[], now_ts=NOW)
    assert b.text.startswith("Curve.fi: 3pool")


def test_describe_labeled_eoa():
    eoa = ContractIdentity(ADDR, False, name_tag="Binance: Hot Wallet")
    b = describe_identity(eoa, my_addresses=[], interaction_count=5, now_ts=NOW)
    assert b.text.startswith("Binance: Hot Wallet") and b.level == "ok"


def test_describe_context_verb():
    idy = ContractIdentity(ADDR, True, name="Pool", verified=True,
                           deployer=DEPLOYER, deployed_at=OLD)
    sent = describe_identity(idy, my_addresses=[], interaction_count=5,
                             context="send", now_ts=NOW)
    assert "sent here 5× before" in sent.text
    first = describe_identity(idy, my_addresses=[], interaction_count=0,
                              context="send", now_ts=NOW)
    assert "first time sending here" in first.text and first.level == "caution"
    inter = describe_identity(idy, my_addresses=[], interaction_count=5,
                              context="interact", now_ts=NOW)
    assert "you've interacted 5×" in inter.text
