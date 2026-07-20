"""Transaction history discovery — abstract source + Blockscout impl.

The wallet asks a ``TransactionSource`` for past transactions of an
address on a chain. Sources are pluggable so we can stack fallbacks
later (Etherscan v2, Otterscan ``ots_*`` against a user-supplied RPC,
``trace_filter`` on an Erigon node). Today the only implementation is
Blockscout's Etherscan-compatible ``/api?module=account&action=txlist``.

The parsing logic is split out as a free function so it's unit-testable
without HTTP, mirroring the ``qeth.plugins.tokens.risk._parse_report`` pattern.
"""

import enum
import json
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from collections.abc import Callable

from . import USER_AGENT
from .chains import Chain
from .token_discovery import BLOCKSCOUT_INSTANCES, ETHERSCAN_V2_CHAINS, ETHERSCAN_V2_BASE


class TxDirection(enum.Enum):
    SENT = "sent"
    RECEIVED = "received"
    SELF = "self"      # from == to == viewer
    UNRELATED = "unrelated"


@dataclass(frozen=True)
class Transaction:
    """A single top-level transaction, normalized across sources.

    Internal calls and ERC-20 ``Transfer`` events are intentionally NOT
    folded in here — keep this record matching what's on-chain at the
    transaction level. A separate API can expose ``txlistinternal`` /
    ``tokentx`` rows when the UI needs them.
    """
    chain_id: int
    hash: str
    block_number: int
    timestamp: int            # unix seconds
    nonce: int
    from_addr: str            # lowercased
    to_addr: str | None    # lowercased; None for contract creations
    value_wei: int            # raw native amount
    gas_used: int
    gas_price_wei: int
    # 10-char hex selector ("0xa9059cbb") for contract calls; empty
    # string for plain native transfers (input == "0x"). Useful for
    # quick UI labeling without decoding the full ABI.
    method_id: str
    input_data: str           # full calldata hex incl. "0x" prefix
    success: bool
    # Local-only: True while the tx is sitting in the mempool after a
    # qeth-driven broadcast and the receipt hasn't landed yet. Always
    # False on entries that come from a TransactionSource (Blockscout
    # never returns mempool entries). Flipped to False by
    # PendingTxWatcher once the receipt arrives, alongside filling in
    # block_number / gas_used / gas_price_wei / success.
    pending: bool = False
    # Local-only terminal state: the nonce was consumed by a *different*
    # tx (replacement / the user re-sent), so this hash will never
    # confirm. Distinct from a reverted tx (success=False) — a dropped
    # tx never made it on-chain at all. Set by PendingTxWatcher.
    dropped: bool = False
    # Local-only: hex of the signed transaction, kept only while pending
    # so PendingTxWatcher can re-broadcast it if the RPC silently drops
    # it (DRPC sometimes acks a tx it never propagates). Public data (no
    # key material); cleared once the tx confirms or is dropped.
    raw_signed: str | None = None

    def direction(self, viewer: str) -> TxDirection:
        v = viewer.lower()
        f = self.from_addr.lower()
        t = (self.to_addr or "").lower()
        if f == v and t == v:
            return TxDirection.SELF
        if f == v:
            return TxDirection.SENT
        if t == v:
            return TxDirection.RECEIVED
        return TxDirection.UNRELATED


class TransactionSourceError(Exception):
    pass


class UnsupportedChain(TransactionSourceError):
    pass


class TransactionSource(ABC):
    """A backend that lists transactions for an address on a chain.

    Returns newest-first within each page. Pagination is by 1-based
    page index plus a per-page ``limit`` — avoids the block-boundary
    edge case that a block-cursor strategy has when several txs from
    a single wallet land in the same block."""

    @abstractmethod
    def list_transactions(
        self,
        chain: Chain,
        address: str,
        page: int = 1,
        limit: int = 50,
        before_block: int | None = None,
    ) -> list[Transaction]:
        """Newest-first. ``before_block`` (when set) caps results at that
        block (explorer ``endblock``) — the block-cursor used to page
        beyond the explorer's ``page × offset ≤ 10000`` window: keep
        ``page=1`` and walk ``before_block`` down from the oldest row."""
        ...

    def supports(self, chain: Chain) -> bool:
        return True


