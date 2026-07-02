"""Block-ordered balance writer — the single owner of balance freshness.

Every path that writes an on-chain balance into the wallet cache — the targeted
ws re-read, the confirm-driven reconcile, discovery's merge, the receipt credit,
the ws native poll — goes through this one component, so the invariant

    per (chain, account, asset), a read at an older block must never regress a
    fresher one; an authoritative zero at a fresh-enough block drops the row

is enforced in ONE place instead of being re-derived (and, for native + the
receipt credit, occasionally skipped) per call site. That fragmentation is what
let a stale discovery resurrect a just-sent token and an out-of-order native
read regress the balance (see docs/race-audit.md, Priority 2).

Main-thread only: every caller is a Qt slot on the GUI thread, so the ordering
maps and the load-modify-save cycle need no locking.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from .wallet_cache import CachedToken, CachedWallet, WalletCache

if TYPE_CHECKING:
    from .token_metadata import TokenMetadataCache
    from .tokenlists import TokenLists


class BalanceLedger:
    """Owns the per-key freshness stamps and the cache mutation that honours
    them. Constructed with the plugin's shared ``WalletCache`` / token-metadata
    sources and its ``unpriced_since`` grace map (a (re-)received token restarts
    its grace window, so the ledger resets it on add — shared with the display
    filter that reads it)."""

    def __init__(self, cache_getter: Callable[[], WalletCache],
                 token_lists_getter: Callable[[], TokenLists],
                 token_metadata_getter: Callable[[], TokenMetadataCache],
                 unpriced_since: dict[tuple[int, str], float]) -> None:
        # Getters, not the objects: the plugin (and its tests) may swap its
        # WalletCache / token-list / metadata instances after construction, and
        # the ledger must see the current ones. (``unpriced_since`` is mutated
        # in place, never reassigned, so it's shared directly.)
        self._get_cache = cache_getter
        self._get_lists = token_lists_getter
        self._get_metadata = token_metadata_getter
        self._unpriced_since = unpriced_since
        # (chain_id, account_lower, token_lower) -> block of the last
        # AUTHORITATIVE balance recorded for that token. A read/discovery that
        # saw the token at an OLDER block must not drop or regress it — this is
        # the per-token ordering that stops a stale read from dropping a
        # freshly-claimed token (a per-account stamp can't: a token can arrive
        # at a block newer than the account's last read).
        self.balance_block: dict[tuple[int, str, str], int] = {}
        # (chain_id, account_lower) -> highest block whose NATIVE read we've
        # applied. Native is ordered per account (a stale read can't regress
        # it); tokens are ordered individually above.
        self.native_block: dict[tuple[int, str], int] = {}

    def reset_chain(self, chain_id: int) -> None:
        """Drop every freshness floor for ``chain_id``. Called on a ws
        (re)connect: while the socket was down we were blind to new heads /
        logs, and a reorg may have rewound the chain below a stamp. Clearing
        the floors lets the fresh reads that follow re-establish truth, rather
        than being ordered out by a stamp from before the gap. Monotonic floors
        otherwise can't over-stamp (min-block reads never exceed the true head),
        so a live reorg self-heals once the head re-passes — this only covers
        the blind-gap case."""
        self.balance_block = {k: v for k, v in self.balance_block.items()
                              if k[0] != chain_id}
        self.native_block = {k: v for k, v in self.native_block.items()
                             if k[0] != chain_id}

    # --- per-token ordering primitives (shared by the discovery merge) ------

    def is_token_stale(self, chain_id: int, account: str, token: str,
                       block) -> bool:
        """True if ``block`` is older than the last block recorded for this
        token — i.e. this read must be ignored for it. A block-less read
        (``None``) is never considered stale (it carries no ordering)."""
        if block is None:
            return False
        bkey = (chain_id, account.lower(), token.lower())
        return int(block) < self.balance_block.get(bkey, 0)

    def stamp_token(self, chain_id: int, account: str, token: str,
                    block) -> None:
        """Record ``block`` as the freshest we've applied for this token."""
        if block is None:
            return
        bkey = (chain_id, account.lower(), token.lower())
        self.balance_block[bkey] = int(block)

    def stamp_native(self, chain_id: int, account: str, block) -> None:
        """Record ``block`` as the freshest native read applied for the account
        (for a caller that writes native through its own path, e.g. discovery's
        cache rebuild, but must still order against later polls)."""
        if block is None:
            return
        self.native_block[(chain_id, account.lower())] = int(block)

    def native_stale(self, chain_id: int, account: str, block) -> bool:
        """True if ``block`` is older than the last native read applied for the
        account — a stale ws poll (an LB jumped backwards) that must not
        regress the balance or re-trigger a 'received' notification."""
        if block is None:
            return False
        return int(block) < self.native_block.get((chain_id, account.lower()), 0)

    def apply_native(self, chain, account: str, native_wei, block) -> bool:
        """Ordered native-ONLY cache write (the ws-poll counterpart to
        apply_read): apply iff ``block`` isn't stale, stamping the native
        block so a later out-of-order poll can't regress it. Leaves an absent
        cache untouched (nothing to update in place). Returns whether the
        cached native changed."""
        nkey = (chain.chain_id, account.lower())
        if block is not None and int(block) < self.native_block.get(nkey, 0):
            return False
        cache = self._get_cache()
        cached = cache.load(chain.chain_id, account)
        if cached is None:
            return False
        if block is not None:
            self.native_block[nkey] = int(block)
        if cached.native_balance_wei == int(native_wei):
            return False
        cached.native_balance_wei = int(native_wei)
        cached.native_balance_updated = int(time.time())
        cache.save(cached)
        return True

    def note_nonzero(self, chain_id: int, account: str, contract: str,
                     block) -> None:
        """Mark a token as seen NON-ZERO at ``block`` so a later stale read
        (older block) can't drop it. Used by the receipt path, which knows a
        received token is non-zero as of the receipt's block without a read."""
        if block is None:
            return
        bkey = (chain_id, account.lower(), contract.lower())
        self.balance_block[bkey] = max(self.balance_block.get(bkey, 0),
                                       int(block))

    # --- the receipt-side credit (delta, idempotent) ------------------------

    def apply_floor(self, chain, account: str, contract: str, block,
                    delta: int) -> None:
        """Receipt-side credit: the account received ``delta`` raw units of
        ``contract`` as of ``block`` (from a confirmed tx's Transfer log — no
        balanceOf read). Block-ordered and idempotent: if an authoritative read
        at or after ``block`` has already been applied for this token, the
        received amount is already in the cached balance — do nothing. That is
        what stops the credit from double-counting on top of the ws absolute
        read at the same block (which lands in either order), and any duplicate
        confirm delivery. Otherwise add ``delta`` (adding the token if absent
        and we have its metadata) and stamp it at ``block`` so a later stale
        read can't drop it.

        Callers must sum a receipt's per-token deltas and call this ONCE per
        token — two Transfer logs of the same token in one tx would otherwise
        have the second skipped by the stamp the first leaves."""
        contract_lower = contract.lower()
        bkey = (chain.chain_id, account.lower(), contract_lower)
        if block is not None and self.balance_block.get(bkey, 0) >= int(block):
            return   # a read at/after this block already reflects the receive
        if int(delta) <= 0:
            return
        now = int(time.time())
        cache = self._get_cache()
        cached = cache.load(chain.chain_id, account)
        if cached is None:
            cached = CachedWallet(
                chain_id=chain.chain_id, address=account.lower(),
                native_balance_wei=0, native_balance_updated=0)
        existing = next(
            (t for t in cached.tokens if t.contract.lower() == contract_lower),
            None)
        if existing is not None:
            existing.balance_raw = max(0, int(existing.balance_raw) + int(delta))
            existing.balance_updated = now
        else:
            # Add it — need symbol/name/decimals to render. Prefer the on-chain
            # metadata cache, fall back to the curated list entry (both carry
            # them); a genuinely unknown token is skipped (the next discovery
            # picks it up with proper metadata).
            entry = self._get_lists().get(chain.chain_id, contract_lower)
            meta = self._get_metadata().get(chain.chain_id, contract_lower)
            if meta is not None:
                symbol, name, decimals = (
                    meta["symbol"], meta["name"], meta["decimals"])
            elif entry is not None:
                symbol, name, decimals = (
                    entry.symbol, entry.name, entry.decimals)
            else:
                return
            self._unpriced_since.pop((chain.chain_id, contract_lower), None)
            cached.tokens.append(CachedToken(
                contract=contract_lower, symbol=symbol, name=name,
                decimals=decimals, logo_uri=entry.logo_uri if entry else None,
                balance_raw=int(delta), price_usd=None,
                balance_updated=now, price_updated=0))
        cache.save(cached)
        self.note_nonzero(chain.chain_id, account, contract_lower, block)

    # --- the ordered absolute write -----------------------------------------

    def apply_read(self, chain, account: str, native_wei, balances_raw: dict,
                   block=None) -> None:
        """Write the authoritative native + per-token balances into the wallet
        cache (absolute values, unlike the receipt path's delta). Each token is
        PER-TOKEN block-ordered via :attr:`balance_block`: a read at an older
        block than the one last recorded for that token is ignored, so a stale
        read can't drop or regress a freshly-claimed token. A token read ZERO at
        a fresh-enough block is dropped; an uncached token is added only when we
        have metadata AND a non-zero balance."""
        now = int(time.time())
        cache = self._get_cache()
        cached = cache.load(chain.chain_id, account)
        nkey = (chain.chain_id, account.lower())
        native_stale = (block is not None
                        and block < self.native_block.get(nkey, 0))
        if cached is None:
            cached = CachedWallet(
                chain_id=chain.chain_id, address=account.lower(),
                native_balance_wei=int(native_wei), native_balance_updated=now)
        elif not native_stale:
            # Native is block-ordered per account (a stale read can't regress
            # it); tokens are ordered individually below.
            cached.native_balance_wei = int(native_wei)
            cached.native_balance_updated = now
        if block is not None and not native_stale:
            self.native_block[nkey] = int(block)
        existing = {t.contract.lower(): t for t in cached.tokens}
        emptied: set[str] = set()
        for contract_lower, raw in balances_raw.items():
            bkey = (chain.chain_id, account.lower(), contract_lower)
            if block is not None and block < self.balance_block.get(bkey, 0):
                continue   # stale read for this token — ignore
            if block is not None:
                self.balance_block[bkey] = int(block)
            tok = existing.get(contract_lower)
            if tok is not None:
                if int(raw) <= 0:
                    emptied.add(contract_lower)   # fully spent → drop the row
                else:
                    tok.balance_raw = int(raw)
                    tok.balance_updated = now
                continue
            if int(raw) <= 0:
                continue
            entry = self._get_lists().get(chain.chain_id, contract_lower)
            meta = self._get_metadata().get(chain.chain_id, contract_lower)
            if meta is not None:
                symbol, name, decimals = (
                    meta["symbol"], meta["name"], meta["decimals"])
            elif entry is not None:
                symbol, name, decimals = entry.symbol, entry.name, entry.decimals
            else:
                continue
            # Freshly (re-)received → restart its unpriced grace window, so a
            # token that was hidden after a past window shows again while its
            # price gets another chance to load.
            self._unpriced_since.pop((chain.chain_id, contract_lower), None)
            cached.tokens.append(CachedToken(
                contract=contract_lower, symbol=symbol, name=name,
                decimals=decimals, logo_uri=entry.logo_uri if entry else None,
                balance_raw=int(raw), price_usd=None,
                balance_updated=now, price_updated=0))
        if emptied:
            cached.tokens = [
                t for t in cached.tokens if t.contract.lower() not in emptied]
        cache.save(cached)
