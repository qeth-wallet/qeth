"""Verified on-chain reads — proof-verified ``eth_call`` / multicall via Helios.

The thin layer between a caller that wants trustworthy chain data and
``qeth.helios`` (the light-client sidecar). When a Helios sidecar for the chain
is ready, reads route through it and are proof-verified against sync-committee-
verified state; otherwise they fall back to the chain's normal RPC, flagged
unverified. Callers get an ``(EthClient, verified)`` / ``(Chain, verified)``
pair and never touch the sidecar directly — this is the one place the "prefer
verified, fall back to direct" rule lives.

Read-side sibling of ``qeth.pyevm_fork`` / ``qeth.simulate``: those verify
state for a *hypothetical* transaction (a local EVM fork over proven state);
this verifies the result of *existing* view calls (``eth_call``,
``Multicall3.aggregate3``). Both rest on the same ``helios.verified_chain``
primitive, so this is the natural home should the two converge into one
verification library.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional, Tuple, TypeVar

from .helios import verified_chain

if TYPE_CHECKING:
    from .chain import EthClient
    from .chains import Chain

_T = TypeVar("_T")

# Default readiness wait for verified-read callers. A few seconds covers a cold
# checkpoint sync (~6 s measured); the reads are background enrichment, so a
# short warm-up is fine and a not-ready sidecar just yields the unverified path.
DEFAULT_WAIT_S = 8.0


def verified_or_plain(chain: "Chain", *, wait_s: float = DEFAULT_WAIT_S,
                      ) -> "Tuple[Chain, bool]":
    """``(chain_to_read_from, verified)`` — the Helios shadow of ``chain`` when a
    sidecar is ready (``verified`` True), else ``chain`` itself (``verified``
    False).

    For callers that read via a raw ``rpc_url`` / web3 (e.g. the ENS resolver
    helpers, which need web3's CCIP-policy knob); ``EthClient`` users want
    ``verified_client`` instead."""
    vc = verified_chain(chain, wait_s=wait_s)
    return (vc, True) if vc is not None else (chain, False)


def verified_or_fallback(
    chain: "Chain", read: "Callable[[str, bool], Optional[_T]]", *,
    wait_s: float = DEFAULT_WAIT_S,
) -> "Tuple[Optional[_T], bool]":
    """Run ``read(rpc_url, strict)`` preferring the Helios path → ``(result,
    verified)``.

    The verified attempt runs against the sidecar with ``strict=True`` (no
    offchain hops); a truthy result is returned as ``(result, True)``. If it's
    falsy (or no sidecar is ready), the unverified fallback runs against the
    plain chain with ``strict=False`` → ``(result, False)``.

    This is the policy for reads that can *also* answer unverified — an offchain
    (CCIP) ENS name fails the strict verified attempt but still resolves on the
    fallback, just without the badge. (Contrast ``verify_names``, which is
    verified-only: it re-reads on-chain solely to PROVE an existing hint.)"""
    vc = verified_chain(chain, wait_s=wait_s)
    if vc is not None:
        result = read(vc.rpc_url, True)
        if result:
            return result, True
    return read(chain.rpc_url, False), False


def verified_client(chain: "Chain", *, wait_s: float = DEFAULT_WAIT_S,
                    fallback: bool = True,
                    ) -> "Tuple[Optional[EthClient], bool]":
    """``(client, verified)`` — an ``EthClient`` whose reads (``call``,
    ``multicall``, ``rpc``) are Helios-proof-verified when a sidecar is ready
    (``verified`` True), else a plain client against ``chain`` (``verified``
    False). ``client.multicall()`` over the verified client is a *verified
    multicall*: one ``aggregate3`` whose every inner call is proven.

    With ``fallback=False`` the unverified client is suppressed — returns
    ``(None, False)`` when no sidecar is ready, for callers that must only ever
    surface proven data and decide for themselves what to do without it."""
    from .chain import EthClient
    target, verified = verified_or_plain(chain, wait_s=wait_s)
    if verified or fallback:
        return EthClient(target), verified
    return None, False
