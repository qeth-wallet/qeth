"""Provenance-based discovery of vault/LP holdings the curated lists miss.

A held ERC-4626 vault share (a Curve LlamaLend / Yearn / Frax lending position,
etc.) is usually NOT on any curated tokenlist and often can't be priced by a
keyless quote source — so the Tokens tab's known-token gate drops it, even
though it can be worth five figures. The robust signal that such a token is a
real holding and not spam is **provenance**: did the account ACQUIRE it via a
transaction it sent (a deposit), rather than receive it in an airdrop (the
scammer's transaction)? A token you deliberately deposited into is legitimate;
that needs no simulation.

This module is the pure core, free of Qt / network side effects at import:
- ``read_vault_assets`` — a cheap ``asset()`` multicall pre-filter that narrows
  the account's held-but-unrecognised tokens to the rare ERC-4626-shaped ones
  (so the per-token provenance lookups below stay bounded).
- ``incoming_transfer_txhashes`` / ``self_acquired_via_own_tx`` — turn explorer
  ``Transfer(*, to=owner)`` log rows into "was this acquired by one of my own
  transactions?".

The network pieces are injected (a multicall client, an explorer-log fetch, a
``tx.from`` reader), so this all unit-tests standalone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..chain import _decode_uint256

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from ..chain import EthClient

_ASSET_SELECTOR = bytes.fromhex("38d52e0f")   # asset() — the ERC-4626 underlying
_ADDR_MAX = 1 << 160


def read_vault_assets(
    client: EthClient, tokens: Iterable[str], *, batch_size: int = 200,
) -> dict[str, str]:
    """``{token_lower: asset_lower}`` for tokens whose ERC-4626 ``asset()``
    resolves to a plausible address (fits in 20 bytes, nonzero, not the token
    itself). One multicall pass; non-vaults (``asset()`` reverts or returns a
    non-address) are omitted. This is the cheap pre-filter that keeps the
    provenance lookups to the rare vault-shaped holdings."""
    toks = [t for t in tokens]
    if not toks:
        return {}
    pending: dict[str, object] = {}
    with client.multicall(batch_size=batch_size) as mc:
        for t in toks:
            pending[t.lower()] = mc.add(t, _ASSET_SELECTOR, decoder=_decode_uint256)
    out: dict[str, str] = {}
    for t, p in pending.items():
        if p.success and isinstance(p.value, int) and 0 < p.value < _ADDR_MAX:  # type: ignore[attr-defined]
            asset = "0x" + f"{p.value:040x}"                                    # type: ignore[attr-defined]
            if asset != t:
                out[t] = asset
    return out


def incoming_transfer_txhashes(rows) -> list[str]:
    """Unique tx hashes (in returned order) from explorer ``Transfer`` log rows
    (each a dict with ``transactionHash``)."""
    out: list[str] = []
    seen: set[str] = set()
    for r in rows or []:
        h = r.get("transactionHash") or r.get("transaction_hash")
        if h and h not in seen:
            seen.add(h)
            out.append(h)
    return out


def self_acquired_via_own_tx(
    tx_hashes: Iterable[str],
    tx_from: Callable[[str], str | None],
    owner_addrs: Iterable[str],
    *, max_lookups: int = 25,
) -> bool:
    """True if any of ``tx_hashes`` was ORIGINATED by one of the user's own
    addresses (``tx.from`` ∈ ``owner_addrs``) — provenance that the token was
    deliberately acquired (a deposit), not airdropped (a scammer's tx, whose
    ``tx.from`` is the scammer). ``tx_from(hash) -> addr | None`` is injected.
    Bounded by ``max_lookups`` — a single match settles it, so we stop early."""
    owners = {a.lower() for a in owner_addrs}
    checked = 0
    for h in tx_hashes:
        if checked >= max_lookups:
            break
        checked += 1
        frm = tx_from(h)
        if frm and frm.lower() in owners:
            return True
    return False
