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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from ...abi import _urllib_transport
from ...fsatomic import atomic_write_text
from ...token_discovery import ETHERSCAN_V2_BASE, ETHERSCAN_V2_CHAINS

CACHE_DIR = Path.home() / ".qeth" / "contract_id"

# Bump when the cached shape — or the way a cached field is DERIVED — changes,
# so older entries are re-fetched rather than served stale. v2 added public
# name-tags (name_tag / deployer_label); v3 folds the OLI projectName into a
# bare name_tag ("Router v1.2" → "Curve Finance: Router v1.2"), and the final
# label is what's cached, so pre-v3 entries must re-fetch to pick it up.
_SCHEMA_VERSION = 3

# Blockscout's public metadata service — the Open Labels Initiative
# dataset. Returns name-tags ("AladdinDAO: Deployer", "Binance: Hot
# Wallet") for any address, free + keyless, where Etherscan paywalls them.
LABELS_BASE = "https://metadata.services.blockscout.com/api/v1/metadata"


def _tag_project(tag: dict) -> str:
    """The OLI tag's ``projectName`` (carried in its JSON-string ``meta``), or
    "" — the owning project ("Curve Finance") when the ``name`` itself doesn't
    include it."""
    meta = tag.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return ""
    return (meta.get("projectName") or "").strip() if isinstance(meta, dict) else ""


def _tag_label(tag: dict) -> str:
    """A name-tag rendered as "Project: Name". OLI sometimes bakes the project
    into ``name`` ("Binance: Hot Wallet") and sometimes only into
    ``projectName`` (Curve's "Router v1.2" + project "Curve Finance"); fold the
    latter in so every label reads consistently, without double-prefixing an
    already-``Project: Label`` name."""
    name = tag["name"]
    if ":" in name:                              # already "Project: Label"
        return name
    project = _tag_project(tag)
    if project and project.lower() not in name.lower():
        return f"{project}: {name}"
    return name

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
    name: str | None = None          # ContractName; None when unverified
    verified: bool = False
    deployer: str | None = None      # creator address
    deployed_at: int | None = None   # unix timestamp of the creation tx
    name_tag: str | None = None      # this address's public label (OLI)
    deployer_label: str | None = None  # the deployer's public label
    # True for a contract identified WITHOUT an Etherscan key: we know it's a
    # contract and (maybe) its public name-tag, but not its verified-source
    # name or deployer. Runtime-only and never cached — so adding a key later
    # re-fetches the full identity instead of being stuck on this stub.
    partial: bool = False

    @property
    def deployed_date(self) -> str | None:
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
            "name_tag": self.name_tag,
            "deployer_label": self.deployer_label,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ContractIdentity:
        return cls(
            address=d["address"],
            is_contract=bool(d.get("is_contract", True)),
            name=d.get("name") or None,
            verified=bool(d.get("verified")),
            deployer=d.get("deployer") or None,
            deployed_at=d.get("deployed_at"),
            name_tag=d.get("name_tag") or None,
            deployer_label=d.get("deployer_label") or None,
        )


