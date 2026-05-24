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
    first = src.list_transactions(ETH, ACTIVE, limit=3)
    assert len(first) == 3
    older = src.list_transactions(
        ETH, ACTIVE, before_block=first[-1].block_number, limit=3,
    )
    # Every entry in the older page must be strictly before the cursor.
    for tx in older:
        assert tx.block_number < first[-1].block_number
