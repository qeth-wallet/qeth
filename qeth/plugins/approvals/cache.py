"""Persisted per-(chain, account) approvals state.

Stores the discovered allowances plus ``last_block`` = the highest Approval-log
block the scan has indexed, so re-opening the tab renders the last-known caps
instantly and the incremental scan windows the Approval logs from there.

JSON at ``~/.qeth/approvals/<chain_id>/<address_lower>.json``. ``allowance`` and
``price_usd`` are stored as strings — a uint256 allowance outgrows JSON's safe
integer range and Decimal has no native JSON type.

``last_block`` semantics changed with event-log discovery: it used to be the tx
cache head (a recent block), it is now the Approval-log cursor. A v1 (pre-event
-log) cache carries the OLD meaning, so ``_SCHEMA_VERSION`` gates it out — an
unversioned/stale cache reads as a MISS, forcing a cold full scan from block 0
that re-discovers the complete approval set (the tx-walk under-counted). Bump
this whenever the persisted shape or a field's meaning changes.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path

from .discovery import ApprovalRow

log = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".qeth" / "approvals"
_SCHEMA_VERSION = 2      # v1 = tx-history discovery (last_block = tx head)


class ApprovalsCache:
    def __init__(self, root: Path | None = None):
        # Resolve CACHE_DIR at instantiation so tests that monkeypatch it win.
        self.root = root if root is not None else CACHE_DIR

    def _path(self, chain_id: int, address: str) -> Path:
        return self.root / str(chain_id) / f"{address.lower()}.json"

    def load(self, chain_id: int, address: str) -> tuple[list[ApprovalRow], int] | None:
        """``(rows, last_block)`` or None when there's no (readable) cache."""
        try:
            data = json.loads(self._path(chain_id, address).read_text())
        except (OSError, ValueError):
            return None
        # Reject a pre-event-log (or otherwise stale-schema) cache: its
        # last_block is the old tx-head meaning, which would poison the
        # incremental log cursor. Treat as a miss → cold full scan.
        if data.get("v") != _SCHEMA_VERSION:
            return None
        rows: list[ApprovalRow] = []
        for d in data.get("rows", []):
            try:
                price = d.get("price_usd")
                rows.append(ApprovalRow(
                    token=d["token"], spender=d["spender"],
                    allowance=int(d["allowance"]),
                    symbol=d.get("symbol", ""), name=d.get("name", ""),
                    decimals=int(d.get("decimals", 18)),
                    spender_label=d.get("spender_label", ""),
                    price_usd=Decimal(price) if price is not None else None,
                    token_balance=int(d.get("token_balance", 0))))
            except (KeyError, ValueError, TypeError):
                continue                              # skip a corrupt row, keep the rest
        return rows, int(data.get("last_block", 0))

    def save(self, chain_id: int, address: str,
             rows: list[ApprovalRow], last_block: int) -> None:
        path = self._path(chain_id, address)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "v": _SCHEMA_VERSION,
                "last_block": int(last_block),
                "rows": [{
                    "token": r.token, "spender": r.spender,
                    "allowance": str(r.allowance), "symbol": r.symbol,
                    "name": r.name, "decimals": r.decimals,
                    "spender_label": r.spender_label,
                    "price_usd": str(r.price_usd) if r.price_usd is not None else None,
                    "token_balance": str(r.token_balance),
                } for r in rows],
            }))
        except OSError:
            log.debug("approvals cache save failed", exc_info=True)