class ContractIdentityCache:
    """Disk-backed store of identities, keyed by (chain, address). Every
    address we look up — contract or EOA — gets an entry, so a second
    open is instant and offline (the facts are immutable)."""

    def __init__(self, root: Path | None = None):
        # Resolve CACHE_DIR at construction so tests can monkey-patch it.
        self.root = root if root is not None else CACHE_DIR

    def _path(self, chain_id: int, address: str) -> Path:
        return self.root / str(chain_id) / f"{address.lower()}.json"

    def load(self, chain_id: int, address: str) -> ContractIdentity | None:
        p = self._path(chain_id, address)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        # Schema-version gate: an entry written by an older qeth (e.g.
        # before public name-tags existed) is treated as a miss so the
        # next open re-fetches it with the current shape, rather than
        # showing a permanently label-less identity from a stale file.
        if data.get("v") != _SCHEMA_VERSION:
            return None
        try:
            return ContractIdentity.from_dict(data)
        except (KeyError, TypeError):
            return None

    def save(self, chain_id: int, identity: ContractIdentity) -> None:
        p = self._path(chain_id, identity.address)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = identity.to_dict()
        payload["v"] = _SCHEMA_VERSION
        atomic_write_text(p, json.dumps(payload, separators=(",", ":")))

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

    def __init__(self, get_api_key: Callable[[], str | None],
                 timeout: float = 15.0, transport=None,
                 get_code: Callable[[int, str], str | None] | None = None):
        self._get_api_key = get_api_key
        self.timeout = timeout
        self._transport = transport or _urllib_transport
        self._supported = ETHERSCAN_V2_CHAINS
        # Keyless EOA-vs-contract probe (``eth_getCode`` via the chain RPC).
        # Lets an EOA recipient be identified with no Etherscan key — the
        # key is only needed for a *contract's* name/verified/deployer.
        self._get_code = get_code

    def supports(self, chain_id: int) -> bool:
        return chain_id in self._supported and bool(self._get_api_key())

    def _get(self, chain_id: int, params: list) -> dict:
        key = self._get_api_key() or ""
        query = urllib.parse.urlencode(
            [("chainid", str(chain_id)), *params, ("apikey", key)])
        raw = self._transport(f"{ETHERSCAN_V2_BASE}?{query}", self.timeout)
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    def fetch_labels(self, chain_id: int, addresses) -> dict:
        """Public name-tags for ``addresses`` from Blockscout's metadata
        service (keyless). Returns ``{address_lower: "Label"}`` for those
        that have a ``name``-type tag (highest ordinal wins); silent {} on
        any error or an unsupported chain."""
        addrs = [a for a in addresses if a]
        if not addrs:
            return {}
        query = urllib.parse.urlencode(
            {"addresses": ",".join(addrs), "chainId": str(chain_id)})
        try:
            raw = self._transport(f"{LABELS_BASE}?{query}", self.timeout)
            data = json.loads(raw)
        except Exception:
            return {}
        out: dict = {}
        entries = data.get("addresses") if isinstance(data, dict) else None
        for addr, info in (entries or {}).items():
            tags = [t for t in (info.get("tags") or [])
                    if t.get("tagType") == "name" and t.get("name")]
            if tags:
                out[addr.lower()] = _tag_label(
                    max(tags, key=lambda t: t.get("ordinal") or 0))
        return out

    def fetch(self, chain_id: int, address: str) -> ContractIdentity | None:
        """Returns the identity, or ``None`` on an unsupported chain /
        transient error (so the caller leaves the cache untouched and can
        retry). A definitive "not a contract" comes back as a populated
        ``ContractIdentity(is_contract=False)``, which IS cached."""
        if not self.supports(chain_id):
            # No Etherscan key (or a non-Etherscan-v2 chain): we can still
            # recognise an EOA recipient without one. See _fetch_keyless.
            return self._fetch_keyless(chain_id, address)
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
            # No creation record → an externally-owned account. Still worth
            # a label lookup (e.g. "Binance: Hot Wallet" for a send target).
            labels = self.fetch_labels(chain_id, [address])
            return ContractIdentity(
                address=address, is_contract=False,
                name_tag=labels.get(address.lower()))
        row = res[0]
        deployer = row.get("contractCreator") or None
        ts = row.get("timestamp")
        deployed_at = int(ts) if isinstance(ts, str) and ts.isdigit() else None

        # Name/verification (getsourcecode, Etherscan) and the public
        # name-tags (Blockscout's metadata service) are independent and hit
        # different hosts, so fetch them together — the identity resolves in
        # two round-trips instead of three, which the user sees as a faster
        # "identifying…". A getsourcecode failure just leaves it unverified;
        # the provenance from the creation record above still stands.
        name: str | None = None
        verified = False
        to_label = [address] + ([deployer] if deployer else [])
        with ThreadPoolExecutor(max_workers=2) as ex:
            sc_fut = ex.submit(self._get, chain_id, [
                ("module", "contract"),
                ("action", "getsourcecode"),
                ("address", address)])
            labels = ex.submit(self.fetch_labels, chain_id, to_label).result()
            try:
                sres = sc_fut.result().get("result")
                if isinstance(sres, list) and sres and isinstance(sres[0], dict):
                    nm = (sres[0].get("ContractName") or "").strip()
                    if nm:
                        name, verified = nm, True
            except Exception:
                pass
        return ContractIdentity(
            address=address, is_contract=True, name=name, verified=verified,
            deployer=deployer, deployed_at=deployed_at,
            name_tag=labels.get(address.lower()),
            deployer_label=labels.get(deployer.lower()) if deployer else None)

    def _fetch_keyless(self, chain_id: int,
                       address: str) -> ContractIdentity | None:
        """Identify an address with no Etherscan key. ``eth_getCode``
        (keyless, cheap, not CU-limited) tells EOA-vs-contract, and
        Blockscout supplies the public label (also keyless).

        An EOA is FULLY resolved this way — identical to the keyed EOA result
        — so it's cached. A contract resolves only *partially* (we get its
        public name-tag, e.g. "ENS: ETH Registrar Controller", but not the
        verified-source name or deployer, which need the explorer). That stub
        is still worth showing — the name-tag is the most useful bit — but is
        flagged ``partial`` so the worker doesn't cache it, leaving a later
        key free to fetch the full identity."""
        if self._get_code is None:
            return None
        try:
            code = self._get_code(chain_id, address)
        except Exception:
            return None
        if code is None:
            return None
        stripped = code[2:] if code.startswith("0x") else code
        is_contract = bool(stripped.strip("0"))   # any non-zero bytecode
        labels = self.fetch_labels(chain_id, [address])
        return ContractIdentity(
            address=address, is_contract=is_contract,
            name_tag=labels.get(address.lower()),
            partial=is_contract)


