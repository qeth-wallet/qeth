import json
import threading
from pathlib import Path
from typing import Optional

from .chains import Chain, DEFAULT_CHAINS

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
                s.chains = [Chain(**c) for c in chains_data]
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
