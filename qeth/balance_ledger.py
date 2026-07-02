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

from .wallet_cache import CachedToken, CachedWallet, WalletCache


class BalanceLedger:
    """Owns the per-key freshness stamps and the cache mutation that honours
    them. Constructed with the plugin's shared ``WalletCache`` / token-metadata
    sources and its ``unpriced_since`` grace map (a (re-)received token restarts
    its grace window, so the ledger resets it on add — shared with the display
    filter that reads it)."""

    def __init__(self, cache_getter: Callable[[], WalletCache],
                 token_lists, token_metadata,
                 unpriced_since: dict[tuple[int, str], float]) -> None:
        # A getter, not the cache itself: the plugin (and its tests) may swap
        # its WalletCache instance after construction, and the ledger must see
        # the current one.
        self._get_cache = cache_getter
        self._token_lists = token_lists
        self._token_metadata = token_metadata
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
            entry = self._token_lists.get(chain.chain_id, contract_lower)
            meta = self._token_metadata.get(chain.chain_id, contract_lower)
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
