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
        return s

    def save(self) -> None:
        with self._lock:
            data = {
                "accounts": self.accounts,
                "chains": [c.to_dict() for c in self.chains],
                "current_chain_id": self.current_chain_id,
                "default_account": self.default_account,
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
