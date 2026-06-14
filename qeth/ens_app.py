"""ENS app data layer — keyless name discovery + on-chain records.

Discover-then-verify (see ``docs/ens-app.md``): **BENS** — Blockscout's
*dedicated* ENS service (NOT its generic NFT endpoint) — gives the candidate
names an address owns, keyless and paginated, with owner / resolved-address /
expiry already attached. Records BENS doesn't return (text records, contenthash
/ IPFS) come from on-chain resolver calls. The chain is the source of truth; the
indexer is a swappable hint.

Qt-free so the parsing / tree / cache logic is unit-testable without a display.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import USER_AGENT

log = logging.getLogger("qeth.ens_app")

# Blockscout ENS (BENS) service — keyless, multichain. The chain id goes in the
# path: ``/api/v1/{chain_id}/...``. ENS itself is mainnet (chain 1).
BENS_BASE = "https://bens.services.blockscout.com/api/v1"

# Open in the official manager app.
ENS_APP_URL = "https://app.ens.domains/{name}"

CACHE_DIR = Path.home() / ".qeth" / "ens"

# Registrar grace period after expiry before a name is released (90 days).
GRACE_PERIOD_S = 90 * 24 * 3600

# Standard ENSIP-5 text-record keys worth reading (no on-chain enumeration of
# keys exists, so we probe a curated set).
TEXT_KEYS = (
    "avatar", "description", "url", "email", "com.twitter", "com.github",
    "com.discord", "org.telegram", "location",
)


@dataclass
class EnsName:
    """One ENS name as the indexer reports it (then verified on-chain by the
    caller). ``expiry_ts`` is None for subdomains (they ride the parent 2LD)."""

    name: str
    resolved_address: Optional[str] = None    # the addr record (where it points)
    owner: Optional[str] = None               # registry owner / controller
    expiry_ts: Optional[int] = None           # unix seconds, or None
    source: str = "owned"                     # owned | resolved | custom

    @property
    def label(self) -> str:
        """The left-most label (``alice`` in ``alice.vitalik.eth``)."""
        return self.name.split(".", 1)[0]

    @property
    def parent(self) -> Optional[str]:
        """The parent name (``vitalik.eth`` for ``alice.vitalik.eth``), or None
        for a 2LD / TLD."""
        parts = self.name.split(".")
        return ".".join(parts[1:]) if len(parts) > 2 else None

    @property
    def is_subdomain(self) -> bool:
        return self.name.count(".") > 1


@dataclass
class EnsRecords:
    """Resolver records read on-chain for one name (lazy, on expand)."""

    addresses: dict[str, str] = field(default_factory=dict)   # coin -> addr
    texts: dict[str, str] = field(default_factory=dict)       # key -> value
    contenthash: Optional[str] = None                         # e.g. ipfs://…


@dataclass
class EnsNode:
    """A name plus its owned subdomains — the tree the UI renders."""

    name: EnsName
    children: "list[EnsNode]" = field(default_factory=list)


# --- expiry ---------------------------------------------------------------

def expiry_status(expiry_ts: Optional[int], now_ts: int,
                  warn_window_s: int = 30 * 24 * 3600) -> str:
    """Classify a name's expiry: ``none`` (no expiry — subdomain), ``active``,
    ``expiring`` (within the warn window), ``grace`` (expired but renewable),
    ``expired`` (released — anyone can register)."""
    if not expiry_ts:
        return "none"
    if now_ts < expiry_ts - warn_window_s:
        return "active"
    if now_ts < expiry_ts:
        return "expiring"
    if now_ts < expiry_ts + GRACE_PERIOD_S:
        return "grace"
    return "expired"


# --- BENS discovery -------------------------------------------------------

def _iso_to_unix(s: Optional[str]) -> Optional[int]:
    """Parse BENS's ISO-8601 ``expiry_date`` (``2048-03-27T13:25:30.000Z``) to
    unix seconds, or None."""
    if not s:
        return None
    try:
        from datetime import datetime
        # fromisoformat is the right tool (flexible on fraction/offset, unlike
        # a fixed strptime format). The "Z"→"+00:00" swap is only for Python
        # 3.10 (our floor): 3.10's fromisoformat can't parse a bare "Z" — that
        # landed in 3.11. Drop this line when the floor moves to 3.11.
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _http_get_json(url: str, timeout: float = 20.0) -> dict:
    """Raw GET → parsed JSON. Sets the qeth UA (Cloudflare rejects the urllib
    default). Separated out so tests can stub it."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return json.load(f)


