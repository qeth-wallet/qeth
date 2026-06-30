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
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from collections.abc import Callable

from . import USER_AGENT

if TYPE_CHECKING:
    from .chains import Chain

log = logging.getLogger("qeth.ens_app")

# How long to wait for a Helios sidecar to become ready before falling back to
# the unverified RPC. Mirrors ``ens._VERIFY_WAIT_S``; the records read is a
# background, best-effort enrichment so a few seconds of warm-up is fine.
VERIFY_WAIT_S = 8.0

# Retry budget for the ownership/resolution batch against the transient
# post-sync "header for hash not found" (see ``verify_names``).
_VERIFY_RETRIES = 3
_VERIFY_RETRY_DELAY_S = 2.0

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

# ENS core contracts (mainnet). The registry is the root of truth for who
# *controls* a node (can set its records); the .eth BaseRegistrar is the
# ERC-721 whose tokenId is the 2LD's labelhash — the "NFT id" — giving the
# *registrant* (the real owner; the controller may be a delegate).
ENS_REGISTRY = "0x00000000000C2E074eC69A0dFb2997BA6C7d2e1e"
ENS_ETH_REGISTRAR = "0x57f1887a8BF19b14fC0dF6Fd9B2acc9Af147eA85"
# The NameWrapper holds wrapped names as ERC-1155s keyed by uint256(namehash).
# For a wrapped name BOTH registry.owner(node) and registrar.ownerOf(tokenId)
# return THIS address (the wrapper holds them) — the real owner is
# NameWrapper.ownerOf(uint256(namehash)). Without unwrapping, every wrapped name
# (which the ENS app shows as yours) would read as "not owned".
ENS_NAME_WRAPPER = "0xD4416b13d2b3a9aBae7AcD5D6C2BbDBE25686401"
# ETHRegistrarController — the .eth registration/renewal entry point. renew()
# and the rentPrice() oracle live here. VERIFIED ON-CHAIN (not from memory):
# this is the controller whose renew() the BaseRegistrar actually accepts; the
# later-deployed 0x59E1…0CE547 exposes the same rentPrice() view but its renew()
# reverts (it isn't an authorized controller), so don't "upgrade" to it without
# re-checking on-chain.
ENS_ETH_CONTROLLER = "0x253553366Da8546fC250F225fe3d25d0C782303b"

_SEL_OWNER = bytes.fromhex("02571be3")      # registry.owner(bytes32)
_SEL_RESOLVER = bytes.fromhex("0178b8bf")   # registry.resolver(bytes32)
_SEL_OWNER_OF = bytes.fromhex("6352211e")   # ERC721.ownerOf(uint256)
_SEL_ADDR = bytes.fromhex("3b3b57de")       # resolver.addr(bytes32) — legacy
_SEL_ADDR_COIN = bytes.fromhex("f1cb7e06")  # resolver.addr(bytes32,uint256)
_SEL_TEXT = bytes.fromhex("59d1d43c")       # resolver.text(bytes32,string)
_SEL_CONTENTHASH = bytes.fromhex("bc1c58d1")  # resolver.contenthash(bytes32)
_SEL_SUPPORTS = bytes.fromhex("01ffc9a7")   # supportsInterface(bytes4)
_SEL_RESOLVE = bytes.fromhex("9061b923")    # resolve(bytes,bytes) — IExtendedResolver
_SEL_RENT_PRICE = bytes.fromhex("83e7f6ff")  # controller.rentPrice(string,uint256)
# ENSIP-10 IExtendedResolver interface id (== the resolve() selector). A resolver
# that supports it answers via resolve()/CCIP rather than plain text()/addr().
_IFACE_EXTENDED = bytes.fromhex("9061b923")
_ETH_COIN_TYPE = 60                         # ENSIP-9 coin type for ETH


