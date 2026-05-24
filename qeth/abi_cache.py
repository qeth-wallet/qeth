"""Disk-backed cache for contract ABIs, keyed by (chain, address).

Verified contracts on Blockscout publish their ABI; we cache it so
the Transactions panel's details dialog can decode calldata without
hitting the network on every open. Unverified contracts get a
sentinel entry so we don't refetch hopelessly.

Layout mirrors qeth.transactions_cache:
    CACHE_DIR / <chain_id> / <address_lower>.json
File content is either the ABI (a JSON list of fragments) or
``{"unverified": true}`` for contracts Blockscout knows about but
has no source for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Union

# An ABI is a JSON list of fragment dicts. We don't tighten the type
# further here — web3.py validates shape when we try to decode.
Abi = list[dict]


CACHE_DIR = Path.home() / ".qeth" / "abi"

# Functions you typically find on a TransparentUpgradeableProxy /
# EIP-1967 proxy wrapper. Their presence isn't conclusive (some
# real contracts expose admin functions too), but an ABI that has
# ONLY these and nothing else is almost certainly a proxy stub whose
# implementation hasn't been merged in — worth refetching with the
# proxy-resolving path.
_PROXY_FUNCTION_MARKERS = frozenset({
    "implementation", "admin", "upgradeTo", "upgradeToAndCall",
    "changeAdmin", "_setImplementation", "_setAdmin",
})


def _is_proxy_stub_abi(abi) -> bool:
    """True when ``abi`` looks like a proxy's own admin surface with
    no implementation methods merged in. Lets the cache decide to
    refetch entries written before proxy resolution landed — without
    a hard version bump that would also discard fully-resolved
    non-proxy ABIs."""
    if not isinstance(abi, list):
        return False
    fn_total = 0
    fn_non_proxy = 0
    for entry in abi:
        if entry.get("type") != "function":
            continue
        fn_total += 1
        if entry.get("name") not in _PROXY_FUNCTION_MARKERS:
            fn_non_proxy += 1
    return fn_total > 0 and fn_non_proxy == 0


class AbiCache:
    """File-backed key/value store for ABIs.

    ``load()`` returns one of three things to keep the call sites
    explicit about cache state:
      - an Abi list:           contract is verified, here's its ABI
      - the sentinel ``False``: contract is known to be unverified
      - ``None``:               not in cache yet, caller should fetch
    """

    def __init__(self, root: Optional[Path] = None):
        # Look up CACHE_DIR at instantiation, so tests that
        # monkey-patch the module constant see the redirect.
        self.root = root if root is not None else CACHE_DIR

    def _path(self, chain_id: int, address: str) -> Path:
        return self.root / str(chain_id) / f"{address.lower()}.json"

    def load(self, chain_id: int, address: str) -> Union[Abi, bool, None]:
        p = self._path(chain_id, address)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        # Two on-disk shapes supported transparently:
        #   - dict with "unverified": True → negative sentinel
        #   - dict with "abi": [...] (new) or bare list (legacy) → ABI
        if isinstance(data, dict):
            if data.get("unverified"):
                return False
            abi = data.get("abi") if isinstance(data.get("abi"), list) else None
        elif isinstance(data, list):
            abi = data
        else:
            return None
        if abi is None:
            return None
        # If the cached ABI looks like a proxy stub (only admin/upgrade
        # functions, no real surface), pretend we have nothing — the
        # next access refetches via the proxy-resolving path. Merged
        # results have plenty of non-proxy methods and are trusted.
        # Same heuristic applies whether the file is legacy or new
        # format, so re-fetches happen even if a proxy's impl gets
        # verified after we first cached the stub.
        if _is_proxy_stub_abi(abi):
            return None
        return abi

    def save(self, chain_id: int, address: str,
             abi: Union[Abi, bool]) -> None:
        """Persist either the ABI (a list) or the negative sentinel
        (``False`` — meaning Blockscout has no source for this
        address, so future calls can skip the fetch)."""
        p = self._path(chain_id, address)
        p.parent.mkdir(parents=True, exist_ok=True)
        if abi is False:
            payload: object = {"unverified": True}
        elif isinstance(abi, list):
            payload = {"abi": abi}
        else:
            return  # nothing useful to save
        p.write_text(json.dumps(payload, separators=(",", ":")))
