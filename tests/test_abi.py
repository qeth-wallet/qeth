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

    def test_v1_fallback_resolves_proxy_via_rpc(self):
        """When v2 is down, a proxy still resolves: read the impl slot from
        the chain (storage_reader) and merge the implementation's ABI."""
        IMPL = "0x" + "11" * 20

        def route(url, timeout):
            if "/api/v2/smart-contracts/" in url:
                return b'{"message":"Server Error"}'   # v2 down (no is_verified)
            if "getabi" in url and IMPL[2:] in url.lower():
                return json.dumps({"status": "1",
                                   "result": json.dumps(ERC20_ABI)}).encode()
            if "getabi" in url:                          # the proxy's own ABI
                return json.dumps({"status": "1",
                                   "result": json.dumps(PROXY_ADMIN_ABI)}).encode()
            raise RuntimeError(f"unrouted: {url}")

        # storage_reader returns the impl in the legacy (zeppelinos) slot.
        ZEPP = "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"
        def reader(cid, addr, slot):
            return "0x" + "00" * 12 + "11" * 20 if slot == ZEPP else "0x" + "00" * 32

        src = BlockscoutAbiSource(transport=route, storage_reader=reader)
        abi = src.fetch(ETH.chain_id, "0xproxy")
        names = {e["name"] for e in abi if e.get("type") == "function"}
        assert "approve" in names and "admin" in names   # impl + proxy merged


class TestEtherscanV2AbiSource:
    def test_getabi_with_proxy_merge(self):
        from qeth.abi import EtherscanV2AbiSource
        IMPL = "11" * 20

        def route(url, timeout):
            assert "chainid=137" in url and "module=contract&action=getabi" in url
            if IMPL in url.lower():
                return json.dumps({"status": "1",
                                   "result": json.dumps(ERC20_ABI)}).encode()
            return json.dumps({"status": "1",
                               "result": json.dumps(PROXY_ADMIN_ABI)}).encode()

        EIP1967 = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
        reader = lambda cid, addr, slot: (
            "0x" + "00" * 12 + IMPL if slot == EIP1967 else "0x" + "00" * 32)
        src = EtherscanV2AbiSource(lambda: "KEY", transport=route,
                                   storage_reader=reader)
        assert src.supports(137)
        abi = src.fetch(137, "0xproxy")
        names = {e["name"] for e in abi if e.get("type") == "function"}
        assert "approve" in names and "admin" in names

    def test_no_key_not_supported(self):
        from qeth.abi import EtherscanV2AbiSource
        assert not EtherscanV2AbiSource(lambda: "").supports(137)

    def test_unverified_returns_false(self):
        from qeth.abi import EtherscanV2AbiSource
        route = lambda url, t: json.dumps(
            {"status": "0", "result": "Contract source code not verified"}).encode()
        src = EtherscanV2AbiSource(lambda: "KEY", transport=route)
        assert src.fetch(137, "0xabc") is False


class TestRoutedAbiSource:
    def _blockscout(self, abi_or_false):
        src = BlockscoutAbiSource(transport=lambda u, t: json.dumps(
            {"is_verified": True, "abi": abi_or_false} if abi_or_false
            else {"is_verified": False}).encode())
        return src

    def test_prefers_primary_when_it_returns_abi(self):
        from qeth.abi import RoutedAbiSource, EtherscanV2AbiSource
        primary = EtherscanV2AbiSource(lambda: "KEY", transport=lambda u, t:
            json.dumps({"status": "1", "result": json.dumps(ERC20_ABI)}).encode())
        secondary_hits = []
        secondary = BlockscoutAbiSource(transport=lambda u, t:
            secondary_hits.append(u) or b'{"is_verified": false}')
        out = RoutedAbiSource(primary, secondary).fetch(137, "0xabc")
        assert isinstance(out, list) and not secondary_hits   # never hit fallback

    def test_falls_back_on_primary_error(self):
        from qeth.abi import RoutedAbiSource, EtherscanV2AbiSource
        def boom(url, t):
            raise AbiSourceError("etherscan 500")
        primary = EtherscanV2AbiSource(lambda: "KEY", transport=boom)
        secondary = self._blockscout(ERC20_ABI)
        out = RoutedAbiSource(primary, secondary).fetch(137, "0xabc")
        assert isinstance(out, list)   # Blockscout served it

    def test_set_storage_reader_propagates(self):
        from qeth.abi import RoutedAbiSource, EtherscanV2AbiSource
        p = EtherscanV2AbiSource(lambda: "KEY")
        s = BlockscoutAbiSource()
        RoutedAbiSource(p, s).set_storage_reader("READER")
        assert p.storage_reader == "READER" and s.storage_reader == "READER"


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


class TestDecodeCallTuples:
    """Tuple/struct args turn into branch nodes with per-component
    children so the dialog can render them indented with each inner
    field's Solidity type alongside its value."""

    REGISTER_ABI = [{
        "type": "function",
        "name": "register",
        "stateMutability": "nonpayable",
        "inputs": [{
            "name": "data",
            "type": "tuple",
            "components": [
                {"name": "label", "type": "string"},
                {"name": "owner", "type": "address"},
                {"name": "secret", "type": "bytes32"},
            ],
        }],
        "outputs": [],
    }]

    def test_decodes_struct_into_children(self):
        from eth_abi import encode
        from eth_utils import function_signature_to_4byte_selector
        selector = function_signature_to_4byte_selector(
            "register((string,address,bytes32))"
        )
        encoded = encode(
            ["(string,address,bytes32)"],
            [("qeth",
              "0x7a16ff8270133f063aab6c9977183d9e72835428",
              b"\x99" * 32)],
        )
        calldata = "0x" + selector.hex() + encoded.hex()

        out = decode_call(self.REGISTER_ABI, calldata)
        assert out is not None
        assert out["function"] == "register"
        arg = out["args"][0]
        assert arg["name"] == "data"
        assert arg["type"] == "tuple"
        # Branch node — children, not a flat value.
        children = arg["children"]
        assert len(children) == 3
        names = [c["name"] for c in children]
        types = [c["type"] for c in children]
        assert names == ["label", "owner", "secret"]
        assert types == ["string", "address", "bytes32"]
        # Inner bytes32 stringifies to canonical 0x… hex.
        assert children[2]["value"] == "0x" + "99" * 32


