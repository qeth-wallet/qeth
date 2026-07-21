"""Approvals discovery — pure, Qt-free approval-pair extraction + allowance reads.

Candidate (token, spender) pairs come primarily from the account's ERC-20
``Approval(owner, …)`` event logs (``approval_pairs_from_log_rows`` over the
``ApprovalLogSource`` walk) — every allowance grant emits one, so this catches
approvals set via ``permit`` / an internal router call, not just the account's
own top-level ``approve`` transactions (``approve_pairs_in``, kept as a
recent-tail patch for the window a logs indexer may lag behind head). We then
read ``allowance(account, spender)`` live for each candidate and keep the
nonzero ones — that multicall is the source of truth for the displayed cap.

Free of Qt / network side effects at import, so it unit-tests standalone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from eth_utils import to_checksum_address

from ...chain import _decode_uint256

if TYPE_CHECKING:
    from collections.abc import Iterable
    from decimal import Decimal

    from ...chain import EthClient
    from ...transactions import Transaction

_SEL_APPROVE = "0x095ea7b3"              # approve(address,uint256)
_SEL_INCREASE_ALLOWANCE = "0x39509351"   # increaseAllowance(address,uint256)
_APPROVE_SELECTORS = {_SEL_APPROVE, _SEL_INCREASE_ALLOWANCE}
_SEL_ALLOWANCE = bytes.fromhex("dd62ed3e")   # allowance(address,address)
# keccak256("Approval(address,address,uint256)") — the ERC-20 Approval event.
_APPROVAL_TOPIC0 = (
    "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
)


def approval_pairs_from_logs(logs, owner: str) -> set[tuple[str, str]]:
    """(token_lower, spender_lower) for every ERC-20 ``Approval(owner, spender,
    …)`` the ``owner`` emitted in these receipt/sim logs — i.e. allowances the
    account just changed, from ANY path (Send, a dapp, the approve dialog).
    Works on a confirmed receipt's logs and on ``eth_simulate`` logs alike (both
    Mappings carrying ``topics`` / ``data`` / ``address``)."""
    from ...tx_activity import _hexstr
    acct = owner.lower()
    out: set[tuple[str, str]] = set()
    for log in logs or []:
        if not hasattr(log, "get"):
            continue
        topics = log.get("topics") or []
        if len(topics) != 3 or _hexstr(topics[0]).lower() != _APPROVAL_TOPIC0:
            continue
        raw = log.get("address") or ""
        token = raw.lower() if isinstance(raw, str) else _hexstr(raw).lower()
        log_owner = ("0x" + _hexstr(topics[1])[-40:]).lower()
        spender = ("0x" + _hexstr(topics[2])[-40:]).lower()
        if token and token != "0x" and log_owner == acct:
            out.add((token, spender))
    return out


def approval_pairs_from_log_rows(
    rows, owner: str,
) -> tuple[set[tuple[str, str]], int]:
    """``(pairs, max_block)`` from explorer ``module=logs`` Approval rows (each a
    dict with ``topics`` = list of hex strings, ``address``, ``blockNumber`` in
    hex). Keeps only 3-topic ERC-20 ``Approval(owner, spender, value)`` logs
    whose owner (topic1) matches — a 4-topic Approval is an ERC-721 NFT approval
    (its "spender" isn't an allowance), excluded here (and ``allowance()`` would
    revert on it anyway). ``max_block`` is the highest block seen (0 if none) —
    the windowing cursor for the next ``fetch(from_block=…)``."""
    acct = owner.lower()
    pairs: set[tuple[str, str]] = set()
    max_block = 0
    for r in rows or []:
        # Advance the cursor over EVERY row the explorer returned in this
        # window — they've all been scanned, so resuming above max_block can't
        # skip anything. (An ERC-721 4-topic Approval or a topic we don't keep
        # still counts as scanned; only the pair-keep below is selective.)
        try:
            b = int(r.get("blockNumber"), 16)
        except (TypeError, ValueError):
            b = 0
        if b > max_block:
            max_block = b
        # Blockscout pads the topics array to 4 slots with trailing None for a
        # 3-topic event ([topic0, owner, spender, None]); Etherscan/node return
        # exactly the emitted topics. Drop the empties, then a real ERC-721
        # Approval (4 genuine topics incl. tokenId) still reads as len 4 and is
        # skipped — only the ERC-20 3-topic allowance is kept.
        topics = [t for t in (r.get("topics") or []) if t]
        if len(topics) != 3:
            continue
        if (topics[0] or "").lower() != _APPROVAL_TOPIC0:
            continue
        log_owner = ("0x" + topics[1][-40:]).lower()
        if log_owner != acct:
            continue
        token = (r.get("address") or "").lower()
        spender = ("0x" + topics[2][-40:]).lower()
        if token and token != "0x":
            pairs.add((token, spender))
    return pairs, max_block


@dataclass
class ApprovalRow:
    token: str              # lowercase contract
    spender: str            # checksummed (for display)
    allowance: int          # raw uint256
    symbol: str = ""
    name: str = ""
    decimals: int = 18
    spender_label: str = ""   # definitive public name-tag (OLI), "" when unknown
    # A SELF-REPORTED name shown only when there's no name-tag: the spender's own
    # ERC-20 name (it's a token) or its verified ABI contract name (proxy-resolved).
    # Rendered in italic — forgeable, so lower-trust than a name-tag. "" when none.
    spender_soft_label: str = ""
    price_usd: Decimal | None = None   # USD per whole token; None = unpriced
    # (the USD *value* of the cap is derived: allowance/10**decimals * price_usd,
    # so it stays correct after a reconcile changes `allowance` in place)
    token_balance: int = 0    # holder's raw wallet balance of `token` (0 = none/unknown)
    #   → the "at risk" USD tag on the token node = token_balance/10**decimals * price_usd


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
) -> tuple[dict[tuple[str, str], int], set[tuple[str, str]]]:
    """One multicall pass reading ``allowance(owner, spender)`` for every pair —
    always the SAME owner (the selected account).

    Returns ``(found, read)``:

    - ``found`` — ``{pair: value}`` for calls that succeeded with a POSITIVE
      allowance (the displayed caps).
    - ``read`` — every pair whose call SUCCEEDED (returned a decodable uint,
      including exactly 0). A pair in ``read`` but not ``found`` was
      definitively read as zero (revoked); a pair in NEITHER had its call
      revert/fail this pass (a non-ERC-20, or a transient glitch).

    The distinction matters for pruning: a cached cap must only be dropped when
    it was DEFINITIVELY read as zero, never on a transient read failure — else a
    momentary RPC hiccup deletes a real approval from the persisted cache."""
    owner_word = _pad_address(owner)
    pending: dict[tuple[str, str], object] = {}
    with client.multicall(batch_size=batch_size) as mc:
        for token, spender in pairs:
            calldata = _SEL_ALLOWANCE + owner_word + _pad_address(spender)
            pending[(token, spender)] = mc.add(
                token, calldata, decoder=_decode_uint256)
    found: dict[tuple[str, str], int] = {}
    read: set[tuple[str, str]] = set()
    for key, p in pending.items():
        if p.success and isinstance(p.value, int):    # type: ignore[attr-defined]
            read.add(key)
            if p.value > 0:                            # type: ignore[attr-defined]
                found[key] = p.value                   # type: ignore[attr-defined]
    return found, read
