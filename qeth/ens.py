"""Tiny ENS reverse-lookup helper.

ENS reverse records aren't trustworthy on their own — anyone can
claim any name in their reverse record. To trust a name, the
forward lookup of that name must resolve back to the same address.
``lookup_ens_name`` does both steps and only returns a name when
the round-trip verifies.

Mainnet only. Even when a user is browsing on Polygon / Arbitrum /
etc., ENS reverse records live on Ethereum mainnet, so the resolver
talks to chain 1 regardless of the user's current view.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QThread, Signal

from . import USER_AGENT


log = logging.getLogger("qeth.ens")


def lookup_ens_name(rpc_url: str, address: str) -> Optional[str]:
    """Reverse-resolve ``address`` on mainnet ENS, verifying via a
    forward lookup. Returns the verified primary name, or None when
    no reverse record exists, the forward lookup mismatches, or any
    RPC error occurs (we treat all failures as "no name" rather
    than surfacing them — ENS data is informational, never blocking
    on it)."""
    try:
        # web3.py's ENS support is built into Web3 instances; one
        # fresh w3 per call is fine — we're not on the hot path.
        from eth_utils import to_checksum_address
        from web3 import Web3
        from web3.providers.rpc import HTTPProvider
    except ImportError:
        return None
    try:
        addr = to_checksum_address(address)
    except Exception:
        return None
    try:
        w3 = Web3(HTTPProvider(
            rpc_url,
            request_kwargs={
                "headers": {"User-Agent": USER_AGENT},
                "timeout": 15,
            },
        ))
        name = w3.ens.name(addr)  # type: ignore[union-attr]  # .ens is set (we built w3 with a provider), never the Empty sentinel
    except Exception as e:
        log.debug("ENS reverse lookup failed for %s: %s", addr, e)
        return None
    if not name:
        return None
    # Verify forward. Some versions of web3.py do this internally;
    # do it again here to be belt-and-suspenders against version
    # drift — the cost of a forged "vitalik.eth" claim on a random
    # address is high.
    try:
        forward = w3.ens.address(name)  # type: ignore[union-attr]  # see above
    except Exception:
        return None
    if not forward or forward.lower() != addr.lower():
        return None
    return name


class EnsReverseWorker(QThread):
    """One-shot reverse lookup off the Qt main thread. Emits
    ``resolved(address, name)`` where ``name`` is the empty string
    for "no verified name" — keeps the signature qint-safe and
    skips the "is this None or empty?" branch in slots."""

    resolved = Signal(str, str)

    def __init__(self, rpc_url: str, address: str, parent=None):
        super().__init__(parent)
        self._rpc_url = rpc_url
        self._address = address

    def run(self) -> None:
        name = lookup_ens_name(self._rpc_url, self._address) or ""
        self.resolved.emit(self._address, name)