@dataclass
class EnsName:
    """One ENS name as the indexer reports it (then verified on-chain by the
    caller). ``expiry_ts`` is None for subdomains (they ride the parent 2LD)."""

    name: str
    resolved_address: str | None = None    # the addr record (where it points)
    owner: str | None = None               # registry owner / controller
    expiry_ts: int | None = None           # unix seconds, or None
    source: str = "owned"                     # owned | resolved | custom

    @property
    def label(self) -> str:
        """The left-most label (``alice`` in ``alice.vitalik.eth``)."""
        return self.name.split(".", 1)[0]

    @property
    def parent(self) -> str | None:
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
    contenthash: str | None = None                         # e.g. ipfs://…


@dataclass
class EnsNode:
    """A name plus its owned subdomains — the tree the UI renders."""

    name: EnsName
    children: list[EnsNode] = field(default_factory=list)


def name_warning(name: str) -> str | None:
    """A look-alike/confusable warning for ``name`` per ENSIP-15 normalization —
    the same check the ENS app uses — or None when the name is clean.

    Scammers register names that *render* like a trusted one but contain
    invisible joiners (``v‍i‍t‍a‍l‍i‍k‍.eth``) or mixed-script homoglyphs (a
    Cyrillic ``а`` for the Latin ``a``); both are disallowed by normalization,
    which raises. A name that normalizes to a *different* string isn't in
    canonical form and displays deceptively. (ENSIP-15 still allows valid emoji
    names, so this doesn't false-positive on those.)

    Uses web3's ``ens`` normalizer; if it's unavailable, returns None rather
    than guessing."""
    try:
        from ens.utils import normalize_name
    except ImportError:
        return None
    try:
        norm = normalize_name(name)
    except Exception:
        return "Look-alike — hidden or mixed-script characters"
    if norm != name:
        return f"Look-alike — normalizes to “{norm}”"
    return None


@dataclass
class OwnershipCheck:
    """On-chain (Helios-verifiable) state for one name — the ground truth the
    BENS hint is checked against. ``controller`` (registry owner) and, for .eth
    2LDs, ``registrant`` (registrar NFT owner) answer "is this really yours";
    ``resolved_address`` answers "does it point where the indexer claims". Any
    field is None when the call reverted or the name uses an offchain (CCIP)
    resolver we can't prove on-chain."""

    controller: str | None = None
    registrant: str | None = None
    resolved_address: str | None = None
    resolver: str | None = None      # registry.resolver(node) — for records
    wrapped: bool = False               # held by the ENS NameWrapper
    # True when the ownership read DEFINITIVELY landed — so a None controller
    # means "the node has no owner / doesn't exist" (a droppable indexer lie),
    # not "the read failed" (unknown, keep). False on a failed/transient read.
    owner_known: bool = False

    def owned_by(self, address: str) -> bool:
        """True when ``address`` is the controller or the registrant."""
        a = address.lower()
        return ((self.controller or "").lower() == a
                or (self.registrant or "").lower() == a)

    def disowned_by(self, address: str) -> bool:
        """True when the chain DEFINITIVELY says ``address`` is neither the
        controller nor the registrant — a different owner, or no owner at all
        (the node doesn't exist). Distinct from an unknown/failed read."""
        return self.owner_known and not self.owned_by(address)


# --- expiry ---------------------------------------------------------------

