"""Contract identity — name, verification, and deployment provenance.

For a contract the user is reviewing (a historical tx) or about to
interact with (a new tx), surface *what is this and who deployed it* so
something suspicious stands out before they sign:

  - an **unverified** contract (no published source),
  - one **deployed very recently** (days, not years),
  - one from a **deployer you've never dealt with** — vs. "same deployer
    as 90 of your other contracts" or "deployed by you".

Data comes from Etherscan v2, two endpoints, both returning **immutable**
facts (deployer/date never change; a name only appears once on
verification) — so identities are cached permanently on disk:

  - ``getcontractcreation`` → ``contractCreator`` + ``timestamp`` (and,
    definitively, *whether the address is a contract at all*),
  - ``getsourcecode``       → ``ContractName`` + verified flag.

Cache layout mirrors ``qeth.abi_cache``:
    CACHE_DIR / <chain_id> / <address_lower>.json
"""

from __future__ import annotations

import datetime
import json
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .abi import _urllib_transport
from .tokens import ETHERSCAN_V2_BASE, ETHERSCAN_V2_CHAINS

CACHE_DIR = Path.home() / ".qeth" / "contract_id"

# A contract younger than this reads as "new" — a soft caution flag.
NEW_CONTRACT_DAYS = 30


@dataclass
class ContractIdentity:
    """What we know about an on-chain address. ``is_contract`` False means
    it's a regular (externally-owned) account — no code, nothing to
    identify. For contracts, ``name``/``verified`` come from the source
    explorer and ``deployer``/``deployed_at`` from its creation tx."""

    address: str
    is_contract: bool
    name: Optional[str] = None          # ContractName; None when unverified
    verified: bool = False
    deployer: Optional[str] = None      # creator address
    deployed_at: Optional[int] = None   # unix timestamp of the creation tx

    @property
    def deployed_date(self) -> Optional[str]:
        if not self.deployed_at:
            return None
        return datetime.datetime.fromtimestamp(
            self.deployed_at, datetime.timezone.utc).strftime("%Y-%m-%d")

    def to_dict(self) -> dict:
        return {
            "address": self.address.lower(),
            "is_contract": self.is_contract,
            "name": self.name,
            "verified": self.verified,
            "deployer": self.deployer.lower() if self.deployer else None,
            "deployed_at": self.deployed_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ContractIdentity":
        return cls(
            address=d["address"],
            is_contract=bool(d.get("is_contract", True)),
            name=d.get("name") or None,
            verified=bool(d.get("verified")),
            deployer=d.get("deployer") or None,
            deployed_at=d.get("deployed_at"),
        )


class ContractIdentityCache:
    """Disk-backed store of identities, keyed by (chain, address). Every
    address we look up — contract or EOA — gets an entry, so a second
    open is instant and offline (the facts are immutable)."""

    def __init__(self, root: Optional[Path] = None):
        # Resolve CACHE_DIR at construction so tests can monkey-patch it.
        self.root = root if root is not None else CACHE_DIR

    def _path(self, chain_id: int, address: str) -> Path:
        return self.root / str(chain_id) / f"{address.lower()}.json"

    def load(self, chain_id: int, address: str) -> Optional[ContractIdentity]:
        p = self._path(chain_id, address)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return ContractIdentity.from_dict(data)
        except (KeyError, TypeError):
            return None

    def save(self, chain_id: int, identity: ContractIdentity) -> None:
        p = self._path(chain_id, identity.address)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(identity.to_dict(), separators=(",", ":")))

    def deployer_contract_count(self, chain_id: int, deployer: str) -> int:
        """How many cached contracts on this chain were deployed by
        ``deployer`` — the basis for "same deployer as N of your
        contracts". Scans the chain's cache dir (small JSON files)."""
        if not deployer:
            return 0
        target = deployer.lower()
        d = self.root / str(chain_id)
        if not d.is_dir():
            return 0
        count = 0
        for f in d.glob("*.json"):
            try:
                data = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and (data.get("deployer") or "").lower() == target:
                count += 1
        return count


