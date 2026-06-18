"""ENS resolution, optionally verified through a Helios light client.

ENS reverse records aren't trustworthy on their own — anyone can claim any
name in their reverse record. To trust a name, the forward lookup of that
name must resolve back to the same address. ``lookup_ens_name`` does both
steps and only returns a name when the round-trip verifies.

**Verified mode.** ENS resolution is pure ``eth_call``s against mainnet
contracts (registry → resolver → ``addr``), which is exactly what a light
client can prove. When a Helios sidecar for mainnet is available, the
``verified_*`` helpers route the resolver reads through it, so the
``name ↔ address`` mapping is proof-verified against sync-committee-verified
state rather than trusted from a remote RPC. They return a ``verified`` flag
the UI surfaces as a badge.

**Strict CCIP.** Some names use offchain (CCIP / ``OffchainLookup``)
resolvers whose answer is fetched from an HTTP gateway — not provable. On the
verified path we disable CCIP-follow (``global_ccip_read_enabled = False``), so
only fully on-chain resolutions count as verified; an offchain name simply
fails the verified attempt and falls back to the normal (unverified, no-badge)
RPC path.

Mainnet only. Even when a user is browsing on Polygon / Arbitrum / etc., ENS
records live on Ethereum mainnet, so the resolver talks to chain 1 regardless
of the user's current view.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

from . import USER_AGENT

if TYPE_CHECKING:
    from .chains import Chain


log = logging.getLogger("qeth.ens")

# Forward resolves are single + user-initiated (typing a recipient), so it's
# worth waiting on a just-warming sidecar. Mass reverse-label lookups pass 0.0
# (verify only if the sidecar is already synced — never block the label).
_VERIFY_WAIT_S = 8.0


def _make_w3(rpc_url: str, ccip: bool):
    """A throwaway web3 client for one ENS query. ``ccip=False`` disables
    OffchainLookup-follow (the strict, fully-on-chain verified path)."""
    from web3 import Web3
    from web3.providers.rpc import HTTPProvider
    w3 = Web3(HTTPProvider(
        rpc_url,
        # Set Content-Type explicitly: passing a custom headers dict drops
        # web3's default, and Helios's strict JSON-RPC server 415s a POST
        # without "application/json" (DRPC is lenient, which masked this on
        # the public path).
        request_kwargs={
            "headers": {
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
            },
            "timeout": 15,
        },
    ))
    # When eth_call's ccip_read_enabled is None (ENS's internal calls), web3
    # consults this provider-level global; flip it off to forbid gateway hops.
    w3.provider.global_ccip_read_enabled = ccip
    return w3


def lookup_ens_name(
    rpc_url: str, address: str, *, ccip: bool = True,
) -> str | None:
    """Reverse-resolve ``address`` on mainnet ENS, verifying via a forward
    lookup. Returns the verified primary name, or None when no reverse record
    exists, the forward lookup mismatches, or any RPC error occurs (ENS data is
    informational — we never block on it). ``ccip=False`` forbids offchain
    gateway resolution (strict on-chain only)."""
    try:
        from eth_utils import to_checksum_address
    except ImportError:
        return None
    try:
        addr = to_checksum_address(address)
    except Exception:
        return None
    try:
        w3 = _make_w3(rpc_url, ccip)
        name = w3.ens.name(addr)
    except Exception as e:
        log.debug("ENS reverse lookup failed for %s: %s", addr, e)
        return None
    if not name:
        return None
    # Verify forward (belt-and-suspenders against web3 version drift — the cost
    # of a forged "vitalik.eth" claim on a random address is high). Reuses the
    # same w3, so the ccip policy applies to the round-trip too.
    try:
        forward = w3.ens.address(name)
    except Exception:
        return None
    if not forward or forward.lower() != addr.lower():
        return None
    return name


def resolve_ens_address(
    rpc_url: str, name: str, *, ccip: bool = True,
) -> str | None:
    """Forward-resolve an ENS name to a checksummed address on mainnet, or None
    when the name has no address record / any RPC error. ``ccip=False`` forbids
    offchain gateway resolution (strict on-chain only)."""
    try:
        from eth_utils import to_checksum_address
    except ImportError:
        return None
    try:
        w3 = _make_w3(rpc_url, ccip)
        addr = w3.ens.address(name)
    except Exception as e:
        log.debug("ENS forward resolve failed for %s: %s", name, e)
        return None
    if not addr:
        return None
    try:
        return to_checksum_address(addr)
    except Exception:
        return None


def verified_resolve_address(
    chain: Chain, name: str, wait_s: float = _VERIFY_WAIT_S,
) -> tuple[str | None, bool]:
    """Forward-resolve ``name`` → (address, verified). Tries the Helios path
    first (strict, no CCIP); on success the mapping is proof-verified. Falls
    back to the normal RPC (CCIP allowed) marked unverified — so an offchain
    name still resolves, just without the badge. The Helios-first / fallback
    orchestration is the shared ``verified.verified_or_fallback`` policy; this
    only supplies the ENS read (strict ⇔ ccip off)."""
    from .verified import verified_or_fallback
    return verified_or_fallback(
        chain,
        lambda url, strict: resolve_ens_address(url, name, ccip=not strict),
        wait_s=wait_s)


def verified_lookup_name(
    chain: Chain, address: str, wait_s: float = _VERIFY_WAIT_S,
) -> tuple[str | None, bool]:
    """Reverse-resolve ``address`` → (name, verified), Helios-first (strict),
    falling back to the normal RPC marked unverified."""
    from .verified import verified_or_fallback
    return verified_or_fallback(
        chain,
        lambda url, strict: lookup_ens_name(url, address, ccip=not strict),
        wait_s=wait_s)


class EnsResolveWorker(QThread):
    """One-shot forward resolution (name → address) off the Qt main thread.
    Emits ``resolved(name, address, verified)`` — ``address`` empty when the
    name doesn't resolve, ``verified`` True when it resolved through Helios."""

    resolved = Signal(str, str, bool)

    def __init__(self, chain: Chain, name: str, parent=None,
                 *, wait_s: float = _VERIFY_WAIT_S):
        super().__init__(parent)
        self._chain = chain
        self._name = name
        self._wait_s = wait_s

    def run(self) -> None:
        addr, verified = verified_resolve_address(
            self._chain, self._name, self._wait_s)
        self.resolved.emit(self._name, addr or "", verified and bool(addr))


class EnsReverseWorker(QThread):
    """One-shot reverse lookup off the Qt main thread. Emits
    ``resolved(address, name, verified)`` — ``name`` empty for "no verified
    name", ``verified`` True when resolved through Helios."""

    resolved = Signal(str, str, bool)

    def __init__(self, chain: Chain, address: str, parent=None,
                 *, wait_s: float = _VERIFY_WAIT_S):
        super().__init__(parent)
        self._chain = chain
        self._address = address
        self._wait_s = wait_s

    def run(self) -> None:
        name, verified = verified_lookup_name(
            self._chain, self._address, self._wait_s)
        self.resolved.emit(self._address, name or "", verified and bool(name))
