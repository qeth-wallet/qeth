"""Live-RPC tests for qeth.chain.

Skipped by default. Run with::

    uv run pytest -m network

These exercise the real Ethereum RPC (whatever is configured as the
default chain's ``rpc_url``) plus Multicall3 on mainnet. They protect
against:

- the RPC provider changing wire format,
- Multicall3 disappearing,
- our ABI selector or encoding drifting from the canonical contracts.
"""


import pytest

from qeth.chain import EthClient
from qeth.chains import DEFAULT_CHAINS

pytestmark = pytest.mark.network


ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)

# Vitalik. Useful as a stable target because the address publicly holds
# diverse balances and isn't going to disappear.
VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"

# Canonical token addresses on Ethereum mainnet.
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
DAI  = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
MKR  = "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2"  # bytes32 name/symbol


@pytest.fixture(scope="module")
def client():
    return EthClient(ETH)


class TestProviderHealth:
    def test_block_number_recent(self, client):
        n = client.get_block_number()
        # Some plausible floor — Ethereum is past block 17M as of early
        # 2023; assert "newer than that" to detect a totally-wrong chain.
        assert n > 17_000_000

    def test_chain_id_matches_config(self, client):
        assert client.chain_id() == ETH.chain_id

    def test_get_balance_returns_int(self, client):
        bal = client.get_balance(VITALIK)
        assert isinstance(bal, int)
        assert bal >= 0


class TestMulticallBalances:
    def test_known_holder(self, client):
        out = client.multicall_erc20_balances([USDC, USDT, DAI], VITALIK)
        # All three should resolve (the call won't have reverted), even
        # if vitalik happens to hold zero of any individual token.
        assert set(out.keys()) == {USDC.lower(), USDT.lower(), DAI.lower()}
        for v in out.values():
            assert isinstance(v, int) and v >= 0


class TestMulticallMetadata:
    def test_string_name_symbol(self, client):
        out = client.multicall_erc20_metadata([USDC, USDT, DAI])
        assert out[USDC.lower()]["symbol"] == "USDC"
        assert out[USDC.lower()]["decimals"] == 6
        assert "Coin" in out[USDC.lower()]["name"]  # "USD Coin"
        assert out[USDT.lower()]["symbol"] == "USDT"
        assert out[USDT.lower()]["decimals"] == 6
        assert out[DAI.lower()]["symbol"] == "DAI"
        assert out[DAI.lower()]["decimals"] == 18

    def test_legacy_bytes32_token(self, client):
        """MKR returns name/symbol as bytes32 instead of string."""
        out = client.multicall_erc20_metadata([MKR])
        assert out[MKR.lower()]["symbol"] == "MKR"
        assert out[MKR.lower()]["name"] == "Maker"
        assert out[MKR.lower()]["decimals"] == 18

    def test_non_token_contract_is_omitted(self, client):
        """A random non-ERC-20 address (e.g., an EOA-like contract) just
        won't appear in the result — guard against schema drift that
        might leak garbage entries."""
        # Multicall3 itself isn't an ERC-20:
        MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
        out = client.multicall_erc20_metadata([MULTICALL3])
        assert MULTICALL3.lower() not in out