class ContractIdentitySource:
    """Fetches identity from Etherscan v2 (multichain). Reuses the same
    endpoint/key plumbing as the ABI source."""

    def __init__(self, get_api_key: Callable[[], Optional[str]],
                 timeout: float = 15.0, transport=None):
        self._get_api_key = get_api_key
        self.timeout = timeout
        self._transport = transport or _urllib_transport
        self._supported = ETHERSCAN_V2_CHAINS

    def supports(self, chain_id: int) -> bool:
        return chain_id in self._supported and bool(self._get_api_key())

    def _get(self, chain_id: int, params: list) -> dict:
        key = self._get_api_key() or ""
        query = urllib.parse.urlencode(
            [("chainid", str(chain_id)), *params, ("apikey", key)])
        raw = self._transport(f"{ETHERSCAN_V2_BASE}?{query}", self.timeout)
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    def fetch(self, chain_id: int, address: str) -> Optional[ContractIdentity]:
        """Returns the identity, or ``None`` on an unsupported chain /
        transient error (so the caller leaves the cache untouched and can
        retry). A definitive "not a contract" comes back as a populated
        ``ContractIdentity(is_contract=False)``, which IS cached."""
        if not self.supports(chain_id):
            return None
        # Creation record: authoritative on contract-vs-EOA, plus deployer
        # and deployment time in one call.
        try:
            cr = self._get(chain_id, [
                ("module", "contract"),
                ("action", "getcontractcreation"),
                ("contractaddresses", address)])
        except Exception:
            return None
        res = cr.get("result")
        if not (isinstance(res, list) and res and isinstance(res[0], dict)):
            # No creation record → an externally-owned account.
            return ContractIdentity(address=address, is_contract=False)
        row = res[0]
        deployer = row.get("contractCreator") or None
        ts = row.get("timestamp")
        deployed_at = int(ts) if isinstance(ts, str) and ts.isdigit() else None

        # Name + verification (a failure here just leaves it unverified —
        # we still have the valuable provenance above).
        name: Optional[str] = None
        verified = False
        try:
            sc = self._get(chain_id, [
                ("module", "contract"),
                ("action", "getsourcecode"),
                ("address", address)])
            sres = sc.get("result")
            if isinstance(sres, list) and sres and isinstance(sres[0], dict):
                nm = (sres[0].get("ContractName") or "").strip()
                if nm:
                    name, verified = nm, True
        except Exception:
            pass

        return ContractIdentity(
            address=address, is_contract=True, name=name, verified=verified,
            deployer=deployer, deployed_at=deployed_at)


@dataclass
class IdentityBadge:
    """A one-line summary plus a severity the UI maps to a colour.
    ``level``: ``ok`` (known/verified), ``info`` (neutral, e.g. EOA),
    ``caution`` (verified but brand-new), ``warn`` (unverified)."""

    text: str
    level: str


def describe_identity(identity: ContractIdentity, *,
                      my_addresses, deployer_count: int = 0,
                      now_ts: float) -> IdentityBadge:
    """Render a contract identity as a human badge. ``deployer_count`` is
    how many of *your* cached contracts share this deployer (including
    this one); ``now_ts`` is the current unix time (passed in for
    testability)."""
    mine = {a.lower() for a in my_addresses}
    if not identity.is_contract:
        return IdentityBadge("Regular account (not a contract)", "info")

    parts: list[str] = []
    if identity.verified and identity.name:
        parts.append(identity.name)
        level = "ok"
    else:
        parts.append("⚠ Unverified contract")
        level = "warn"

    if identity.deployed_at:
        label = f"deployed {identity.deployed_date}"
        if (now_ts - identity.deployed_at) / 86400 < NEW_CONTRACT_DAYS:
            label += " (new)"
            if level == "ok":
                level = "caution"
        parts.append(label)

    deployer = identity.deployer
    if deployer and deployer.lower() in mine:
        parts.append("deployed by you")
    elif deployer:
        others = max(0, deployer_count - 1)
        if others > 0:
            parts.append(f"same deployer as {others} of your contracts")
        else:
            parts.append(f"deployer {deployer[:8]}…{deployer[-4:]}")

    return IdentityBadge(" · ".join(parts), level)
