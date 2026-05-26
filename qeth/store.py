import json
import threading
from dataclasses import fields
from pathlib import Path
from typing import Optional

from .chains import Chain, DEFAULT_CHAINS


_CHAIN_FIELDS = {f.name for f in fields(Chain)}


def _merge_chain(persisted: dict) -> Chain:
    """Build a Chain from a persisted dict, filling in any missing fields
    from DEFAULT_CHAINS when chain_id matches (so old configs pick up
    newly-added fields like coingecko_id). Persisted values win when
    both sides have a value."""
    cid = persisted.get("chain_id")
    default = next((d for d in DEFAULT_CHAINS if d.chain_id == cid), None)
    base = default.to_dict() if default else {}
    merged = {**base, **persisted}
    # Drop any keys that aren't on the current Chain dataclass (forwards-
    # compatible with old configs that might carry unknown fields).
    return Chain(**{k: v for k, v in merged.items() if k in _CHAIN_FIELDS})

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
            s.window_geometry = data.get("window_geometry")
            s.splitter_state_main = data.get("splitter_state_main")
            s.splitter_state_left = data.get("splitter_state_left")
            raw_headers = data.get("header_states") or {}
            if isinstance(raw_headers, dict):
                s.header_states = {
                    str(k): str(v) for k, v in raw_headers.items()
                    if isinstance(v, str)
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
                "window_geometry": self.window_geometry,
                "splitter_state_main": self.splitter_state_main,
                "splitter_state_left": self.splitter_state_left,
                "header_states": dict(self.header_states),
            }
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(data, indent=2))

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

    # --- token-level user overrides -----------------------------------------

    def is_hidden(self, chain_id: int, address: str) -> bool:
        return (int(chain_id), address.lower()) in self.hidden_tokens

    def is_force_shown(self, chain_id: int, address: str) -> bool:
        return (int(chain_id), address.lower()) in self.shown_tokens

    def hide_token(self, chain_id: int, address: str) -> None:
        with self._lock:
            key = (int(chain_id), address.lower())
            self.hidden_tokens.add(key)
            self.shown_tokens.discard(key)
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
