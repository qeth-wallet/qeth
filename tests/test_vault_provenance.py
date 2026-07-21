"""Vault-provenance discovery core — pure logic + the explorer transfer-log
fetch (no Qt, no real network)."""

import json

from qeth.chains import DEFAULT_CHAINS
from qeth.token_discovery.vault_provenance import (
    incoming_transfer_txhashes,
    read_vault_assets,
    self_acquired_via_own_tx,
)
from qeth.transactions import fetch_incoming_transfer_logs

ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
TOKEN = "0x" + "aa" * 20
ASSET = "0x" + "bb" * 20
OWNER = "0x" + "7a" * 20


# --- read_vault_assets (asset() multicall pre-filter) ----------------------

class _MCP:
    def __init__(self, success, value):
        self.success = success
        self.value = value


class _FakeMulticall:
    def __init__(self, assets):
        self._assets = assets          # {token_lower: asset_int | None (revert)}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def add(self, token, calldata, decoder=None):
        v = self._assets.get(token.lower())
        return _MCP(v is not None, v if v is not None else 0)


class _FakeClient:
    def __init__(self, assets):
        self._assets = assets

    def multicall(self, batch_size=200):
        return _FakeMulticall(self._assets)


def test_read_vault_assets_keeps_resolving_and_drops_reverts():
    spam = "0x" + "cc" * 20
    client = _FakeClient({TOKEN.lower(): int(ASSET, 16), spam.lower(): None})
    assert read_vault_assets(client, [TOKEN, spam]) == {TOKEN.lower(): ASSET.lower()}


def test_read_vault_assets_rejects_self_and_non_address():
    client = _FakeClient({TOKEN.lower(): int(TOKEN, 16)})     # asset() == self
    assert read_vault_assets(client, [TOKEN]) == {}
    client2 = _FakeClient({TOKEN.lower(): (1 << 200)})        # not a 20-byte address
    assert read_vault_assets(client2, [TOKEN]) == {}


def test_read_vault_assets_empty():
    assert read_vault_assets(_FakeClient({}), []) == {}


# --- provenance from transfer logs -----------------------------------------

def test_incoming_transfer_txhashes_dedupes_in_order():
    rows = [{"transactionHash": "0xa"}, {"transactionHash": "0xb"},
            {"transactionHash": "0xa"}]
    assert incoming_transfer_txhashes(rows) == ["0xa", "0xb"]
    assert incoming_transfer_txhashes([]) == []


def test_self_acquired_true_when_owner_originated():
    tx_from = {"0xh1": "0x" + "ee" * 20, "0xh2": OWNER}
    assert self_acquired_via_own_tx(
        ["0xh1", "0xh2"], lambda h: tx_from.get(h), [OWNER]) is True


def test_self_acquired_false_for_airdrop():
    # every acquiring tx was sent by someone else → airdrop, not mine
    assert self_acquired_via_own_tx(
        ["0xh1", "0xh2"], lambda h: "0x" + "ee" * 20, [OWNER]) is False


def test_self_acquired_matches_any_of_my_accounts():
    other = "0x" + "b3" * 20                 # a second account of mine
    assert self_acquired_via_own_tx(
        ["0xh1"], lambda h: other, [OWNER, other]) is True


def test_self_acquired_bounded_by_max_lookups():
    seen = []
    def tf(h):
        seen.append(h)
        return "0x" + "ee" * 20              # never mine
    self_acquired_via_own_tx([f"0x{i}" for i in range(100)], tf, [OWNER], max_lookups=5)
    assert len(seen) == 5                    # stops after the cap


# --- fetch_incoming_transfer_logs (explorer logs API, fake transport) ------

def test_transfer_logs_query_shape_and_rows():
    cap = {}
    def ft(url, timeout):
        cap["url"] = url
        return json.dumps({"status": "1",
                           "result": [{"transactionHash": "0xa"}]}).encode()
    rows = fetch_incoming_transfer_logs(ETH, TOKEN, OWNER, get_api_key=lambda: None,
                                        transport=ft)
    assert rows == [{"transactionHash": "0xa"}]
    u = cap["url"].lower()
    assert "action=getlogs" in u
    assert "address=" + TOKEN.lower() in u
    assert "topic2=0x" + "0" * 24 + OWNER[2:].lower() in u    # to = owner


def test_transfer_logs_fails_over_etherscan_to_blockscout():
    def ft(url, timeout):
        if "etherscan" in url:
            return json.dumps({"status": "0", "message": "NOTOK",
                               "result": "Query error"}).encode()
        return json.dumps({"status": "1",
                           "result": [{"transactionHash": "0xb"}]}).encode()
    rows = fetch_incoming_transfer_logs(ETH, TOKEN, OWNER, get_api_key=lambda: "K",
                                        transport=ft)
    assert rows == [{"transactionHash": "0xb"}]


def test_transfer_logs_empty_is_not_an_error():
    def ft(url, timeout):
        return json.dumps({"status": "0", "message": "No records found",
                           "result": []}).encode()
    assert fetch_incoming_transfer_logs(ETH, TOKEN, OWNER, get_api_key=lambda: None,
                                        transport=ft) == []