def expiry_status(expiry_ts: int | None, now_ts: int,
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

def _iso_to_unix(s: str | None) -> int | None:
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


ZERO_ADDRESS = "0x" + "00" * 20
ZERO_ADDR_BYTES = b"\x00" * 20   # the zero address as a raw 20-byte word


def nonzero_addr(addr: str | None) -> str | None:
    """The address, or None when it's empty / the zero address. A cleared ENS
    record reads back as the zero address (and indexers report it that way); we
    treat that as "no address" rather than showing a literal zero."""
    if not addr or addr == ZERO_ADDRESS:
        return None
    return addr


def _parse_name_item(it: dict) -> EnsName | None:
    name = it.get("name")
    if not name:
        return None
    return EnsName(
        name=str(name),
        resolved_address=nonzero_addr((it.get("resolved_address") or {}).get("hash")),
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
    token: str | None = None
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


# BENS's owned_by sweep is keyed on the registry *controller*, so a .eth name
# you hold as the registrant (the NFT) but whose manager is delegated elsewhere
# — a common DAO/multisig setup, e.g. crv.eth — never shows up there. We close
# that gap by enumerating the BaseRegistrar ERC-721s the address holds
# (Blockscout, keyless) and turning each tokenId (== uint256(labelhash)) back
# into a name via the ENS metadata service. Mainnet only — .eth registrations
# live on L1. (Wrapped names are already covered: BENS returns them with
# owner=NameWrapper because it matches on the wrapped owner.)
BLOCKSCOUT_MAINNET = "https://eth.blockscout.com"
ENS_METADATA_BASE = "https://metadata.ens.domains/mainnet"


def _registrar_token_ids(
    address: str, *,
    base_url: str = BLOCKSCOUT_MAINNET,
    get_json: Callable[[str], dict] = _http_get_json,
    max_pages: int = 10,
) -> list[int]:
    """The BaseRegistrar ERC-721 tokenIds (== uint256(labelhash)) ``address``
    holds, via Blockscout's NFT-ownership API (keyless, paginated). Tolerant:
    returns what it gathered on any error."""
    out: list[int] = []
    reg = ENS_ETH_REGISTRAR.lower()
    base = f"{base_url}/api/v2/addresses/{address}/nft"
    url = f"{base}?" + urllib.parse.urlencode({"type": "ERC-721"})
    for _ in range(max_pages):
        try:
            d = get_json(url)
        except Exception as e:
            log.debug("Blockscout NFT lookup failed (%s): %s", address, e)
            break
        for it in d.get("items") or []:
            tok = it.get("token") or {}
            addr = tok.get("address") or tok.get("address_hash") or ""
            if addr.lower() != reg:
                continue
            tid = it.get("id")
            try:
                out.append(int(tid))
            except (TypeError, ValueError):
                continue
        npp = d.get("next_page_params")
        if not isinstance(npp, dict):
            break
        url = f"{base}?" + urllib.parse.urlencode(npp)
    return out


def _ens_metadata_name(
    token_id: int, *,
    base_url: str = ENS_METADATA_BASE,
    registrar: str = ENS_ETH_REGISTRAR,
    get_json: Callable[[str], dict] = _http_get_json,
) -> str | None:
    """Reverse a BaseRegistrar tokenId to its ``.eth`` name via the ENS metadata
    service (``metadata.ens.domains``). None on any failure."""
    url = f"{base_url}/{registrar}/{token_id}"
    try:
        d = get_json(url)
    except Exception as e:
        log.debug("ENS metadata lookup failed (%s): %s", token_id, e)
        return None
    name = d.get("name")
    return str(name) if name else None


def lookup_registrant_names(
    chain_id: int, address: str, *,
    skip_labelhashes: set[int] | None = None,
    get_json: Callable[[str], dict] = _http_get_json,
) -> list[EnsName]:
    """Unwrapped ``.eth`` names ``address`` holds as the *registrant* (NFT
    owner) — the ones BENS's controller-keyed sweep misses. Enumerates the
    BaseRegistrar NFTs held, skipping any tokenId already found via BENS
    (``skip_labelhashes`` = those names' uint256 labelhashes) to avoid redundant
    metadata calls, then resolves each remaining tokenId to a name. Mainnet
    only; tolerant of every failure (a hint, never blocking)."""
    if chain_id != 1:
        return []
    skip = skip_labelhashes or set()
    out: list[EnsName] = []
    seen: set[str] = set()
    for tid in _registrar_token_ids(address, get_json=get_json):
        if tid in skip:
            continue
        name = _ens_metadata_name(tid, get_json=get_json)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            # source="registrant": this came from a fresh on-chain NFT read, so
            # the verify pass must not drop it when its (Helios-verified, head-
            # lagging) ownership read still shows the previous registrant — a
            # just-transferred name would otherwise flicker in and vanish.
            out.append(EnsName(name, source="registrant"))
    return out


def fetch_name(
    chain_id: int, name: str, *,
    base_url: str = BENS_BASE,
    get_json: Callable[[str], dict] = _http_get_json,
) -> EnsName | None:
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

def decode_contenthash(raw: str | None) -> str | None:
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


# --- on-chain reads -------------------------------------------------------

def namehash(name: str) -> bytes:
    """EIP-137 namehash of an ENS name — the 32-byte node used as the key in
    the registry and resolvers."""
    from eth_utils import keccak
    node = b"\x00" * 32
    if name:
        for part in reversed(name.split(".")):
            node = keccak(node + keccak(text=part))
    return node


def _labelhash(label: str) -> bytes:
    """keccak256 of a single label — the .eth registrar's tokenId (as bytes)."""
    from eth_utils import keccak
    return keccak(text=label)


def _is_eth_2ld(name: str) -> bool:
    """True for a second-level ``label.eth`` — the only names that are .eth
    registrar NFTs (subdomains and other TLDs aren't)."""
    return name.endswith(".eth") and name.count(".") == 1


def rent_price(chain, label: str, duration_s: int, *, client=None) -> int | None:
    """Cost in wei to renew (or register) ``label``.eth for ``duration_s``
    seconds — ``base + premium`` from the ETHRegistrarController's rentPrice
    oracle (the premium is 0 for a straight renewal; it only applies to a name
    in its post-expiry temporary-premium window). ``label`` is the bare label
    ('vitalik', not 'vitalik.eth'). Returns None if the read doesn't land — the
    caller must not proceed to send value it couldn't price."""
    from .chain import EthClient
    from eth_abi import decode as abi_decode, encode as abi_encode
    cl = client if client is not None else EthClient(chain)
    data = "0x" + (_SEL_RENT_PRICE
                   + abi_encode(["string", "uint256"], [label, duration_s])).hex()
    try:
        out = cl.call({"to": ENS_ETH_CONTROLLER, "data": data})
        base, premium = abi_decode(["uint256", "uint256"],
                                   bytes.fromhex(out[2:]))
        return int(base) + int(premium)
    except Exception:
        log.debug("ENS rent_price read failed for %s", label, exc_info=True)
        return None


def _decode_addr_word(raw) -> str | None:
    """Decode a 32-byte address word (registry.owner / ownerOf / legacy addr).
    None for the zero address or short/empty data."""
    b = bytes(raw) if not isinstance(raw, (bytes, bytearray)) else raw
    if len(b) < 32:
        return None
    word = b[:32]
    if word[12:32] == ZERO_ADDR_BYTES:
        return None
    from eth_utils import to_checksum_address
    return to_checksum_address("0x" + word[12:32].hex())


def _decode_addr_bytes(raw) -> str | None:
    """Decode ``addr(bytes32,uint256)``'s dynamic-bytes return (a 20-byte
    address payload) to a checksummed address, or None."""
    h = _abi_bytes(raw)
    if not h:
        return None
    payload = bytes.fromhex(h[2:])
    if len(payload) != 20 or payload == ZERO_ADDR_BYTES:
        return None
    from eth_utils import to_checksum_address
    return to_checksum_address("0x" + payload.hex())


def verify_names(
    chain: Chain, names: list[str], *, wait_s: float = VERIFY_WAIT_S,
) -> tuple[dict[str, OwnershipCheck], bool]:
    """Batch on-chain check of controller + registrant + resolved-address for
    ``names`` → ``({name_lower: OwnershipCheck}, verified)``.

    Two ``aggregate3`` multicalls total — round 1 reads registry.owner +
    registry.resolver (+ registrar.ownerOf for .eth 2LDs); round 2 reads each
    resolver's ``addr`` — so a whole wallet's names verify in two round-trips,
    not 2N. Everything runs through a Helios sidecar, so the reads are proof-
    verified against light-client state — at the chain head (``latest``), so a
    just-changed owner / resolved address shows immediately instead of lagging
    finality.

    **Verified-only.** Returns ``({}, False)`` when no Helios sidecar is ready:
    the resolved-address this confirms already came from the indexer, so we
    re-read on-chain only when we can actually PROVE it. (Records, by contrast,
    fall back to an unverified read — they're data the indexer never had.)

    Offchain (CCIP) resolvers can't be proven on-chain: their ``addr`` reverts
    here, leaving ``resolved_address`` None — those are followed unverified
    elsewhere, never badged."""
    from .verified import verified_client
    # fallback=False: ownership/resolution is re-read on-chain ONLY when we can
    # prove it (the indexer already gave us a hint), so no sidecar → nothing.
    client, verified = verified_client(chain, wait_s=wait_s, fallback=False)
    if client is None:
        return {}, False
    # A just-synced sidecar can briefly fail an eth_call ("header for hash not
    # found") while the execution RPC catches up. The reads are batched into
    # several aggregate3 calls, so a transient can hit SOME batches and not
    # others — leaving those names with owner_known False (read didn't land).
    # Retry until EVERY name's ownership read lands, so a name isn't silently
    # left un-dropped/un-badged just because its batch blipped. After the budget
    # is spent we accept the partial result (those names stay unknown → kept,
    # never a false drop). An empty result (total failure) also retries.
    states: dict[str, OwnershipCheck] = {}
    for attempt in range(_VERIFY_RETRIES):
        try:
            states = _read_name_states(client, names)
        except Exception:
            log.debug("ENS verify_names read failed", exc_info=True)
            states = {}
        if states and all(st.owner_known for st in states.values()):
            return states, verified
        if attempt < _VERIFY_RETRIES - 1:
            time.sleep(_VERIFY_RETRY_DELAY_S)
    return states, verified


def _read_name_states(client, names: list[str]) -> dict[str, OwnershipCheck]:
    """The multicall body of ``verify_names``, factored out so it can be tested
    against a fake client without a live chain."""
    nodes = {n: namehash(n) for n in names}
    out = {n.lower(): OwnershipCheck() for n in names}

    owner_p: dict = {}
    resolver_p: dict = {}
    registrant_p: dict = {}
    wrapped_p: dict = {}
    # Read at "latest": show the MOST RECENT on-chain state (Helios still proves
    # it — the chain head is sync-committee-verified, just not finalized), so a
    # just-changed owner/record appears at once instead of lagging ~2 epochs
    # behind finality and surfacing the obsolete value.
    with client.multicall(block="latest") as mc:
        for n in names:
            node = nodes[n]
            owner_p[n] = mc.add(ENS_REGISTRY, _SEL_OWNER + node,
                                decoder=_decode_addr_word)
            resolver_p[n] = mc.add(ENS_REGISTRY, _SEL_RESOLVER + node,
                                   decoder=_decode_addr_word)
            # NameWrapper.ownerOf(uint256(node)) — the real owner when wrapped;
            # zero (→ None) for unwrapped names.
            wrapped_p[n] = mc.add(ENS_NAME_WRAPPER, _SEL_OWNER_OF + node,
                                  decoder=_decode_addr_word)
            if _is_eth_2ld(n):
                tid = _labelhash(n.split(".")[0])
                registrant_p[n] = mc.add(ENS_ETH_REGISTRAR, _SEL_OWNER_OF + tid,
                                         decoder=_decode_addr_word)

    wrapper = ENS_NAME_WRAPPER.lower()
    resolvers: dict = {}
    for n in names:
        st = out[n.lower()]
        wrapped_owner = wrapped_p[n].value if wrapped_p[n].success else None
        controller = owner_p[n].value if owner_p[n].success else None
        registrant = registrant_p[n].value if (
            n in registrant_p and registrant_p[n].success) else None
        # A wrapped name reads as owned-by-the-wrapper in both roles; the real
        # owner is the ERC-1155 holder. Substitute it and flag the wrap.
        if (controller or "").lower() == wrapper or \
                (registrant or "").lower() == wrapper:
            st.wrapped = True
            if (controller or "").lower() == wrapper:
                controller = wrapped_owner
            if (registrant or "").lower() == wrapper:
                registrant = wrapped_owner
        st.controller = controller
        st.registrant = registrant
        # The ownership answer is definitive iff the registry.owner read landed
        # — and, when wrapped, the NameWrapper.ownerOf read too (else a failed
        # wrapper read would look like "no owner" and wrongly drop the name).
        owner_known = owner_p[n].success
        if st.wrapped:
            owner_known = owner_known and wrapped_p[n].success
        st.owner_known = owner_known
        if resolver_p[n].success and resolver_p[n].value:
            st.resolver = resolver_p[n].value
            resolvers[n] = resolver_p[n].value

    if resolvers:
        coin_p: dict = {}
        legacy_p: dict = {}
        coin_arg = _ETH_COIN_TYPE.to_bytes(32, "big")
        with client.multicall(block="latest") as mc:
            for n, r in resolvers.items():
                node = nodes[n]
                coin_p[n] = mc.add(r, _SEL_ADDR_COIN + node + coin_arg,
                                   decoder=_decode_addr_bytes)
                legacy_p[n] = mc.add(r, _SEL_ADDR + node,
                                     decoder=_decode_addr_word)
        for n in resolvers:
            addr = (coin_p[n].value if coin_p[n].success else None) \
                or (legacy_p[n].value if legacy_p[n].success else None)
            out[n.lower()].resolved_address = addr
    return out


def _decode_abi_string(raw) -> str | None:
    """Decode an ABI ``string`` return (resolver.text). None when empty."""
    from eth_abi import decode as abi_decode
    s = abi_decode(["string"], bytes(raw))[0]
    return s or None


def _decode_bool(raw) -> bool:
    b = bytes(raw) if not isinstance(raw, (bytes, bytearray)) else raw
    return len(b) >= 32 and int.from_bytes(b[:32], "big") == 1


def _dns_encode(name: str) -> bytes:
    """DNS wire format of a name (length-prefixed labels + 0x00) — the form
    ENSIP-10 ``resolve(name, data)`` expects."""
    out = b""
    for label in name.split("."):
        b = label.encode("utf-8")
        out += bytes([len(b)]) + b
    return out + b"\x00"


def _read_records_ccip(
    w3, resolver: str, name: str, *, text_keys: tuple = TEXT_KEYS,
) -> EnsRecords:
    """Follow CCIP-Read (EIP-3668) for an offchain / ExtendedResolver name: call
    ``resolver.resolve(dnsname, inner)`` with ``ccip_read_enabled`` so web3 walks
    the OffchainLookup to the gateway and returns the signed answer. The result
    is UNVERIFIABLE on-chain (a gateway can't be proof-checked), so callers must
    mark it unverified. Best-effort per record; a slow/missing gateway just
    yields fewer records, never raises out."""
    from eth_abi import decode as abi_decode, encode as abi_encode
    rec = EnsRecords()
    node = namehash(name)
    dnsname = _dns_encode(name)

    def _resolve(inner: bytes) -> bytes:
        data = _SEL_RESOLVE + abi_encode(["bytes", "bytes"], [dnsname, inner])
        raw = w3.eth.call({"to": resolver, "data": "0x" + data.hex()},
                          "latest", ccip_read_enabled=True)
        return abi_decode(["bytes"], bytes(raw))[0]   # inner call's return data

    for key in text_keys:
        try:
            inner = _SEL_TEXT + abi_encode(["bytes32", "string"], [node, key])
            res = _resolve(inner)
            val = abi_decode(["string"], res)[0] if res else ""
            if val:
                rec.texts[key] = str(val)
        except Exception:
            pass
    try:
        payload = abi_decode(["bytes"], _resolve(_SEL_CONTENTHASH + node))[0]
        if payload:
            rec.contenthash = decode_contenthash("0x" + payload.hex())
    except Exception:
        pass
    return rec


def _read_records_via_client(
    client, name: str, *, text_keys: tuple = TEXT_KEYS, block: str = "latest",
    resolver: str | None = None,
) -> tuple[EnsRecords, bool, bool, str | None]:
    """Read a name's resolver records on-chain → ``(records, ok, extended,
    resolver)``.

    - ``ok`` False = a read DIDN'T LAND (a transient multicall failure, e.g. a
      just-synced sidecar's "header for hash not found"). An empty result with
      ``ok`` False is a glitch, NOT "no records", so it must never overwrite
      records on screen.
    - ``extended`` True = the resolver is an ENSIP-10 ``IExtendedResolver``
      (CCIP/offchain). Its on-chain ``text()``/``contenthash()`` revert by
      design, so an empty on-chain read is EXPECTED (``ok`` True) and the caller
      should follow the gateway via ``_read_records_ccip`` (unverified).

    Round 1 looks up the resolver; round 2 batches ``supportsInterface`` +
    every ``text(node,key)`` + ``contenthash(node)`` into one ``aggregate3``."""
    from eth_abi import encode as abi_encode
    rec = EnsRecords()
    node = namehash(name)
    if resolver is None:
        with client.multicall(block=block) as mc:
            resolver_p = mc.add(ENS_REGISTRY, _SEL_RESOLVER + node,
                                decoder=_decode_addr_word)
        if not resolver_p.success:
            return rec, False, False, None      # resolver lookup glitched
        resolver = resolver_p.value             # None ⇒ zero ⇒ no resolver
    if not resolver:
        return rec, True, False, None           # landed: name has no resolver
    text_p: dict = {}
    iface = _IFACE_EXTENDED + b"\x00" * 28      # supportsInterface(bytes4) arg
    with client.multicall(block=block) as mc:
        supports_p = mc.add(resolver, _SEL_SUPPORTS + iface, decoder=_decode_bool)
        # The ETH address record (where the name points). Read it here at the
        # chain head alongside text/contenthash so a setAddr shows the moment it
        # confirms — not only via the finalized ownership pass (~2 epochs late).
        addr_p = mc.add(resolver, _SEL_ADDR + node, decoder=_decode_addr_word)
        for key in text_keys:
            data = _SEL_TEXT + abi_encode(["bytes32", "string"], [node, key])
            text_p[key] = mc.add(resolver, data, decoder=_decode_abi_string)
        content_p = mc.add(
            resolver, _SEL_CONTENTHASH + node,
            decoder=lambda raw: decode_contenthash(_abi_bytes(raw)))
    extended = bool(supports_p.success and supports_p.value)
    if any(p.success for p in (addr_p, *text_p.values(), content_p)):
        if addr_p.success and addr_p.value:
            rec.addresses[str(_ETH_COIN_TYPE)] = addr_p.value
        for key, p in text_p.items():
            if p.success and p.value:
                rec.texts[key] = str(p.value)
        if content_p.success and content_p.value:
            rec.contenthash = content_p.value
        return rec, True, extended, resolver
    if extended:
        return rec, True, extended, resolver    # CCIP: on-chain empty expected
    return rec, False, extended, resolver        # glitch — neither landed


def read_records(
    chain: Chain, name: str, *, text_keys: tuple = TEXT_KEYS,
    client=None, resolver: str | None = None,
) -> tuple[EnsRecords, bool]:
    """Fast, UNVERIFIED record read → ``(records, ok)`` (see
    ``_read_records_via_client`` for ``ok``). The first-paint path: shows records
    in ~1 s instead of waiting on the verified read to proof-fetch every slot
    through Helios; the ✓ comes later via ``verified_read_records``.

    ``client`` reuses a warm ``EthClient`` across expands; ``resolver``
    pre-supplies the name's resolver to skip a round-trip — if that (possibly
    stale) resolver lands empty, we re-read without it to self-correct."""
    from .chain import EthClient
    cl = client if client is not None else EthClient(chain)
    try:
        rec, ok, extended, res = _read_records_via_client(
            cl, name, text_keys=text_keys, block="latest", resolver=resolver)
        if (resolver is not None and ok and not extended
                and not rec.texts and not rec.contenthash):
            # a stale pre-supplied resolver landed empty → re-read without it
            rec, ok, extended, res = _read_records_via_client(
                cl, name, text_keys=text_keys, block="latest")
        if extended and res and not rec.texts and not rec.contenthash:
            # offchain (CCIP): follow the gateway. Unverifiable, but it beats a
            # row stuck "loading". A gateway failure just leaves it empty.
            try:
                rec = _read_records_ccip(cl.w3, res, name, text_keys=text_keys)
            except Exception:
                log.debug("ENS CCIP follow failed", exc_info=True)
            ok = True
        return rec, ok
    except Exception:
        log.debug("ENS read_records failed", exc_info=True)
        return EnsRecords(), False


def verified_read_records(
    chain: Chain, name: str, *,
    wait_s: float = VERIFY_WAIT_S, text_keys: tuple = TEXT_KEYS,
) -> tuple[EnsRecords, bool]:
    """Verified-ONLY record read at the chain HEAD → ``(records, verified)``.
    Reads at ``latest`` (sync-committee-verified by Helios, not finalized) so the
    ✓ reflects the MOST RECENT on-chain records, not the ~2-epoch-stale finalized
    ones. ``verified`` is True when a Helios sidecar proved the on-chain reads
    that LANDED — including a name whose records are served on-chain even though
    its resolver ALSO implements the extended (CCIP) interface for subnames.
    ``(EnsRecords(), False)`` (no upgrade emit) only when nothing verifiable came
    back: a glitch (``ok`` False), or a resolver that served NOTHING on-chain and
    is extended — i.e. the records exist only offchain, which ``read_records``
    already fetched via the gateway and which can't be proof-checked."""
    from .verified import verified_client
    client, verified = verified_client(chain, wait_s=wait_s, fallback=False)
    if client is None or not verified:
        return EnsRecords(), False
    # Retry the just-synced-sidecar transient (same as verify_names) so on-chain
    # records reliably earn their ✓ instead of intermittently missing it.
    for attempt in range(_VERIFY_RETRIES):
        try:
            rec, ok, extended, _ = _read_records_via_client(
                client, name, text_keys=text_keys, block="latest")
        except Exception:
            log.debug("ENS verified_read_records failed", exc_info=True)
            rec, ok, extended = EnsRecords(), False, False
        if ok:
            offchain_only = extended and not rec.texts and not rec.contenthash
            return (EnsRecords(), False) if offchain_only else (rec, True)
        if attempt < _VERIFY_RETRIES - 1:
            time.sleep(_VERIFY_RETRY_DELAY_S)
    return EnsRecords(), False


def _abi_bytes(raw) -> str | None:
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

    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir if cache_dir is not None else CACHE_DIR

    def _path(self, chain_id: int, address: str) -> Path:
        return self.cache_dir / str(chain_id) / f"{address.lower()}.json"

    def load(self, chain_id: int, address: str) -> list[EnsName] | None:
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
                    resolved_address=nonzero_addr(d.get("resolved_address")),
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

    # --- per-name records (keyed by name, not address) -------------------

    def _records_path(self, chain_id: int, name: str) -> Path:
        return self.cache_dir / str(chain_id) / "records" / f"{name.lower()}.json"

    def load_records(
        self, chain_id: int, name: str,
    ) -> tuple[EnsRecords, bool] | None:
        """Cached ``(records, verified)`` for a name, or None. So re-expanding a
        name (or reopening the app) paints its records instantly while a refresh
        runs."""
        p = self._records_path(chain_id, name)
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning("ENS records cache parse failed %s: %s", p, e)
            return None
        rec = EnsRecords(
            addresses=dict(d.get("addresses") or {}),
            texts=dict(d.get("texts") or {}),
            contenthash=d.get("contenthash"),
        )
        return rec, bool(d.get("verified"))

    def save_records(self, chain_id: int, name: str, rec: EnsRecords,
                     verified: bool) -> None:
        from .fsatomic import atomic_write_text
        p = self._records_path(chain_id, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "chain_id": int(chain_id), "name": name.lower(),
            "addresses": rec.addresses, "texts": rec.texts,
            "contenthash": rec.contenthash, "verified": bool(verified),
        }
        atomic_write_text(p, json.dumps(data, indent=2))

    def forget_records(self, chain_id: int, name: str) -> None:
        """Drop a name's cached records — so a refresh after the user CHANGED
        them won't paint the now-stale (possibly verified) old value before the
        fresh read lands."""
        p = self._records_path(chain_id, name)
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("ENS records cache unlink failed %s: %s", p, e)
