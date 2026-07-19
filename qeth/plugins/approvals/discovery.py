"""Approvals discovery — pure, Qt-free approval-pair extraction + allowance reads.

The candidate (token, spender) pairs come straight from the account's own
``approve()`` / ``increaseAllowance()`` transactions — no cross-product, no
held-token guessing. We then read ``allowance(account, spender)`` live for each
pair and keep the nonzero ones. Cache-only source (the scan worker feeds it the
account's full history), so an approval whose tx isn't in the history — granted
pre-qeth, from another wallet, via Permit2 — won't appear (documented v1 limit).

Free of Qt / network side effects at import, so it unit-tests standalone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from eth_utils import to_checksum_address

from ...chain import _decode_uint256

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ...chain import EthClient
    from ...transactions import Transaction

_SEL_APPROVE = "0x095ea7b3"              # approve(address,uint256)
_SEL_INCREASE_ALLOWANCE = "0x39509351"   # increaseAllowance(address,uint256)
_APPROVE_SELECTORS = {_SEL_APPROVE, _SEL_INCREASE_ALLOWANCE}
_SEL_ALLOWANCE = bytes.fromhex("dd62ed3e")   # allowance(address,address)


@dataclass
class ApprovalRow:
    token: str              # lowercase contract
    spender: str            # checksummed (for display)
    allowance: int          # raw uint256
    symbol: str = ""
    name: str = ""
    decimals: int = 18
    spender_label: str = ""   # "Uniswap: Router" etc., "" when unknown


def _pad_address(addr: str) -> bytes:
    h = addr[2:] if addr.startswith("0x") else addr
    return b"\x00" * 12 + bytes.fromhex(h.lower())


def spender_of(data: str | None) -> str | None:
    """Checksummed spender from approve/increaseAllowance calldata (ABI word 1 =
    the low 20 bytes of ``data[10:74]``), or None if too short / malformed."""
    if not data or len(data) < 74:       # 0x + 8-char selector + 64-char word
        return None
    try:
        return to_checksum_address("0x" + data[34:74])
    except Exception:
        return None


def approve_pairs_in(txs: Iterable[Transaction], account: str) -> set[tuple[str, str]]:
    """(token_lower, spender_lower) for every approve/increaseAllowance the
    account itself SENT in ``txs``. Approvals are always sent by the granter, so
    the from_addr filter also excludes received rows the cache keeps."""
    acct = account.lower()
    out: set[tuple[str, str]] = set()
    for t in txs:
        if t.from_addr.lower() != acct:
            continue
        if (t.method_id or "").lower() not in _APPROVE_SELECTORS:
            continue
        token = (t.to_addr or "").lower()
        spender = spender_of(t.input_data)
        if token and spender:
            out.add((token, spender.lower()))
    return out


def fetch_allowances(
    client: EthClient, owner: str, pairs: Iterable[tuple[str, str]],
    *, batch_size: int = 100,
) -> dict[tuple[str, str], int]:
    """One multicall pass reading ``allowance(owner, spender)`` for every pair —
    always the SAME owner (the selected account). Keeps only calls that
    succeeded with a positive allowance; reverts (non-ERC-20s) drop silently."""
    owner_word = _pad_address(owner)
    pending: dict[tuple[str, str], object] = {}
    with client.multicall(batch_size=batch_size) as mc:
        for token, spender in pairs:
            calldata = _SEL_ALLOWANCE + owner_word + _pad_address(spender)
            pending[(token, spender)] = mc.add(
                token, calldata, decoder=_decode_uint256)
    out: dict[tuple[str, str], int] = {}
    for key, p in pending.items():
        if p.success and isinstance(p.value, int) and p.value > 0:  # type: ignore[attr-defined]
            out[key] = p.value                                       # type: ignore[attr-defined]
    return out
