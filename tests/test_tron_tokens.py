"""Tron on the token path: TronGrid holdings, the CoinGecko Tron list
(base58, ``chainId: null``), DefiLlama's ``tron:<T…>`` keys, explorer links
and the per-chain Multicall3 address — all converting at the edge so qeth's
core keeps hex addresses."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from qeth.address import tron_from_hex
from qeth.chains import DEFAULT_CHAINS, TRON, Chain
from qeth.explorer import explorer_url
from qeth.token_discovery import RateLimited, TronGridSource
from qeth.token_discovery.tokenlists import TokenListEntry, _from_tokenlists_schema

TRON_CHAIN = next(c for c in DEFAULT_CHAINS if c.family == TRON)
ETH = DEFAULT_CHAINS[0]
USDT_T = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT = "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c"
SPAM_T = tron_from_hex("0x" + "5a" * 20)
HOLDER = "0x" + "11" * 20


def _serve(monkeypatch, payload):
    seen = []

    def urlopen(req, timeout=None):
        seen.append(req.full_url)
        if isinstance(payload, Exception):
            raise payload
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return seen


def test_tron_is_a_default_chain_with_its_own_multicall():
    assert TRON_CHAIN.chain_id == 728126428
    assert TRON_CHAIN.native_decimals == 6 and not TRON_CHAIN.is_evm
    # Multicall3's Tron deployment, TEazPvZwDjDtFeJupyo7QunvnrnUjPH8ED.
    assert tron_from_hex(TRON_CHAIN.multicall_address) == "TEazPvZwDjDtFeJupyo7QunvnrnUjPH8ED"


def test_eth_client_uses_the_chains_multicall_address():
    from qeth.chain import MULTICALL3, EthClient
    assert EthClient(ETH).multicall_address == MULTICALL3
    assert EthClient(TRON_CHAIN).multicall_address == TRON_CHAIN.multicall_address
    mc = EthClient(TRON_CHAIN).multicall()
    assert mc.block_number() is not None
    assert mc._queued[0][0] == TRON_CHAIN.multicall_address


class TestTronGridSource:
    def test_lists_held_trc20s_with_list_metadata(self, monkeypatch):
        seen = _serve(monkeypatch, {"success": True, "data": [{"trc20": [
            {USDT_T: "4894284965"}, {SPAM_T: "1000"}, {USDT_T[:-1] + "x": "5"},
            {tron_from_hex("0x" + "77" * 20): "0"}]}]})
        known = {USDT: TokenListEntry(728126428, USDT, "USDT", "Tether USD", 6, "coingecko")}
        src = TronGridSource(lambda cid, a: known.get(a))
        out = src.list_balances(TRON_CHAIN, HOLDER)
        assert seen[0] == f"https://api.trongrid.io/v1/accounts/{tron_from_hex(HOLDER)}"
        by = {b.contract.lower(): b for b in out}
        assert set(by) == {USDT, "0x" + "5a" * 20}   # bad + zero rows dropped
        assert (by[USDT].symbol, by[USDT].decimals, by[USDT].balance_raw) == ("USDT", 6, 4894284965)
        assert by["0x" + "5a" * 20].symbol == ""      # unknown: left to metadata

    def test_unactivated_account_holds_nothing(self, monkeypatch):
        _serve(monkeypatch, {"success": True, "data": []})
        assert TronGridSource().list_balances(TRON_CHAIN, HOLDER) == []

    def test_rate_limit_is_reported_as_such(self, monkeypatch):
        _serve(monkeypatch, urllib.error.HTTPError("u", 429, "x", {}, None))
        with pytest.raises(RateLimited):
            TronGridSource().list_balances(TRON_CHAIN, HOLDER)

    def test_only_tron(self):
        assert TronGridSource().supports(TRON_CHAIN)
        assert not TronGridSource().supports(ETH)


def test_coingecko_tron_list_parses_base58_without_chain_ids():
    data = {"tokens": [
        {"chainId": None, "address": USDT_T, "symbol": "USDT", "name": "Tether",
         "decimals": 6, "logoURI": "https://x/usdt.png"},
        {"chainId": None, "address": "not-an-address", "symbol": "BAD"},
    ]}
    entries = list(_from_tokenlists_schema(data, "coingecko", 728126428))
    assert [(e.chain_id, e.address, e.decimals) for e in entries] == [(728126428, USDT, 6)]


def test_defillama_keys_tron_tokens_by_base58(monkeypatch):
    from qeth.pricing.defillama import DefiLlamaPrices
    seen = _serve(monkeypatch, {"coins": {
        f"tron:{USDT_T}": {"price": 0.9997, "timestamp": 1},
        "coingecko:tron": {"price": 0.34, "timestamp": 1}}})
    out = DefiLlamaPrices().fetch(TRON_CHAIN, [USDT], include_native=True)
    assert f"tron:{USDT_T}" in seen[-1]
    assert str(out[USDT].price_usd) == "0.9997" and str(out[""].price_usd) == "0.34"


@pytest.mark.parametrize("kind, value, want", [
    ("tx", "0x" + "ab" * 32, "https://tronscan.org/#/transaction/" + "ab" * 32),
    ("address", USDT, f"https://tronscan.org/#/address/{USDT_T}"),
    ("token", USDT, f"https://tronscan.org/#/token20/{USDT_T}"),
])
def test_tronscan_links(kind, value, want):
    assert explorer_url(TRON_CHAIN, kind, value, ref_addr=HOLDER) == want


def test_etherscan_links_unchanged():
    assert explorer_url(ETH, "token", USDT, ref_addr=HOLDER) == \
        f"https://etherscan.io/token/{USDT}?a={HOLDER}"
    assert explorer_url(ETH, "tx", "0x12") == "https://etherscan.io/tx/0x12"
    assert explorer_url(Chain("X", 5, "", explorer=""), "tx", "0x12") is None