# Transport: anything callable that takes a URL and returns the raw
# response bytes. The default uses urllib; tests inject a fake.
Transport = Callable[[str, float], bytes]


def _urllib_transport(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _parse_blockscout_tx(entry: dict, chain_id: int) -> Transaction | None:
    """Convert one Etherscan-shaped result row into a ``Transaction``.

    Returns ``None`` for rows we can't make sense of (missing hash etc.)
    so a single bad entry doesn't sink the whole page."""
    try:
        h = entry["hash"]
        from_addr = (entry.get("from") or "").lower()
        # Blockscout uses empty string for contract creations.
        to_raw = entry.get("to") or ""
        to_addr = to_raw.lower() if to_raw else None
        input_data = entry.get("input") or "0x"
        method_id = entry.get("methodId") or ""
        if not method_id and input_data and input_data != "0x" and len(input_data) >= 10:
            method_id = input_data[:10]
        # Success heuristic: txreceipt_status is canonical, isError is a
        # fallback some instances return. Treat both missing as "assume
        # success" — Blockscout omits the field on very old txs.
        receipt = (entry.get("txreceipt_status") or "").strip()
        is_err = (entry.get("isError") or "0").strip()
        if receipt == "1":
            success = True
        elif receipt == "0":
            success = False
        else:
            success = (is_err == "0")
        return Transaction(
            chain_id=chain_id,
            hash=h,
            block_number=int(entry["blockNumber"]),
            timestamp=int(entry.get("timeStamp") or 0),
            nonce=int(entry.get("nonce") or 0),
            from_addr=from_addr,
            to_addr=to_addr,
            value_wei=int(entry.get("value") or 0),
            gas_used=int(entry.get("gasUsed") or 0),
            gas_price_wei=int(entry.get("gasPrice") or 0),
            method_id=method_id,
            input_data=input_data,
            success=success,
        )
    except (KeyError, ValueError, TypeError):
        return None


class EtherscanV2TransactionSource(TransactionSource):
    """Etherscan v2 multichain ``module=account&action=txlist``.

    Response shape is the same v1 schema Blockscout returns, so the
    free ``_parse_blockscout_tx`` function does double duty — only
    the URL changes. Requires a global API key fetched dynamically
    so the user can paste it at runtime."""

    def __init__(
        self,
        get_api_key,
        timeout: float = 20.0,
        transport: Transport | None = None,
        supported_chains: frozenset[int] | None = None,
    ):
        self._get_api_key = get_api_key
        self.timeout = timeout
        self._transport: Transport = transport or _urllib_transport
        self._supported = (
            supported_chains
            if supported_chains is not None
            else ETHERSCAN_V2_CHAINS
        )

    def supports(self, chain: Chain) -> bool:
        if chain.chain_id not in self._supported:
            return False
        return bool(self._get_api_key())

    def list_transactions(
        self,
        chain: Chain,
        address: str,
        page: int = 1,
        limit: int = 50,
        before_block: int | None = None,
    ) -> list[Transaction]:
        key = self._get_api_key()
        if not key:
            raise UnsupportedChain("No Etherscan API key configured")
        params = [
            ("chainid", str(chain.chain_id)),
            ("module", "account"),
            ("action", "txlist"),
            ("address", address),
            ("sort", "desc"),
            ("page", str(max(1, int(page)))),
            ("offset", str(max(1, int(limit)))),
            ("apikey", key),
        ]
        if before_block is not None:
            params.append(("endblock", str(int(before_block))))
        url = f"{ETHERSCAN_V2_BASE}?" + urllib.parse.urlencode(params)
        raw = self._transport(url, self.timeout)
        data = json.loads(raw)

        if data.get("status") != "1":
            # The detail is in `result` for Etherscan (message is just
            # "NOTOK"); check both.
            detail = (str(data.get("message") or "") + " "
                      + str(data.get("result") or "")).lower()
            if "no transactions" in detail or "not found" in detail:
                return []
            # Explorer page-window cap: `page × offset` must be ≤ 10000, so
            # txlist can't page past ~10k rows. Treat hitting it as the end
            # of available history rather than surfacing the raw error.
            if ("result window is too large" in detail
                    or "pageno x offset" in detail):
                return []
            raise TransactionSourceError(
                data.get("result") or data.get("message") or "etherscan error"
            )

        out: list[Transaction] = []
        for entry in data.get("result") or []:
            tx = _parse_blockscout_tx(entry, chain.chain_id)
            if tx is not None:
                out.append(tx)
        return out


class RoutedTransactionSource(TransactionSource):
    """Prefer ``primary`` when it supports the chain, fall back to
    ``secondary``. Mirrors ``RoutedTokenSource``."""

    def __init__(self, primary: TransactionSource, secondary: TransactionSource):
        self._primary = primary
        self._secondary = secondary

    def supports(self, chain: Chain) -> bool:
        return self._primary.supports(chain) or self._secondary.supports(chain)

    def list_transactions(
        self, chain: Chain, address: str, page: int = 1, limit: int = 50,
        before_block: int | None = None,
    ) -> list[Transaction]:
        if self._primary.supports(chain):
            return self._primary.list_transactions(
                chain, address, page, limit, before_block)
        if self._secondary.supports(chain):
            return self._secondary.list_transactions(
                chain, address, page, limit, before_block)
        raise UnsupportedChain(
            f"No transaction source supports chain {chain.chain_id}"
        )


class BlockscoutTransactionSource(TransactionSource):
    """Etherscan-compatible ``/api?module=account&action=txlist``.

    Returns top-level (external) transactions for an address, sorted
    newest first. The address appears as either ``from`` or ``to`` on
    each row — direction is determined by the caller via
    ``Transaction.direction(viewer)``.
    """

    def __init__(
        self,
        instances: dict[int, str] | None = None,
        timeout: float = 20.0,
        transport: Transport | None = None,
    ):
        self.instances = instances if instances is not None else BLOCKSCOUT_INSTANCES
        self.timeout = timeout
        self._transport: Transport = transport or _urllib_transport

    def supports(self, chain: Chain) -> bool:
        return chain.chain_id in self.instances

    def list_transactions(
        self,
        chain: Chain,
        address: str,
        page: int = 1,
        limit: int = 50,
        before_block: int | None = None,
    ) -> list[Transaction]:
        base = self.instances.get(chain.chain_id)
        if not base:
            raise UnsupportedChain(
                f"No Blockscout instance configured for chain {chain.chain_id}"
            )
        params = [
            ("module", "account"),
            ("action", "txlist"),
            ("address", address),
            ("sort", "desc"),
            ("page", str(max(1, int(page)))),
            ("offset", str(max(1, int(limit)))),
        ]
        if before_block is not None:
            params.append(("endblock", str(int(before_block))))
        url = f"{base.rstrip('/')}/api?" + urllib.parse.urlencode(params)
        raw = self._transport(url, self.timeout)
        data = json.loads(raw)

        # Etherscan-compatible: status "0" with "No transactions found"
        # is a valid empty result, not an error.
        if data.get("status") != "1":
            detail = (str(data.get("message") or "") + " "
                      + str(data.get("result") or "")).lower()
            if "no transactions" in detail or "not found" in detail:
                return []
            # Explorer page-window cap (page × offset ≤ 10000): we've paged
            # as deep as txlist allows — treat as the end of history.
            if ("result window is too large" in detail
                    or "pageno x offset" in detail):
                return []
            raise TransactionSourceError(
                data.get("result") or data.get("message") or "blockscout error"
            )

        out: list[Transaction] = []
        for entry in data.get("result") or []:
            tx = _parse_blockscout_tx(entry, chain.chain_id)
            if tx is not None:
                out.append(tx)
        return out


# keccak256("Approval(address,address,uint256)") — the ERC-20 Approval event.
_APPROVAL_TOPIC0 = (
    "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
)


class ApprovalLogSource:
    """ERC-20 ``Approval(owner, spender, value)`` event logs via the explorer
    logs API (``module=logs&action=getLogs``), filtering ``topic0=Approval AND
    topic1=owner`` across every token contract at once — Etherscan v2 when a key
    covers the chain, else the chain's Blockscout instance (both expose the same
    Etherscan-compatible logs endpoint).

    This is the approvals discovery source of record: an Approval log is emitted
    by EVERY path that grants an allowance — a plain ``approve`` tx, an EIP-2612
    ``permit``, an approval set via an internal router/aggregator call — so it
    catches allowances the account's own top-level ``approve`` calldata misses.
    And filtering on the indexed ``owner`` topic returns only this account's
    logs across all tokens in one query, so a >10k-tx account costs a handful of
    windowed requests instead of paging its whole transaction history.

    Returns raw log rows (Etherscan JSON: ``address`` / ``topics`` /
    ``blockNumber``), ascending, capped at the explorer's ~1000-row limit per
    call. The caller windows by advancing ``from_block`` to the highest block
    seen; an empty list means the end of the range.
    """

    def __init__(
        self,
        get_api_key,
        instances: dict[int, str] | None = None,
        etherscan_chains: frozenset[int] | None = None,
        timeout: float = 20.0,
        transport: Transport | None = None,
    ):
        self._get_api_key = get_api_key
        self.instances = instances if instances is not None else BLOCKSCOUT_INSTANCES
        self._etherscan_chains = (
            etherscan_chains if etherscan_chains is not None else ETHERSCAN_V2_CHAINS
        )
        self.timeout = timeout
        self._transport: Transport = transport or _urllib_transport

    def supports(self, chain: Chain) -> bool:
        if chain.chain_id in self._etherscan_chains and self._get_api_key():
            return True
        return chain.chain_id in self.instances

    @staticmethod
    def _owner_topic(owner: str) -> str:
        h = owner[2:] if owner.startswith("0x") else owner
        return "0x" + "0" * 24 + h.lower()

    def fetch(self, chain: Chain, owner: str, from_block: int = 0) -> list[dict]:
        params = [
            ("module", "logs"),
            ("action", "getLogs"),
            ("fromBlock", str(max(0, int(from_block)))),
            ("toBlock", "latest"),
            ("topic0", _APPROVAL_TOPIC0),
            ("topic1", self._owner_topic(owner)),
            ("topic0_1_opr", "and"),
        ]
        key = self._get_api_key()
        if chain.chain_id in self._etherscan_chains and key:
            all_params = [("chainid", str(chain.chain_id)), *params, ("apikey", key)]
            url = f"{ETHERSCAN_V2_BASE}?" + urllib.parse.urlencode(all_params)
        else:
            base = self.instances.get(chain.chain_id)
            if not base:
                raise UnsupportedChain(
                    f"No Approval-log source supports chain {chain.chain_id}"
                )
            url = f"{base.rstrip('/')}/api?" + urllib.parse.urlencode(params)

        raw = self._transport(url, self.timeout)
        data = json.loads(raw)
        if data.get("status") != "1":
            detail = (str(data.get("message") or "") + " "
                      + str(data.get("result") or "")).lower()
            # Empty result is a valid "no logs in range", not an error. The
            # explorers phrase it variously ("No logs found", "No records
            # found", "not found").
            if ("no logs" in detail or "no records" in detail
                    or "not found" in detail or "no transactions" in detail):
                return []
            # Some instances cap a single logs response window; treat as end.
            if ("result window is too large" in detail
                    or "pageno x offset" in detail):
                return []
            raise TransactionSourceError(
                data.get("result") or data.get("message") or "getLogs error"
            )
        result = data.get("result")
        return result if isinstance(result, list) else []
