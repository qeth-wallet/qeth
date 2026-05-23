"""Live test for the Blockscout token-discovery source."""

import pytest

from qeth.chains import DEFAULT_CHAINS
from qeth.tokens import BlockscoutSource

pytestmark = pytest.mark.network

ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
OP = next(c for c in DEFAULT_CHAINS if c.chain_id == 10)
VITALIK = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"


def test_blockscout_returns_tokens_for_known_address():
    src = BlockscoutSource()
    out = src.list_balances(OP, VITALIK)
    # Vitalik definitely holds tokens on Optimism; just verify schema.
    assert isinstance(out, list)
    assert len(out) > 0
    for b in out[:5]:
        assert b.contract.startswith("0x") and len(b.contract) == 42
        assert isinstance(b.balance_raw, int)
        assert isinstance(b.decimals, int)
        assert b.symbol  # non-empty string


def test_blockscout_supports_check():
    src = BlockscoutSource()
    assert src.supports(ETH)
    assert src.supports(OP)
    # A made-up chain shouldn't be supported.
    from qeth.chains import Chain
    fake = Chain(name="Fake", chain_id=999999, rpc_url="https://x")
    assert not src.supports(fake)
