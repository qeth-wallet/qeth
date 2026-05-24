"""Live test for the Blockscout transaction-history source."""

import pytest

from qeth.chains import DEFAULT_CHAINS
from qeth.transactions import BlockscoutTransactionSource

pytestmark = pytest.mark.network

ETH = next(c for c in DEFAULT_CHAINS if c.chain_id == 1)
# An address with plenty of mainnet history. Same one we used to probe
# the API while designing this module.
ACTIVE = "0x7a16ff8270133f063aab6c9977183d9e72835428"


def test_blockscout_returns_recent_txs():
    src = BlockscoutTransactionSource()
    out = src.list_transactions(ETH, ACTIVE, limit=5)
    assert isinstance(out, list)
    assert len(out) > 0
    for tx in out:
        assert tx.hash.startswith("0x") and len(tx.hash) == 66
        assert tx.block_number > 0
        assert tx.timestamp > 0
        # The page is sorted newest-first; sanity-check the ordering.
    for prev, nxt in zip(out, out[1:]):
        assert prev.block_number >= nxt.block_number


def test_pagination_returns_older_page():
    src = BlockscoutTransactionSource()
    first = src.list_transactions(ETH, ACTIVE, page=1, limit=3)
    assert len(first) == 3
    older = src.list_transactions(ETH, ACTIVE, page=2, limit=3)
    # Pages 1 and 2 must not overlap by hash; page 2 must be strictly
    # older or equal in block number to page 1's last entry.
    page1_hashes = {tx.hash for tx in first}
    assert all(tx.hash not in page1_hashes for tx in older)
    if older:
        assert older[0].block_number <= first[-1].block_number
