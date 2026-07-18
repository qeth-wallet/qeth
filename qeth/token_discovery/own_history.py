"""Discover tokens the user obtained through their OWN transactions.

Vault / LP tokens (yb-WBTC, Curve LP, …) aren't on curated lists, so ordinary
discovery drops them. But a token received in a transaction the user
ORIGINATED is a token they meant to hold — spam-resistant by construction. We
reconstruct that set locally by joining two on-disk caches per (chain, viewer):

  ``TransactionCache``  gives ``from_addr`` (the origin) but no token legs;
  ``ActivityCache``     gives the ERC-20s the viewer RECEIVED (``inn``) per hash.

A tx counts when any of the user's addresses originated it and it succeeded;
its received-token contracts are collected. Cross-account sends work: an
incoming tx sits in the recipient's tx cache with ``from_addr`` = the sender
(still one of the user's addresses). A tx whose activity was never resolved
(never viewed) is skipped — a receipt backfill is out of scope for v1.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..activity_cache import ActivityCache
    from ..transactions_cache import TransactionCache


def discover_own_tokens(
    chain_id: int,
    my_addresses: Iterable[str],
    *,
    viewers: Iterable[str] | None = None,
    tx_cache: TransactionCache | None = None,
    activity_cache: ActivityCache | None = None,
) -> set[str]:
    """The set of ERC-20 contract addresses (lower-case) the user received in
    transactions they originated on ``chain_id``. Pure disk I/O — reads
    already-resolved activities from the cache; it never re-parses a tx.

    ``viewers`` scopes WHICH wallets' caches are read (defaults to all of
    ``my_addresses``), scanned in the given order — so a caller can prioritise
    the on-screen wallet. The ORIGIN check always spans every ``my_addresses``,
    so a cross-account send (wallet A → wallet B, both ours) is still caught
    when only B's cache is scanned.
    """
    # Imported lazily: activity_cache → tx_activity → abi → token_discovery,
    # so a module-level import here would form a cycle when this package is
    # imported via abi/transactions.
    from ..activity_cache import ActivityCache
    from ..transactions_cache import TransactionCache
    txc = tx_cache if tx_cache is not None else TransactionCache()
    acc = activity_cache if activity_cache is not None else ActivityCache()
    mine = {a.lower() for a in my_addresses}   # origin test spans ALL our addrs
    scan = viewers if viewers is not None else my_addresses
    ordered: list[str] = []
    seen: set[str] = set()
    for v in scan:                             # ordered, de-duplicated
        vl = v.lower()
        if vl not in seen:
            seen.add(vl)
            ordered.append(vl)
    found: set[str] = set()
    for viewer in ordered:
        txs = txc.load(chain_id, viewer) or []
        acts = acc.load(chain_id, viewer) or {}
        for tx in txs:
            if tx.from_addr.lower() not in mine:
                continue                       # not originated by us
            if not tx.success or tx.pending or tx.dropped:
                continue
            act = acts.get(tx.hash)
            if act is None:
                continue                       # activity never resolved
            for leg in act.inn:
                if leg.contract:               # None = native coin
                    found.add(leg.contract.lower())
    return found
