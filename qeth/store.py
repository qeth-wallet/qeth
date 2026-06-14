import json
import threading
from dataclasses import fields
from pathlib import Path
from typing import Optional

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
    return Chain(**{k: v for k, v in base.items() if k in _CHAIN_FIELDS})

CONFIG_DIR = Path.home() / ".qeth"
CONFIG_FILE = CONFIG_DIR / "config.json"


class Store:
    """Thread-safe persistent state. UI and RPC server both touch it."""

    def __init__(self):
        self._lock = threading.RLock()
        self.accounts: list[dict] = []  # {address, path, source, scheme, label}
        self.chains: list[Chain] = list(DEFAULT_CHAINS)
        self.current_chain_id: int = 1
        self.default_account: Optional[str] = None
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
        self.window_geometry: Optional[str] = None
        # Same encoding for QSplitter.saveState() — preserves the user's
        # drag positions for the outer horizontal split (tree+details vs
        # token panel) and the inner vertical split (tree vs details).
        self.splitter_state_main: Optional[str] = None
        self.splitter_state_left: Optional[str] = None
        # Per-panel header state (hex of QHeaderView.saveState()), keyed
        # by an opaque panel name. Lets users drag columns to widths
        # they prefer and have those persist across runs.
        self.header_states: dict[str, str] = {}
        # Etherscan v2 API key — global (one key covers every chain
        # the unified v2 API supports). When set, the tokens /
        # transactions plugins prefer Etherscan over Blockscout for
        # discovery; empty means "Blockscout only".
        self.etherscan_api_key: Optional[str] = None
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
            data = {
                "accounts": self.accounts,
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
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Atomic: a crash mid-write must not torch the accounts list.
        atomic_write_text(CONFIG_FILE, json.dumps(data, indent=2))

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

    def reorder_accounts(self, ordered_addresses: list[str]) -> None:
        """Rewrite ``self.accounts`` so its order matches the given
        list of addresses (case-insensitive). Addresses not present
        in the new order keep their existing relative position at
        the end — so a partial reorder (e.g. only one scheme group)
        leaves the rest alone. Persists on disk."""
        with self._lock:
            wanted = [a.lower() for a in ordered_addresses]
            by_addr: dict[str, list[dict]] = {}
            for a in self.accounts:
                by_addr.setdefault(a["address"].lower(), []).append(a)
            new_list: list[dict] = []
            seen: set[str] = set()
            for addr in wanted:
                if addr in by_addr and addr not in seen:
                    new_list.extend(by_addr[addr])
                    seen.add(addr)
            # Append any unreferenced accounts in their original order.
            for a in self.accounts:
                if a["address"].lower() not in seen:
                    new_list.append(a)
            self.accounts = new_list
        self.save()

    def set_label(self, address: str, label: str) -> bool:
        """Update the human-readable label on the account whose
        address matches ``address`` (case-insensitive). Returns
        True if an account was found and modified, False otherwise.
        Persists on disk on success."""
        addr = address.lower()
        changed = False
        with self._lock:
            for a in self.accounts:
                if a["address"].lower() == addr:
                    if a.get("label") != label:
                        a["label"] = label
                        changed = True
                    break
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

    def set_etherscan_api_key(self, key: Optional[str]) -> bool:
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

    def get_header_state(self, name: str) -> Optional[str]:
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
