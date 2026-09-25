"""TronClient transport (failover, error decoding) and fee estimation, with
the HTTP layer faked — the numbers are the live mainnet ones from the
research (docs/tron.md)."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from qeth.chains import TRON, Chain
from qeth.tron import fees as fees_mod
from qeth.tron.client import BlockRef, TronClient, TronError, TronTransportError
from qeth.tron.fees import (
    TronAccountNotActivated, build_tx, estimate_fee, trc20_transfer_data,
)
from qeth.tron.tx import TransferContract, TriggerSmartContract

CHAIN = Chain("Tron", 728126428, "", "TRX", family=TRON, native_decimals=6,
              api_url="https://a.invalid", api_fallbacks=("https://b.invalid",))
OWNER = "0x" + "11" * 20
TO = "0x" + "22" * 20
USDT = "0x" + "a6" * 20


class FakeHTTP:
    """Routes urlopen by URL; each route is a list of responses consumed in
    order (an Exception is raised, anything else is returned as JSON)."""

    def __init__(self, routes: dict[str, list]):
        self.routes = routes
        self.seen: list[tuple[str, dict | None]] = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.seen.append((req.full_url, body))
        queue = self.routes[req.full_url]
        resp = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(resp, Exception):
            raise resp
        return io.BytesIO(json.dumps(resp).encode())


def _http_error(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


@pytest.fixture
def http(monkeypatch):
    def install(routes):
        fake = FakeHTTP(routes)
        monkeypatch.setattr("urllib.request.urlopen", fake)
        return fake
    fees_mod._params_cache.clear()
    return install


def test_rate_limit_fails_over_to_the_next_endpoint(http):
    fake = http({
        "https://a.invalid/wallet/getaccount": [_http_error(429)],
        "https://b.invalid/wallet/getaccount": [{"balance": 5}],
    })
    assert TronClient(CHAIN).get_balance(OWNER) == 5
    # Addresses go out in the 41… hex form (no `visible`).
    assert fake.seen[0][1] == {"address": "41" + "11" * 20}


def test_all_endpoints_down_is_a_transport_error(http):
    http({
        "https://a.invalid/wallet/getaccount": [urllib.error.URLError("dns")],
        "https://b.invalid/wallet/getaccount": [_http_error(503)],
    })
    with pytest.raises(TronTransportError):
        TronClient(CHAIN).get_account(OWNER)


def test_a_node_refusal_is_final_and_readable(http):
    http({"https://a.invalid/wallet/broadcasthex": [
        {"result": False, "code": "SIGERROR",
         "message": "5472616e73616374696f6e2065787069726564"}]})   # hex text
    with pytest.raises(TronError, match="Transaction expired"):
        TronClient(CHAIN).broadcast(b"\x0a\x00")


def test_blocks_parse(http):
    http({"https://a.invalid/walletsolidity/getblock": [{
        "blockID": "00" * 32,
        "block_header": {"raw_data": {"number": 7, "timestamp": 1000}}}]})
    assert TronClient(CHAIN).solid_block() == BlockRef(7, "00" * 32, 1000)


def test_constant_call_revert_raises(http):
    http({"https://a.invalid/wallet/triggerconstantcontract": [
        {"result": {"result": False, "message": "REVERT opcode executed"}}]})
    with pytest.raises(TronError, match="REVERT"):
        TronClient(CHAIN).trigger_constant(OWNER, USDT, b"\x01")


# --- fees -------------------------------------------------------------------------

PARAMS = {"chainParameter": [
    {"key": "getTransactionFee", "value": 1000},
    {"key": "getEnergyFee", "value": 100},
    {"key": "getCreateAccountFee", "value": 100000},
    {"key": "getCreateNewAccountFeeInSystemContract", "value": 1000000},
]}


def _fee_routes(*, to_exists=True, resources=None, energy=None):
    base = "https://a.invalid"
    return {
        f"{base}/wallet/getchainparameters": [PARAMS],
        f"{base}/wallet/getaccount": [{"balance": 10**9}] + (
            [{"balance": 1} if to_exists else {}]),
        f"{base}/wallet/getaccountresource": [resources if resources is not None
                                              else {"freeNetLimit": 600}],
        f"{base}/wallet/triggerconstantcontract": [
            {"result": {"result": True}, "energy_used": energy or 0}],
        f"{base}/wallet/getcontract": [{}],      # no energy sharing
    }


def test_trx_transfer_on_free_bandwidth_burns_nothing(http):
    http(_fee_routes())
    fee = estimate_fee(TronClient(CHAIN), TransferContract(OWNER, TO, 10**6))
    assert 260 <= fee.bandwidth <= 275
    assert fee.total_burn == 0


def test_bandwidth_burn_is_all_or_nothing(http):
    http(_fee_routes(resources={"freeNetLimit": 600, "freeNetUsed": 400}))
    fee = estimate_fee(TronClient(CHAIN), TransferContract(OWNER, TO, 10**6))
    assert fee.bandwidth_burn == fee.bandwidth * 1000     # 200 left < size


def test_trx_to_a_new_account_pays_activation(http):
    http(_fee_routes(to_exists=False))
    fee = estimate_fee(TronClient(CHAIN), TransferContract(OWNER, TO, 10**6))
    assert fee.activation == 1_000_000
    assert fee.bandwidth_burn == 100_000      # free bandwidth doesn't apply
    assert fee.total_burn == 1_100_000


def test_trc20_energy_is_burned_beyond_staked(http):
    http(_fee_routes(energy=64285,
                     resources={"freeNetLimit": 600, "EnergyLimit": 10000}))
    c = TriggerSmartContract(OWNER, USDT, trc20_transfer_data(TO, 10**6))
    fee = estimate_fee(TronClient(CHAIN), c)
    assert fee.energy == 64285
    assert fee.energy_burn == (64285 - 10000) * 100
    assert fee.fee_limit == int(64285 * 100 * 1.5) + 1


def test_an_unactivated_sender_is_explained(http):
    routes = _fee_routes()
    routes["https://a.invalid/wallet/getaccount"] = [{}]
    http(routes)
    with pytest.raises(TronAccountNotActivated, match="never received TRX"):
        estimate_fee(TronClient(CHAIN), TransferContract(OWNER, TO, 1))


def test_trc20_calldata_uses_the_20_byte_body():
    data = trc20_transfer_data("0x" + "ab" * 20, 5)
    assert data[:4].hex() == "a9059cbb"
    assert data[4:36] == bytes(12) + bytes.fromhex("ab" * 20)
    assert int.from_bytes(data[36:], "big") == 5


def test_build_tx_references_the_solid_block(http):
    block = {"block_header": {"raw_data": {"number": 0x65a6, "timestamp": 1000}}}
    http({
        "https://a.invalid/walletsolidity/getblock": [{**block, "blockID": "11" * 32}],
        "https://a.invalid/wallet/getblock": [{**block, "blockID": "22" * 32}],
    })
    tx = build_tx(TronClient(CHAIN), TransferContract(OWNER, TO, 1))
    assert tx.ref_block_bytes == bytes.fromhex("65a6")
    assert tx.ref_block_hash == bytes.fromhex("11" * 8)
    assert tx.expiration == 1000 + fees_mod.EXPIRATION_MS



# --- energy sharing (a dapp's "energy subsidy") -----------------------------------

DEPLOYER = "0x" + "33" * 20
ROUTER = "0x" + "a3" * 20


def _sharing(http, contract: dict, deployer_res: dict, energy=335_747, owner_res=None):
    routes = _fee_routes(energy=energy)
    routes["https://a.invalid/wallet/getcontract"] = [contract]
    # The caller's resources first, then the deployer's.
    routes["https://a.invalid/wallet/getaccountresource"] = [
        owner_res or {"freeNetLimit": 600}, deployer_res]
    http(routes)
    return estimate_fee(TronClient(CHAIN), TriggerSmartContract(OWNER, ROUTER, b"\x35"))


def test_a_one_percent_contract_charges_the_caller_a_hundredth(http):
    """SunSwap's UniversalRouter (1%): the real swap 8aa357… used 335,747
    energy, the contract paid 332,389 and the caller 3,358 (0.3358 TRX) —
    charging all of it overstated a ~2 TRX swap as 30+ TRX."""
    fee = _sharing(http, {"origin_address": "41" + DEPLOYER[2:],
                          "consume_user_resource_percent": 1,
                          "origin_energy_limit": 1_000_000},
                   {"EnergyLimit": 1_967_951_136, "EnergyUsed": 1_367_292_900})
    assert fee.energy == 335_747 and fee.energy_by_contract == 332_389
    assert fee.energy_burn == 3_358 * 100
    # The cap stays on the total — if the subsidy ran dry, the caller pays.
    assert fee.fee_limit == int(335_747 * 100 * 1.5) + 1


@pytest.mark.parametrize("contract, deployer_res, by_contract", [
    # capped by the contract's per-call limit
    ({"consume_user_resource_percent": 1, "origin_energy_limit": 100_000},
     {"EnergyLimit": 10**9}, 100_000),
    # capped by what the deployer has staked
    ({"consume_user_resource_percent": 1, "origin_energy_limit": 10**6},
     {"EnergyLimit": 50_000}, 50_000),
    # 100%: the caller pays everything (most contracts — USDT included)
    ({"consume_user_resource_percent": 100, "origin_energy_limit": 10**6},
     {"EnergyLimit": 10**9}, 0),
    # proto3: a 0% contract omits the field — its deployer pays it all
    ({"origin_energy_limit": 10**6}, {"EnergyLimit": 10**9}, 335_747),
    # …and an absent limit is 0: the deployer pays nothing
    ({"consume_user_resource_percent": 1}, {"EnergyLimit": 10**9}, 0),
])
def test_the_deployers_share_follows_java_tron(http, contract, deployer_res, by_contract):
    fee = _sharing(http, {"origin_address": "41" + DEPLOYER[2:], **contract}, deployer_res)
    assert fee.energy_by_contract == by_contract
    assert fee.energy_burn == (335_747 - by_contract) * 100


def test_the_deployer_calling_its_own_contract_pays_itself(http):
    fee = _sharing(http, {"origin_address": "41" + OWNER[2:],
                          "consume_user_resource_percent": 1,
                          "origin_energy_limit": 10**6}, {"EnergyLimit": 10**9})
    assert fee.energy_by_contract == 0


def test_an_unknown_sharing_is_charged_in_full(http):
    routes = _fee_routes(energy=335_747)
    routes["https://a.invalid/wallet/getcontract"] = [_http_error(400)]
    routes["https://b.invalid/wallet/getcontract"] = [_http_error(400)]
    http(routes)
    fee = estimate_fee(TronClient(CHAIN), TriggerSmartContract(OWNER, ROUTER, b"\x35"))
    assert fee.energy_by_contract == 0 and fee.energy_burn == 335_747 * 100