class TestStringify:
    """The decoded-value coercer is the only place where we control
    how exotic Solidity types render in the dialog."""

    def test_bytes_renders_as_hex(self):
        from qeth.abi import _stringify
        assert _stringify(b"\x99\x23\xeb\x94") == "0x9923eb94"

    def test_dict_value_renders_nested_bytes_as_hex(self):
        """Solidity tuple/struct args come back as dicts from web3.py.
        The default str(d) would print inner bytes as Python's b'\\x…'
        repr — we must walk the dict and stringify each entry."""
        from qeth.abi import _stringify
        out = _stringify({
            "label": "qeth",
            "secret": b"\x99\x23\xeb\x94",
            "amount": 1000,
        })
        assert "0x9923eb94" in out
        assert "b'" not in out  # no Python bytes-repr leaking through

    def test_list_of_bytes(self):
        from qeth.abi import _stringify
        out = _stringify([b"\xaa", b"\xbb\xcc"])
        assert out == "[0xaa, 0xbbcc]"

    def test_none_value(self):
        from qeth.abi import _stringify
        assert _stringify(None) == ""

    def test_string_type_gets_quoted(self):
        """With a Solidity string type hint we quote the value so
        the rendered ``label: string = "qeth"`` reads as a string
        literal rather than a bare identifier."""
        from qeth.abi import _stringify
        assert _stringify("qeth", type_hint="string") == '"qeth"'

    def test_string_array_quotes_each_element(self):
        from qeth.abi import _stringify
        assert _stringify(["a", "b"], type_hint="string[]") == '["a", "b"]'

    def test_address_is_not_quoted(self):
        """Hex addresses already self-identify as values; quoting
        them would just add noise."""
        from qeth.abi import _stringify
        addr = "0x7a16fF8270133F063aAb6C9977183D9e72835428"
        assert _stringify(addr, type_hint="address") == addr


# --- event-log decoding ---------------------------------------------------

from qeth.abi import decode_event, _TRANSFER_TOPIC, _APPROVAL_TOPIC, _event_topic0

A = "0x" + "11" * 20
B = "0x" + "22" * 20
CONTRACT = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def _topic_addr(addr: str) -> str:
    return "0x000000000000000000000000" + addr[2:]


def _u256_data(n: int) -> str:
    return "0x" + f"{n:064x}"


class TestDecodeEvent:
    def test_erc20_transfer_without_abi(self):
        log = {"address": CONTRACT,
               "topics": [_TRANSFER_TOPIC, _topic_addr(A), _topic_addr(B)],
               "data": _u256_data(100)}
        ev = decode_event(log)
        assert ev["event"] == "Transfer"
        assert ev["contract"] == CONTRACT
        names = [(a["name"], a["type"], a["value"].lower()) for a in ev["args"]]
        assert names == [
            ("from", "address", A), ("to", "address", B),
            ("value", "uint256", "100"),
        ]

    def test_erc721_transfer_has_tokenid(self):
        log = {"address": CONTRACT,
               "topics": [_TRANSFER_TOPIC, _topic_addr(A), _topic_addr(B),
                          _u256_data(7)],
               "data": "0x"}
        ev = decode_event(log)
        assert [a["name"] for a in ev["args"]] == ["from", "to", "tokenId"]
        assert ev["args"][2]["value"] == "7"

    def test_erc20_approval_without_abi(self):
        log = {"address": CONTRACT,
               "topics": [_APPROVAL_TOPIC, _topic_addr(A), _topic_addr(B)],
               "data": _u256_data(5)}
        ev = decode_event(log)
        assert ev["event"] == "Approval"
        assert [a["name"] for a in ev["args"]] == ["owner", "spender", "value"]

    def test_unknown_event_without_abi_is_none(self):
        log = {"address": CONTRACT, "topics": ["0x" + "de" * 32], "data": "0x"}
        assert decode_event(log) is None

    def test_unknown_event_decodes_with_abi(self):
        topic = _event_topic0("Deposit(address,uint256)")
        log = {"address": CONTRACT,
               "topics": [topic, _topic_addr(A)], "data": _u256_data(123)}
        abi = [{"type": "event", "name": "Deposit", "anonymous": False,
                "inputs": [
                    {"name": "dst", "type": "address", "indexed": True},
                    {"name": "wad", "type": "uint256", "indexed": False}]}]
        ev = decode_event(log, abi)
        assert ev["event"] == "Deposit"
        assert ev["args"][0]["name"] == "dst"
        assert ev["args"][0]["value"].lower() == A
        assert ev["args"][1] == {"name": "wad", "type": "uint256", "value": "123"}

    def test_handles_hexbytes_topics(self):
        from hexbytes import HexBytes
        log = {"address": CONTRACT,
               "topics": [HexBytes(_TRANSFER_TOPIC), HexBytes(_topic_addr(A)),
                          HexBytes(_topic_addr(B))],
               "data": HexBytes(_u256_data(1))}
        ev = decode_event(log)
        assert ev["event"] == "Transfer" and ev["args"][2]["value"] == "1"
