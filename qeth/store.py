import json
import os
import threading
from dataclasses import fields
from pathlib import Path

from .chains import Chain, DEFAULT_CHAINS
from .fsatomic import atomic_write_text


_CHAIN_FIELDS = {f.name for f in fields(Chain)}


# For chains we ship in DEFAULT_CHAINS, the user can only edit the
# RPC URL (via the chain-RPC dialog). Everything else — name,
# symbol, explorer, coingecko_id, eip1559 — is canonical metadata
# we maintain. Older configs sometimes carry a stale or even wrong
# value here (chain 56 was manually added before BNB shipped, so
# it had coingecko_id="ethereum" → BNB native got priced as ETH).
# Defaults' metadata wins for shipped chains; persisted only
# contributes ``rpc_url``.
_USER_EDITABLE_FIELDS = {"rpc_url"}


def _merge_chain(persisted: dict) -> Chain:
    """Build a Chain from a persisted dict.

    Shipped defaults (DEFAULT_CHAINS): take canonical metadata from
    the default, overlay only the user-editable fields (rpc_url)
    from the persisted entry. Old configs that carried a wrong
    coingecko_id / symbol from a manual add silently heal.

    Custom chains (not in DEFAULT_CHAINS): use persisted as-is.
    Dropping unknown keys keeps forwards compatibility with
    configs from a newer build."""
    cid = persisted.get("chain_id")
    default = next((d for d in DEFAULT_CHAINS if d.chain_id == cid), None)
    if default is None:
        return Chain(**{k: v for k, v in persisted.items() if k in _CHAIN_FIELDS})
    base = default.to_dict()
    for f in _USER_EDITABLE_FIELDS:
        if persisted.get(f):
            base[f] = persisted[f]
    # A user who points a shipped chain at their own node wants THAT node —
    # not our public DRPC/publicnode fallbacks silently leaking their address
    # set onto a public endpoint when a single read hiccups. So when the
    # rpc_url is overridden to something other than our default, drop the
    # inherited public read-fallbacks and ws endpoints: reads then use only
    # the user's node (failover collapses to a plain provider), and the live
    # watcher derives ws from it (or falls back to polling that same node).
    if base.get("rpc_url") and base["rpc_url"] != default.rpc_url:
        base["fallback_rpcs"] = ()
        base["ws_url"] = ()
    return Chain(**{k: v for k, v in base.items() if k in _CHAIN_FIELDS})

CONFIG_DIR = Path.home() / ".qeth"
CONFIG_FILE = CONFIG_DIR / "config.json"


