"""Tron transaction history from TronGrid's indexed ``/v1`` API.

``TronGridTransactionSource`` implements qeth's ``TransactionSource`` over
``/v1/accounts/{addr}/transactions`` (newest first, ``fingerprint``-paged).
Rows become ordinary ``Transaction`` records in qeth's internal form: ``0x``
hashes and addresses, ``nonce = -1`` (Tron has none — lists order by time)
and ``fee`` = what the chain reports it burned.

The history plugin pages older rows with a block cursor ("at or below block
N"); TronGrid filters by timestamp, so the cursor block's timestamp is taken
from rows already seen, else read from the block's header.
"""

from __future__ import annotations

import threading
from typing import Any

from ..address import tron_from_hex
from ..chains import TRON, Chain
from ..transactions import Transaction, TransactionSource, TransactionSourceError
from .client import TRONGRID_INSTANCES, TronClient, TronError

# TronGrid's page cap.
_MAX_LIMIT = 200


def _addr(v: Any) -> str | None:
    """A ``41…`` hex address from the API → internal lower-case ``0x`` form."""
    if not isinstance(v, str) or len(v) != 42 or not v.startswith("41"):
        return None
    return "0x" + v[2:].lower()


def parse_tron_tx(row: dict, chain_id: int) -> Transaction | None:
    """One ``/v1/accounts/{addr}/transactions`` row → ``Transaction``, or None
    for a row without a recognisable contract."""
    raw = row.get("raw_data") or {}
    contracts = raw.get("contract") or []
    tx_id = row.get("txID")
    if not contracts or not isinstance(tx_id, str):
        return None
    c = contracts[0]
    value = (c.get("parameter") or {}).get("value") or {}
    owner = _addr(value.get("owner_address"))
    if owner is None:
        return None
    ctype = c.get("type")
    data = ""
    if ctype == "TriggerSmartContract":
        to = _addr(value.get("contract_address"))
        amount = int(value.get("call_value") or 0)
        data = str(value.get("data") or "")
    else:
        # TransferContract, and the resource / vote contracts — which carry a
        # receiver (if any) and an amount under various names.
        to = _addr(value.get("to_address") or value.get("receiver_address"))
        amount = int(value.get("amount") or value.get("balance")
                     or value.get("frozen_balance") or value.get("unfreeze_balance")
                     or 0)
    ret = (row.get("ret") or [{}])[0]
    fee = max(int(ret.get("fee") or 0),
              int(row.get("net_fee") or 0) + int(row.get("energy_fee") or 0))
    return Transaction(
        chain_id=chain_id,
        hash="0x" + tx_id.lower(),
        block_number=int(row.get("blockNumber") or 0),
        timestamp=int(row.get("block_timestamp") or 0) // 1000,
        nonce=-1,
        from_addr=owner,
        to_addr=to,
        value_wei=amount,
        gas_used=int(row.get("energy_usage_total") or 0),
        gas_price_wei=0,
        method_id=("0x" + data[:8].lower()) if len(data) >= 8 else "",
        input_data="0x" + data.lower(),
        success=ret.get("contractRet", "SUCCESS") == "SUCCESS",
        fee=fee,
    )


class TronGridTransactionSource(TransactionSource):
    """The account's SENT transactions (``only_from``), newest first."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout
        # block number → timestamp (ms), learned from rows returned so far, so
        # the older-page cursor rarely costs a block-header read.
        self._block_ts: dict[tuple[int, int], int] = {}
        self._lock = threading.Lock()

    def supports(self, chain: Chain) -> bool:
        return chain.family == TRON and chain.chain_id in TRONGRID_INSTANCES

    def _block_timestamp(self, client: TronClient, number: int) -> int:
        key = (client.chain.chain_id, number)
        with self._lock:
            ts = self._block_ts.get(key)
        if ts is not None:
            return ts
        resp = client._post("/wallet/getblock",
                            {"id_or_num": str(number), "detail": False})
        ts = int(((resp.get("block_header") or {}).get("raw_data") or {})
                 .get("timestamp") or 0)
        if not ts:
            raise TransactionSourceError(f"block {number} has no timestamp")
        return ts

    def list_transactions(
        self, chain: Chain, address: str, page: int = 1, limit: int = 50,
        before_block: int | None = None,
    ) -> list[Transaction]:
        client = TronClient(chain, timeout=self.timeout)
        params: dict[str, Any] = {
            "only_from": "true",
            "limit": min(limit, _MAX_LIMIT),
            "order_by": "block_timestamp,desc",
        }
        try:
            if before_block is not None:
                # Inclusive, like the explorers' ``endblock`` — the caller
                # dedupes the boundary block's rows by hash.
                params["max_timestamp"] = self._block_timestamp(client, before_block)
            path = f"/v1/accounts/{tron_from_hex(address)}/transactions"
            resp = client.index_get(path, params)
            for _ in range(page - 1):           # page N: follow N-1 cursors
                fingerprint = (resp.get("meta") or {}).get("fingerprint")
                if not fingerprint:
                    return []
                resp = client.index_get(path, {**params, "fingerprint": fingerprint})
        except TronError as e:
            raise TransactionSourceError(str(e)) from e
        out: list[Transaction] = []
        for row in resp.get("data") or []:
            tx = parse_tron_tx(row, chain.chain_id)
            if tx is None:
                continue
            out.append(tx)
            if tx.block_number and row.get("block_timestamp"):
                with self._lock:
                    self._block_ts[(chain.chain_id, tx.block_number)] = int(
                        row["block_timestamp"])
        return out
