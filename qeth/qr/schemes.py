"""Address derivation schemes for an imported QR account.

The device exports an account-level extended public key (its ``origin`` path);
a scheme says which non-hardened suffix indexes the individual addresses below
it. Two single-exchange schemes for now (BIP44 receive chain, and the flat
"legacy" chain); Ledger-Live's per-account hardened layout needs a scan each
and is a later slice.
"""

from __future__ import annotations

from collections.abc import Callable

# scheme name → (address index → non-hardened suffix below the exported node)
QR_ADDRESS_SCHEMES: dict[str, Callable[[int], list[int]]] = {
    "BIP44 (…/0/i)": lambda i: [0, i],
    "Legacy (…/i)": lambda i: [i],
}


def components_to_path(components) -> str:
    """crypto-keypath components ``[index, is_hardened, …]`` → a BIP32 path
    string (``m/44'/60'/0'``)."""
    parts = ["m"]
    items = list(components)
    for i in range(0, len(items) - 1, 2):
        index, hardened = items[i], items[i + 1]
        parts.append(f"{index}'" if hardened else str(index))
    return "/".join(parts)


def full_path(origin_components, suffix: list[int]) -> str:
    """The full derivation path (from master) for an address: the exported
    node's origin path plus the scheme's non-hardened suffix — this is what
    goes in the sign-request's crypto-keypath so the device knows which key."""
    base = components_to_path(origin_components)
    return base + "".join(f"/{i}" for i in suffix)
