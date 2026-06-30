"""Tests for qeth.ens_write — the ENS write-calldata builders.

Qt-free and network-free: every builder returns ``(to_addr, data_hex)`` we can
assert byte-for-byte. Locks in the selectors, the ABI encoding, the contenthash
round-trip against ``ens_app.decode_contenthash``, and that subnode creation
routes to the registry (unwrapped) vs the NameWrapper (wrapped).
"""

from eth_abi import encode as abi_encode

from qeth.ens_app import (
    ENS_ETH_CONTROLLER, ENS_ETH_REGISTRAR, ENS_NAME_WRAPPER, ENS_REGISTRY,
    _labelhash, decode_contenthash, namehash,
)
from qeth import ens_write


RESOLVER = ens_write.PUBLIC_RESOLVER
NAME = "vitalik.eth"
ADDR = "0x" + "ab" * 20
# A real dag-pb CIDv1 (the form ENS tooling and gateways use).
IPFS_URL = "ipfs://bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"


def _selector(data_hex: str) -> str:
    return data_hex[2:10]


def _body(data_hex: str) -> bytes:
    return bytes.fromhex(data_hex[10:])


class TestRecordWrites:
    def test_set_addr(self):
        to, data = ens_write.set_addr(RESOLVER, NAME, ADDR)
        assert to == RESOLVER
        assert _selector(data) == "d5fa2b00"
        assert _body(data) == abi_encode(
            ["bytes32", "address"], [namehash(NAME), ADDR])

    def test_set_text(self):
        to, data = ens_write.set_text(RESOLVER, NAME, "url", "https://x.example")
        assert to == RESOLVER
        assert _selector(data) == "10f13a8c"
        assert _body(data) == abi_encode(
            ["bytes32", "string", "string"],
            [namehash(NAME), "url", "https://x.example"])

    def test_set_coin_addr(self):
        payload = ens_write.eth_addr_bytes(ADDR)
        coin = ens_write.COIN_TYPES["OP"]
        to, data = ens_write.set_coin_addr(RESOLVER, NAME, coin, payload)
        assert to == RESOLVER
        assert _selector(data) == "8b95dd71"
        assert _body(data) == abi_encode(
            ["bytes32", "uint256", "bytes"], [namehash(NAME), coin, payload])

    def test_set_contenthash(self):
        to, data = ens_write.set_contenthash(RESOLVER, NAME, IPFS_URL)
        assert to == RESOLVER
        assert _selector(data) == "304e6ade"
        assert _body(data) == abi_encode(
            ["bytes32", "bytes"],
            [namehash(NAME), ens_write.encode_contenthash(IPFS_URL)])

    def test_set_resolver_targets_registry(self):
        to, data = ens_write.set_resolver(NAME)
        assert to == ENS_REGISTRY            # registry write, not the resolver
        assert _selector(data) == "1896f70a"
        assert _body(data) == abi_encode(
            ["bytes32", "address"], [namehash(NAME), RESOLVER])

    def test_eth_addr_bytes(self):
        assert ens_write.eth_addr_bytes(ADDR) == bytes.fromhex("ab" * 20)
        assert len(ens_write.eth_addr_bytes(ADDR)) == 20


class TestContenthashRoundTrip:
    def test_ipfs_round_trips(self):
        raw = ens_write.encode_contenthash(IPFS_URL)
        assert raw[:2] == b"\xe3\x01"        # ipfs-ns codec
        assert decode_contenthash("0x" + raw.hex()) == IPFS_URL

    def test_ipns_codec(self):
        url = "ipns://" + IPFS_URL[len("ipfs://"):]
        raw = ens_write.encode_contenthash(url)
        assert raw[:2] == b"\xe5\x01"        # ipns-ns codec
        assert decode_contenthash("0x" + raw.hex()) == url

    def test_empty_clears(self):
        assert ens_write.encode_contenthash("") == b""
        assert ens_write.encode_contenthash("   ") == b""

    def test_rejects_non_ipfs(self):
        for bad in ("https://x.example", "bafy...", "ipfs://zzz"):
            try:
                ens_write.encode_contenthash(bad)
            except ValueError:
                continue
            raise AssertionError(f"expected ValueError for {bad!r}")


class TestSubnode:
    def test_unwrapped_targets_registry_with_labelhash(self):
        to, data = ens_write.add_subnode(
            NAME, "blog", ADDR, wrapped=False)
        assert to == ENS_REGISTRY
        assert _selector(data) == "5ef2c7f0"
        assert _body(data) == abi_encode(
            ["bytes32", "bytes32", "address", "address", "uint64"],
            [namehash(NAME), _labelhash("blog"), ADDR,
             ens_write.PUBLIC_RESOLVER, 0])

    def test_wrapped_targets_namewrapper_with_string_label(self):
        to, data = ens_write.add_subnode(
            NAME, "blog", ADDR, wrapped=True, fuses=0, expiry=123)
        assert to == ENS_NAME_WRAPPER
        assert _selector(data) == "24c1af44"
        assert _body(data) == abi_encode(
            ["bytes32", "string", "address", "address",
             "uint64", "uint32", "uint64"],
            [namehash(NAME), "blog", ADDR, ens_write.PUBLIC_RESOLVER,
             0, 0, 123])


class TestRenew:
    def test_renew_targets_controller_with_bare_label(self):
        # renew takes the LABEL ('vitalik'), not the full name or a namehash.
        to, data = ens_write.renew("vitalik", 2 * ens_write.SECONDS_PER_YEAR)
        assert to == ENS_ETH_CONTROLLER
        assert _selector(data) == "acf1a841"
        assert _body(data) == abi_encode(
            ["string", "uint256"], ["vitalik", 2 * ens_write.SECONDS_PER_YEAR])

    def test_seconds_per_year_is_365_days(self):
        assert ens_write.SECONDS_PER_YEAR == 365 * 24 * 60 * 60


class TestTransfer:
    FROM = "0x" + "11" * 20
    TO = "0x" + "22" * 20

    def test_unwrapped_moves_erc721_on_registrar(self):
        # Unwrapped: ERC-721 safeTransferFrom on the BaseRegistrar, tokenId =
        # uint256(labelhash(label)).
        to, data = ens_write.transfer_name(
            NAME, self.FROM, self.TO, wrapped=False)
        assert to == ENS_ETH_REGISTRAR
        assert _selector(data) == "42842e0e"
        token_id = int.from_bytes(_labelhash("vitalik"), "big")
        assert _body(data) == abi_encode(
            ["address", "address", "uint256"],
            [self.FROM, self.TO, token_id])

    def test_wrapped_moves_erc1155_on_namewrapper(self):
        # Wrapped: ERC-1155 safeTransferFrom on the NameWrapper, id =
        # uint256(namehash), amount 1, empty data.
        to, data = ens_write.transfer_name(
            NAME, self.FROM, self.TO, wrapped=True)
        assert to == ENS_NAME_WRAPPER
        assert _selector(data) == "f242432a"
        token_id = int.from_bytes(namehash(NAME), "big")
        assert _body(data) == abi_encode(
            ["address", "address", "uint256", "uint256", "bytes"],
            [self.FROM, self.TO, token_id, 1, b""])