def _parse_name_item(it: dict) -> Optional[EnsName]:
    name = it.get("name")
    if not name:
        return None
    return EnsName(
        name=str(name),
        resolved_address=(it.get("resolved_address") or {}).get("hash"),
        owner=(it.get("owner") or {}).get("hash"),
        expiry_ts=_iso_to_unix(it.get("expiry_date")),
    )


def lookup_owned_names(
    chain_id: int, address: str, *,
    base_url: str = BENS_BASE,
    get_json: Callable[[str], dict] = _http_get_json,
    max_pages: int = 20,
) -> list[EnsName]:
    """The names ``address`` owns/controls, via BENS ``addresses:lookup``
    (keyless), paginated. ``owned_by`` = registry controller (the names the user
    can manage). The caller verifies each on-chain. Tolerant: any error returns
    what was gathered so far (an indexer is a hint, never blocking)."""
    out: list[EnsName] = []
    seen: set[str] = set()
    token: Optional[str] = None
    for _ in range(max_pages):
        q = {
            "address": address, "owned_by": "true", "resolved_to": "false",
            "only_active": "true", "page_size": "50",
        }
        if token:
            q["page_token"] = token
        url = f"{base_url}/{chain_id}/addresses:lookup?" + urllib.parse.urlencode(q)
        try:
            d = get_json(url)
        except Exception as e:
            log.debug("BENS lookup failed (%s): %s", address, e)
            break
        for it in d.get("items") or []:
            n = _parse_name_item(it)
            if n is not None and n.name.lower() not in seen:
                seen.add(n.name.lower())
                out.append(n)
        npp = d.get("next_page_params")
        token = npp.get("page_token") if isinstance(npp, dict) else None
        if not token:
            break
    return out


def fetch_name(
    chain_id: int, name: str, *,
    base_url: str = BENS_BASE,
    get_json: Callable[[str], dict] = _http_get_json,
) -> Optional[EnsName]:
    """One name's details via BENS ``domains/{name}`` (owner / resolved-address
    / expiry) — for a custom-pinned name we didn't get from the owner sweep.
    None on any failure (or a name BENS doesn't know)."""
    url = f"{base_url}/{chain_id}/domains/{urllib.parse.quote(name)}"
    try:
        d = get_json(url)
    except Exception as e:
        log.debug("BENS fetch_name failed (%s): %s", name, e)
        return None
    n = _parse_name_item(d)        # the detail object shares the item fields
    if n is not None:
        n.source = "custom"
    return n


# --- tree building (pure) -------------------------------------------------

def build_tree(names: list[EnsName]) -> list[EnsNode]:
    """Nest a flat name list into roots → owned subdomains, by suffix. A name
    whose parent is also in the set hangs under it; otherwise it's a root.
    Sorted alphabetically; children recurse."""
    by_name = {n.name: EnsNode(n) for n in names}
    roots: list[EnsNode] = []
    for node in by_name.values():
        p = node.name.parent
        if p and p in by_name:
            by_name[p].children.append(node)
        else:
            roots.append(node)

    def _sort(nodes: list[EnsNode]) -> None:
        nodes.sort(key=lambda x: x.name.name)
        for n in nodes:
            _sort(n.children)
    _sort(roots)
    return roots


# --- contenthash decode (pure, EIP-1577) ----------------------------------