@dataclass
class IdentityBadge:
    """A one-line summary plus a severity the UI maps to a colour.
    ``level``: ``ok`` (known/verified), ``info`` (neutral, e.g. EOA),
    ``caution`` (verified but brand-new), ``warn`` (unverified)."""

    text: str
    level: str


def describe_identity(identity: ContractIdentity, *,
                      my_addresses, deployer_count: int = 0,
                      interaction_count: int | None = None,
                      context: str = "interact",
                      now_ts: float) -> IdentityBadge:
    """Render a contract identity as a human badge. ``deployer_count`` is
    how many of *your* cached contracts share this deployer (including
    this one); ``interaction_count`` is your prior usage of this address in
    the cached history (None = don't show it); ``context`` picks the verb —
    ``"interact"`` ("you've interacted N×", for a contract you call) vs
    ``"send"`` ("sent here N×", for a transfer destination). ``now_ts`` is
    the current unix time (passed in for testability)."""
    mine = {a.lower() for a in my_addresses}
    if not identity.is_contract:
        who = identity.name_tag or "Regular account (not a contract)"
        if interaction_count == 0:
            return IdentityBadge(f"{who}\n⚠ first time sending here", "caution")
        text = who
        if interaction_count:
            text += f"\nsent here {interaction_count:,}× before"
        # A labeled EOA (a known exchange/entity) is reassuring; an
        # unlabeled one is neutral.
        return IdentityBadge(text, "ok" if identity.name_tag else "info")

    # The badge is rendered over several lines (one fact-group per line)
    # so a long identity stays scannable instead of one " · " run-on.
    if identity.name_tag:
        headline = identity.name_tag          # curated public label wins
        level = "ok"
    elif identity.verified and identity.name:
        headline = identity.name
        level = "ok"
    elif identity.partial:
        # No Etherscan key, so we couldn't check the source — say so plainly
        # rather than assert "unverified" (which we don't actually know).
        headline = "Contract · add an Etherscan key to verify"
        level = "info"
    else:
        headline = "⚠ Unverified contract"
        level = "warn"

    # Provenance line: when + who deployed it.
    provenance: list[str] = []
    if identity.deployed_at:
        label = f"deployed {identity.deployed_date}"
        if (now_ts - identity.deployed_at) / 86400 < NEW_CONTRACT_DAYS:
            label += " (new)"
            if level == "ok":
                level = "caution"
        provenance.append(label)
    deployer = identity.deployer
    if deployer and deployer.lower() in mine:
        provenance.append("deployed by you")
    elif identity.deployer_label:
        seg = f"by {identity.deployer_label}"      # "by AladdinDAO: Deployer"
        others = max(0, deployer_count - 1)
        if others > 0:
            seg += f" (+{others} of your contracts)"
        provenance.append(seg)
    elif deployer:
        others = max(0, deployer_count - 1)
        if others > 0:
            provenance.append(f"same deployer as {others} of your contracts")
        else:
            provenance.append(f"deployer {deployer[:8]}…{deployer[-4:]}")

    lines = [headline]
    if provenance:
        lines.append(" · ".join(provenance))
    # Familiarity: how often you've used it. A never-before-seen contract
    # is a soft caution (pairs with "(new)" / unverified); a heavily-used
    # one is reassuring.
    if interaction_count is not None:
        if interaction_count >= 1:
            lines.append(f"sent here {interaction_count:,}× before"
                         if context == "send"
                         else f"you've interacted {interaction_count:,}×")
        else:
            lines.append("⚠ first time sending here" if context == "send"
                         else "⚠ first interaction")
            if level == "ok":
                level = "caution"

    return IdentityBadge("\n".join(lines), level)
