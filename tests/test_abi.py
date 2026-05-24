"""Hermetic tests for qeth.abi — Blockscout fetch + calldata decoder.

Live integration sits in test_network_abi.py.
"""

import json

import pytest

from qeth.abi import AbiSourceError, BlockscoutAbiSource, decode_call
from qeth.chains import DEFAULT_CHAINS


ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)

ERC20_ABI = [
    {
        "type": "function",
        "name": "transfer",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "approve",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

PROXY_ADMIN_ABI = [
    {
        "type": "function",
        "name": "admin",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
]


def _routed_transport(routes: dict[str, dict], log: list[str]):
    """Transport that maps URL → canned JSON response. Tests build
    a route table keyed by a substring of the URL."""
    def transport(url: str, timeout: float) -> bytes:
        log.append(url)
        for key, payload in routes.items():
            if key in url:
                return json.dumps(payload).encode()
        raise RuntimeError(f"unrouted URL in test: {url}")
    return transport


# --- BlockscoutAbiSource via v2 ------------------------------------------

class TestBlockscoutAbiSource:
    def test_verified_non_proxy_returns_own_abi(self):
        urls = []
        src = BlockscoutAbiSource(transport=_routed_transport({
            "/api/v2/smart-contracts/": {
                "is_verified": True,
                "abi": ERC20_ABI,
                "implementations": [],
            },
        }, urls))
        abi = src.fetch(ETH.chain_id, "0xabc")
        assert isinstance(abi, list)
        names = {e.get("name") for e in abi if e.get("type") == "function"}
        assert names == {"transfer", "approve"}

    def test_proxy_resolves_implementation_abi(self):
        """USDC-shape: the proxy address itself has a small admin-
        only ABI; the implementation has the real ERC-20 surface.
        ``fetch`` must merge both so a transfer call decodes."""
        proxy = "0xPROXY"
        impl = "0xIMPL"
        urls = []

        def route(url, timeout):
            urls.append(url)
            if "/api/v2/smart-contracts/" + proxy.lower() in url.lower():
                return json.dumps({
                    "is_verified": True,
                    "abi": PROXY_ADMIN_ABI,
                    "implementations": [{"address_hash": impl, "name": "Impl"}],
                }).encode()
            if "/api/v2/smart-contracts/" + impl.lower() in url.lower():
                return json.dumps({
                    "is_verified": True,
                    "abi": ERC20_ABI,
                    "implementations": [],
                }).encode()
            raise RuntimeError(f"unrouted: {url}")

        src = BlockscoutAbiSource(transport=route)
        abi = src.fetch(ETH.chain_id, proxy)
        assert isinstance(abi, list)
        names = {e.get("name") for e in abi if e.get("type") == "function"}
        # Both the proxy's admin-only methods AND the implementation's
        # real ERC-20 surface end up in the merged result.
        assert {"admin", "transfer", "approve"} <= names

    def test_unverified_returns_false(self):
        urls = []
        src = BlockscoutAbiSource(transport=_routed_transport({
            "/api/v2/smart-contracts/": {
                "is_verified": False,
                "abi": None,
                "implementations": [],
            },
        }, urls))
        assert src.fetch(ETH.chain_id, "0xabc") is False

    def test_proxy_cycle_terminates(self):
        """Pathological: contract A → impl B → impl A. The recursion
        must break by depth/seen tracking and not infinite-loop."""
        a = "0xaaaa"
        b = "0xbbbb"
        urls = []

        def route(url, timeout):
            urls.append(url)
            if "/api/v2/smart-contracts/" + a.lower() in url.lower():
                return json.dumps({
                    "is_verified": True, "abi": PROXY_ADMIN_ABI,
                    "implementations": [{"address_hash": b, "name": "B"}],
                }).encode()
            if "/api/v2/smart-contracts/" + b.lower() in url.lower():
                return json.dumps({
                    "is_verified": True, "abi": ERC20_ABI,
                    "implementations": [{"address_hash": a, "name": "A"}],
                }).encode()
            raise RuntimeError(f"unrouted: {url}")

        src = BlockscoutAbiSource(transport=route, max_proxy_depth=5)
        abi = src.fetch(ETH.chain_id, a)
        # Doesn't raise, returns at least A's + B's ABIs.
        assert isinstance(abi, list)
        assert any(e.get("name") == "admin" for e in abi)
        assert any(e.get("name") == "transfer" for e in abi)

    def test_v1_fallback_when_v2_unavailable(self):
        """If the v2 endpoint isn't reachable (older Blockscout, 404),
        fall back to the v1 ``getabi`` endpoint."""
        urls = []

        def route(url, timeout):
            urls.append(url)
            if "/api/v2/smart-contracts/" in url:
                # Simulate a 404-style payload (no is_verified field).
                return b'{"message":"Not found"}'
            if "module=contract&action=getabi" in url:
                return json.dumps({
                    "status": "1",
                    "result": json.dumps(ERC20_ABI),
                }).encode()
            raise RuntimeError(f"unrouted: {url}")

        src = BlockscoutAbiSource(transport=route)
        abi = src.fetch(ETH.chain_id, "0xabc")
        assert isinstance(abi, list)

    def test_unsupported_chain_raises(self):
        from qeth.chains import Chain
        src = BlockscoutAbiSource()
        fake = Chain(name="Fake", chain_id=999_999, rpc_url="https://x")
        with pytest.raises(AbiSourceError):
            src.fetch(fake.chain_id, "0xabc")


# --- _dedup_by_selector ---------------------------------------------------

class TestDedupBySelector:
    def test_drops_duplicate_functions(self):
        from qeth.abi import _dedup_by_selector
        merged = _dedup_by_selector([
            ERC20_ABI[0],   # transfer
            ERC20_ABI[1],   # approve
            ERC20_ABI[0],   # transfer (dup)
        ])
        assert sum(1 for e in merged if e.get("name") == "transfer") == 1
        assert sum(1 for e in merged if e.get("name") == "approve") == 1

    def test_keeps_non_function_entries(self):
        from qeth.abi import _dedup_by_selector
        events = [
            {"type": "event", "name": "Transfer", "inputs": []},
            {"type": "constructor", "inputs": []},
        ]
        out = _dedup_by_selector(events + ERC20_ABI)
        assert any(e.get("type") == "event" for e in out)
        assert any(e.get("type") == "constructor" for e in out)


# --- decode_call ----------------------------------------------------------

class TestDecodeCall:
    def test_decodes_erc20_transfer(self):
        calldata = (
            "0xa9059cbb"
            "0000000000000000000000005d6a4ba137d77df7c3cdd7131c430da5497c7ace"
            "000000000000000000000000000000000000000000000000000000001dcd6500"
        )
        out = decode_call(
            ERC20_ABI, calldata,
            address="0xdac17f958d2ee523a2206206994597c13d831ec7",
        )
        assert out is not None
        assert out["function"] == "transfer"
        # args is a list preserving declaration order; each entry has
        # name/type/value so the UI can render annotated signatures.
        assert [a["name"] for a in out["args"]] == ["_to", "_value"]
        assert [a["type"] for a in out["args"]] == ["address", "uint256"]
        to_arg, value_arg = out["args"]
        assert to_arg["value"].lower() \
            == "0x5d6a4ba137d77df7c3cdd7131c430da5497c7ace"
        assert value_arg["value"] == "500000000"

    def test_no_abi_returns_none(self):
        assert decode_call(None, "0xa9059cbb...") is None
        assert decode_call([], "0xa9059cbb...") is None

    def test_empty_calldata_returns_none(self):
        assert decode_call(ERC20_ABI, "") is None
        assert decode_call(ERC20_ABI, "0x") is None

    def test_unmatched_selector_returns_none(self):
        assert decode_call(
            ERC20_ABI, "0xdeadbeef00000000000000000000000000000000",
        ) is None