def decode_contenthash(raw: Optional[str]) -> Optional[str]:
    """Decode an EIP-1577 contenthash hex to a ``scheme://…`` URL, or None.
    Covers the common IPFS (ipfs-ns + dag-pb CIDv1) and IPNS cases; returns a
    raw ``0x…`` marker for anything else so the UI can still show *something*."""
    if not raw or raw in ("0x", "0x0"):
        return None
    h = raw[2:] if raw.startswith("0x") else raw
    try:
        data = bytes.fromhex(h)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    # multicodec prefix: 0xe301 = ipfs-ns, 0xe501 = ipns-ns (varint-ish, the
    # 2-byte forms cover the overwhelming majority of ENS contenthashes).
    if data[:2] == b"\xe3\x01":
        scheme = "ipfs"
    elif data[:2] == b"\xe5\x01":
        scheme = "ipns"
    else:
        return "contenthash:" + raw            # unknown codec — show raw
    try:
        from base64 import b32encode
        cid_bytes = data[2:]
        # CIDv1 base32 ("b" prefix), lower-case, no padding — the form
        # gateways and ENS tooling use.
        b32 = b32encode(cid_bytes).decode("ascii").rstrip("=").lower()
        return f"{scheme}://b{b32}"
    except Exception:
        return "contenthash:" + raw


# --- on-chain records -----------------------------------------------------

def read_records(rpc_url: str, name: str, *,
                 text_keys: tuple = TEXT_KEYS) -> EnsRecords:
    """Read a name's resolver records on-chain: a curated set of text records
    plus the contenthash. Best-effort per record — a resolver that lacks one
    just omits it; any hard failure returns whatever was gathered. ``rpc_url``
    should be mainnet (or a verified Helios sidecar — same as ``ens.py``)."""
    rec = EnsRecords()
    try:
        from .ens import _make_w3
        from eth_utils import keccak
    except ImportError:
        return rec
    try:
        w3 = _make_w3(rpc_url, ccip=True)
    except Exception:
        return rec
    # namehash for the raw contenthash call
    node = b"\x00" * 32
    for part in reversed(name.split(".")):
        node = keccak(node + keccak(text=part))
    for key in text_keys:
        try:
            v = w3.ens.get_text(name, key)
            if v:
                rec.texts[key] = str(v)
        except Exception:
            pass
    # contenthash: resolver.contenthash(node) — selector 0xbc1c58d1
    try:
        resolver = w3.ens.resolver(name)
        if resolver is not None:
            data = "0xbc1c58d1" + node.hex()
            raw = w3.eth.call({"to": resolver.address, "data": data})
            rec.contenthash = decode_contenthash(_abi_bytes(raw))
    except Exception:
        pass
    return rec


def _abi_bytes(raw) -> Optional[str]:
    """Decode an ABI-encoded ``bytes`` return (offset, length, payload) to a
    ``0x…`` hex string, or None when empty."""
    b = bytes(raw) if not isinstance(raw, (bytes, bytearray)) else raw
    if len(b) < 64:
        return None
    length = int.from_bytes(b[32:64], "big")
    if length == 0:
        return None
    payload = b[64:64 + length]
    return "0x" + payload.hex()


# --- disk cache -----------------------------------------------------------

class EnsCache:
    """Per-(chain, address) cache of discovered names, JSON on disk — so the
    tree renders instantly on reopen while a refresh runs in the background.
    Mirrors ``WalletCache``'s shape."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.cache_dir = cache_dir if cache_dir is not None else CACHE_DIR

    def _path(self, chain_id: int, address: str) -> Path:
        return self.cache_dir / str(chain_id) / f"{address.lower()}.json"

    def load(self, chain_id: int, address: str) -> Optional[list[EnsName]]:
        p = self._path(chain_id, address)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("ENS cache parse failed %s: %s", p, e)
            return None
        out: list[EnsName] = []
        for d in data.get("names", []):
            if d.get("name"):
                out.append(EnsName(
                    name=str(d["name"]),
                    resolved_address=d.get("resolved_address"),
                    owner=d.get("owner"),
                    expiry_ts=d.get("expiry_ts"),
                    source=d.get("source", "owned"),
                ))
        return out

    def save(self, chain_id: int, address: str, names: list[EnsName]) -> None:
        from .fsatomic import atomic_write_text
        p = self._path(chain_id, address)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "chain_id": int(chain_id),
            "address": address.lower(),
            "names": [
                {"name": n.name, "resolved_address": n.resolved_address,
                 "owner": n.owner, "expiry_ts": n.expiry_ts, "source": n.source}
                for n in names
            ],
        }
        atomic_write_text(p, json.dumps(data, indent=2))