def ensure_private_root() -> None:
    """Create ``~/.qeth`` owner-only (0700). Every qeth cache lives under it,
    so a private root keeps another local user from traversing into any of
    them — wallet addresses, chain ids, contract metadata leak through dir
    names and cache paths otherwise — regardless of each subdir's own mode
    (a 0700 root denies the `x` traversal the whole subtree needs). Idempotent
    and best-effort: tightens a root left at a looser umask by an older build,
    and a chmod failure (exotic FS) still leaves the dir created."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


class Store:
    """Thread-safe persistent state. UI and RPC server both touch it."""

    def __init__(self):
        self._lock = threading.RLock()
        # Save ordering: _save_seq is bumped with each snapshot under _lock;
        # _io_lock serializes the file write and _last_written_seq drops a
        # stale one — two threads (the GUI and the aiohttp RPC thread's
        # add_chain) can snapshot in one order and reach disk in the other,
        # which would regress the file to older state.
        self._io_lock = threading.Lock()
        self._save_seq = 0
        self._last_written_seq = 0
        self.accounts: list[dict] = []  # {address, path, source, scheme, label}
        self.chains: list[Chain] = list(DEFAULT_CHAINS)
        self.current_chain_id: int = 1
        self.default_account: str | None = None
        # User overrides for the token panel: (chain_id, addr_lower) tuples.
        # `hidden` always wins over `shown` when both contain the same key.
        self.hidden_tokens: set[tuple[int, str]] = set()
        self.shown_tokens: set[tuple[int, str]] = set()
        # Custom-added tokens (by contract): always balance-checked, but shown
        # only when the balance is non-zero — unlike `shown_tokens` (pin),
        # which force display even at zero. (chain_id, addr_lower).
        self.custom_tokens: set[tuple[int, str]] = set()
        # Custom-pinned ENS names (lower-case) for the ENS plugin — shown in
        # addition to whatever the indexer discovers. ENS is mainnet-only.
        self.custom_ens_names: set[str] = set()
        # Hex-encoded QByteArray from QMainWindow.saveGeometry(), so size +
        # position + maximized state all round-trip.
        self.window_geometry: str | None = None
        # Same encoding for QSplitter.saveState() — preserves the user's
        # drag positions for the outer horizontal split (tree+details vs
        # token panel) and the inner vertical split (tree vs details).
        self.splitter_state_main: str | None = None
        self.splitter_state_left: str | None = None
        # Per-panel header state (hex of QHeaderView.saveState()), keyed
        # by an opaque panel name. Lets users drag columns to widths
        # they prefer and have those persist across runs.
        self.header_states: dict[str, str] = {}
        # Etherscan v2 API key — global (one key covers every chain
        # the unified v2 API supports). When set, the tokens /
        # transactions plugins prefer Etherscan over Blockscout for
        # discovery; empty means "Blockscout only".
        self.etherscan_api_key: str | None = None
        # Desktop notifications for sent/received ETH + tokens (tray
        # showMessage). On by default; toggled from the tray menu.
        self.notifications_enabled: bool = True

    @classmethod
    def load(cls) -> "Store":
        s = cls()
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
            except json.JSONDecodeError:
                return s
            s.accounts = data.get("accounts", [])
            s.current_chain_id = data.get("current_chain_id", 1)
            s.default_account = data.get("default_account")
            chains_data = data.get("chains")
            if chains_data:
                s.chains = [_merge_chain(c) for c in chains_data]
                # Forward-fill: when DEFAULT_CHAINS gains a new
                # entry (e.g. BNB Smart Chain shipped after the
                # user's last save), append it so existing
                # installs pick it up without a manual add-chain
                # dance. qeth has no remove-chain UI, so anything
                # missing here was added to the defaults after
                # this config was last written.
                known = {c.chain_id for c in s.chains}
                for d in DEFAULT_CHAINS:
                    if d.chain_id not in known:
                        s.chains.append(d)
            s.hidden_tokens = {
                (int(t["chain_id"]), str(t["address"]).lower())
                for t in data.get("hidden_tokens", [])
                if t.get("address") and t.get("chain_id") is not None
            }
            s.shown_tokens = {
                (int(t["chain_id"]), str(t["address"]).lower())
                for t in data.get("shown_tokens", [])
                if t.get("address") and t.get("chain_id") is not None
            }
            s.custom_tokens = {
                (int(t["chain_id"]), str(t["address"]).lower())
                for t in data.get("custom_tokens", [])
                if t.get("address") and t.get("chain_id") is not None
            }
            s.window_geometry = data.get("window_geometry")
            s.splitter_state_main = data.get("splitter_state_main")
            s.splitter_state_left = data.get("splitter_state_left")
            raw_headers = data.get("header_states") or {}
            if isinstance(raw_headers, dict):
                s.header_states = {
                    str(k): str(v) for k, v in raw_headers.items()
                    if isinstance(v, str)
                }
            etherscan_key = data.get("etherscan_api_key")
            if isinstance(etherscan_key, str) and etherscan_key.strip():
                s.etherscan_api_key = etherscan_key.strip()
            if "notifications_enabled" in data:
                s.notifications_enabled = bool(data["notifications_enabled"])
            s.custom_ens_names = {
                str(n).lower() for n in data.get("custom_ens_names", [])
                if isinstance(n, str) and n
            }
        return s

    def save(self) -> None:
        with self._lock:
            self._save_seq += 1
            seq = self._save_seq
            data = {
                # Copy each account dict: json.dumps runs after the lock is
                # released, and a concurrent set_label inserting a "label" key
                # into a live dict would raise "dictionary changed size during
                # iteration" mid-serialize (everything else here is already a
                # fresh list/dict).
                "accounts": [dict(a) for a in self.accounts],
                "chains": [c.to_dict() for c in self.chains],
                "current_chain_id": self.current_chain_id,
                "default_account": self.default_account,
                "hidden_tokens": [
                    {"chain_id": cid, "address": addr}
                    for (cid, addr) in sorted(self.hidden_tokens)
                ],
                "shown_tokens": [
                    {"chain_id": cid, "address": addr}
                    for (cid, addr) in sorted(self.shown_tokens)
                ],
                "custom_tokens": [
                    {"chain_id": cid, "address": addr}
                    for (cid, addr) in sorted(self.custom_tokens)
                ],
                "window_geometry": self.window_geometry,
                "splitter_state_main": self.splitter_state_main,
                "splitter_state_left": self.splitter_state_left,
                "header_states": dict(self.header_states),
                "etherscan_api_key": self.etherscan_api_key,
                "notifications_enabled": self.notifications_enabled,
                "custom_ens_names": sorted(self.custom_ens_names),
            }
        ensure_private_root()
        payload = json.dumps(data, indent=2)
        # Serialize the write and drop a stale snapshot: `seq` was taken with
        # the snapshot under _lock, so a lower seq reaching disk after a higher
        # one is an out-of-order write that would regress the file (the higher
        # snapshot is strictly newer — it saw every mutation the lower one did).
        with self._io_lock:
            if seq < self._last_written_seq:
                return
            self._last_written_seq = seq
            # Atomic: a crash mid-write must not torch the accounts list.
            atomic_write_text(CONFIG_FILE, payload)

    def add_account(self, account: dict) -> bool:
        with self._lock:
            addr = account["address"].lower()
            path = account.get("path", "")
            for a in self.accounts:
                if a["address"].lower() == addr and a.get("path", "") == path:
                    return False
            self.accounts.append(account)
            if self.default_account is None:
                self.default_account = account["address"]
        self.save()
        return True

    def remove_account(self, address: str) -> bool:
        with self._lock:
            addr = address.lower()
            before = len(self.accounts)
            self.accounts = [a for a in self.accounts if a["address"].lower() != addr]
            if len(self.accounts) == before:
                return False
            if self.default_account and self.default_account.lower() == addr:
                self.default_account = self.accounts[0]["address"] if self.accounts else None
        self.save()
        return True

    def current_chain(self) -> Chain:
        with self._lock:
            for c in self.chains:
                if c.chain_id == self.current_chain_id:
                    return c
            return self.chains[0]

    def set_current_chain(self, chain_id: int, *, persist: bool = True) -> None:
        with self._lock:
            self.current_chain_id = chain_id
        if persist:
            self.save()

    def reorder_accounts(self, ordered_keys: list[tuple[str, str]]) -> None:
        """Rewrite ``self.accounts`` so its order matches the given
        ``(address, path)`` keys (address case-insensitive). Keyed on the
        (address, path) PAIR — the record's unique identity — so the same
        address held in two places (e.g. Ledger + Air-gapped, or watch-only)
        reorders independently instead of both records jumping together.
        Records not referenced keep their existing relative position at the
        end — so a partial reorder (one scheme group) leaves the rest alone.
        Persists on disk."""
        with self._lock:
            remaining = list(self.accounts)
            new_list: list[dict] = []
            for address, path in ordered_keys:
                al = address.lower()
                for i, a in enumerate(remaining):
                    if a["address"].lower() == al and a.get("path", "") == path:
                        new_list.append(remaining.pop(i))
                        break
            new_list.extend(remaining)   # unreferenced, in original order
            self.accounts = new_list
        self.save()

    def set_label(self, address: str, label: str) -> bool:
        """Update the human-readable label on EVERY account holding
        ``address`` (case-insensitive) — a label names the address, so all its
        rows (the same address held via Ledger + Air-gapped, watch-only, …)
        show it, not just the first. Returns True if any record changed.
        Persists on disk on success."""
        addr = address.lower()
        changed = False
        with self._lock:
            for a in self.accounts:
                if a["address"].lower() == addr and a.get("label") != label:
                    a["label"] = label
                    changed = True
        if changed:
            self.save()
        return changed

    def set_default_account(self, address: str) -> None:
        with self._lock:
            self.default_account = address
        self.save()

    def add_chain(self, chain: Chain) -> None:
        with self._lock:
            if any(c.chain_id == chain.chain_id for c in self.chains):
                return
            self.chains.append(chain)
        self.save()

    def set_etherscan_api_key(self, key: str | None) -> bool:
        """Persist the (global) Etherscan v2 API key. Empty string
        and None both clear the override. Returns True when the
        stored value actually changed."""
        cleaned = (key or "").strip() or None
        with self._lock:
            if self.etherscan_api_key == cleaned:
                return False
            self.etherscan_api_key = cleaned
        self.save()
        return True

    def set_chain_rpc_url(self, chain_id: int, rpc_url: str) -> bool:
        """Override the RPC URL for an existing chain. Returns
        True if the chain was found and updated, False otherwise.
        Used by the chain-RPC dialog so the user can swap a rate-
        limited endpoint without re-adding the whole chain."""
        with self._lock:
            for c in self.chains:
                if c.chain_id == int(chain_id):
                    c.rpc_url = rpc_url
                    break
            else:
                return False
        self.save()
        return True

    # --- token-level user overrides -----------------------------------------

    def is_hidden(self, chain_id: int, address: str) -> bool:
        return (int(chain_id), address.lower()) in self.hidden_tokens

    def is_force_shown(self, chain_id: int, address: str) -> bool:
        return (int(chain_id), address.lower()) in self.shown_tokens

    def is_custom_token(self, chain_id: int, address: str) -> bool:
        return (int(chain_id), address.lower()) in self.custom_tokens

    def add_custom_token(self, chain_id: int, address: str) -> None:
        with self._lock:
            key = (int(chain_id), address.lower())
            self.custom_tokens.add(key)
            self.hidden_tokens.discard(key)
        self.save()

    def remove_custom_token(self, chain_id: int, address: str) -> None:
        with self._lock:
            self.custom_tokens.discard((int(chain_id), address.lower()))
        self.save()

    def add_custom_ens_name(self, name: str) -> None:
        with self._lock:
            self.custom_ens_names.add(name.strip().lower())
        self.save()

    def remove_custom_ens_name(self, name: str) -> None:
        with self._lock:
            self.custom_ens_names.discard(name.strip().lower())
        self.save()

    def hide_token(self, chain_id: int, address: str) -> None:
        with self._lock:
            key = (int(chain_id), address.lower())
            self.hidden_tokens.add(key)
            self.shown_tokens.discard(key)
            # Hiding a custom token also stops tracking it (no point checking
            # the balance of something the user explicitly hid).
            self.custom_tokens.discard(key)
        self.save()

    def unhide_token(self, chain_id: int, address: str) -> None:
        with self._lock:
            self.hidden_tokens.discard((int(chain_id), address.lower()))
        self.save()

    def force_show_token(self, chain_id: int, address: str) -> None:
        with self._lock:
            key = (int(chain_id), address.lower())
            self.shown_tokens.add(key)
            self.hidden_tokens.discard(key)
        self.save()

    def unforce_show_token(self, chain_id: int, address: str) -> None:
        with self._lock:
            self.shown_tokens.discard((int(chain_id), address.lower()))
        self.save()

    def set_window_geometry(self, geometry_hex: str) -> None:
        with self._lock:
            self.window_geometry = geometry_hex
        self.save()

    def set_splitter_states(self, main_hex: str, left_hex: str) -> None:
        with self._lock:
            self.splitter_state_main = main_hex
            self.splitter_state_left = left_hex
        self.save()

    def get_header_state(self, name: str) -> str | None:
        return self.header_states.get(name)

    def set_header_state(self, name: str, state_hex: str) -> None:
        """Persist the hex-encoded QHeaderView.saveState() for a panel.
        Empty/falsy state is treated as "forget" so a panel that has
        nothing useful to save doesn't leave a stale entry behind."""
        with self._lock:
            if state_hex:
                self.header_states[name] = state_hex
            else:
                self.header_states.pop(name, None)
        self.save()
