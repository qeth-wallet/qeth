"""ENS write calldata — set records + manage subdomains.

The write-side companion to ``ens_app`` (which reads). Each builder returns
``(to_addr, data_hex)`` for an ordinary transaction the wallet's existing
sign+broadcast flow can carry — record writes go to the name's resolver,
subdomain creation to the registry (unwrapped) or the NameWrapper (wrapped).

Qt-free and pure: it only encodes calldata (``selector + eth_abi.encode(...)``,
the same shape ``ens_app`` uses for reads) so it's unit-testable without a chain
or a wallet. Authorization is enforced on-chain (a write the caller isn't
permitted to make simply reverts, which the sign dialog's simulation surfaces).
"""

from __future__ import annotations

from base64 import b32decode

from .ens_app import (
    ENS_ETH_CONTROLLER, ENS_ETH_REGISTRAR, ENS_NAME_WRAPPER, ENS_REGISTRY,
    _labelhash, namehash,
)

# Latest canonical mainnet PublicResolver — the resolver to point a name at when
# it has none set, so records can then be written. (Same one vitalik.eth uses.)
PUBLIC_RESOLVER = "0x231b0Ee14048e9dCcD1d247744d114a4EB5E8E63"

# Write selectors (verified keccak(sig)[:4]).
_SEL_SET_ADDR = bytes.fromhex("d5fa2b00")          # setAddr(bytes32,address)
_SEL_SET_ADDR_COIN = bytes.fromhex("8b95dd71")     # setAddr(bytes32,uint256,bytes)
_SEL_SET_TEXT = bytes.fromhex("10f13a8c")          # setText(bytes32,string,string)
_SEL_SET_CONTENTHASH = bytes.fromhex("304e6ade")   # setContenthash(bytes32,bytes)
_SEL_SET_RESOLVER = bytes.fromhex("1896f70a")      # registry.setResolver(bytes32,address)
# registry.setSubnodeRecord(bytes32,bytes32,address,address,uint64)
_SEL_SUBNODE_RECORD = bytes.fromhex("5ef2c7f0")
# registry.setSubnodeOwner(bytes32,bytes32,address) — reassign a subnode's owner
# (manager) without touching its resolver/records.
_SEL_SUBNODE_OWNER = bytes.fromhex("06ab5923")
# registry.setOwner(bytes32,address) — reassign a node's OWN manager (registry
# owner). Callable by the node's current manager; works for a 2LD or a subdomain.
_SEL_SET_OWNER = bytes.fromhex("5b0fc9c3")
# registry.setRecord(bytes32,address,address,uint64) — set a node's own owner,
# resolver and ttl in one call (used zeroed to relinquish a subnode you manage).
_SEL_SET_RECORD = bytes.fromhex("cf408823")
# NameWrapper.setSubnodeRecord(bytes32,string,address,address,uint64,uint32,uint64)
_SEL_WRAPPED_SUBNODE = bytes.fromhex("24c1af44")
# NameWrapper.setSubnodeOwner(bytes32,string,address,uint32,uint64) — reassign a
# wrapped subnode's owner, or (with owner=0) BURN it: the wrapped delete path.
_SEL_WRAPPED_SUBNODE_OWNER = bytes.fromhex("c658e086")
_SEL_RENEW = bytes.fromhex("acf1a841")             # controller.renew(string,uint256)
# Ownership transfer — the .eth name is an NFT: ERC-721 on the BaseRegistrar
# (unwrapped, tokenId = labelhash) or ERC-1155 on the NameWrapper (wrapped,
# id = namehash). safeTransferFrom for both (reverts if the recipient is a
# contract that can't receive the token, which protects against loss).
_SEL_SAFE_TRANSFER_721 = bytes.fromhex("42842e0e")   # (address,address,uint256)
_SEL_SAFE_TRANSFER_1155 = bytes.fromhex("f242432a")  # (addr,addr,uint256,uint256,bytes)
# BaseRegistrar.reclaim(uint256 id, address owner) — set the registry manager
# (controller) of an unwrapped .eth 2LD. id = labelhash(label).
_SEL_RECLAIM = bytes.fromhex("28ed4f6c")

ZERO_ADDRESS = "0x" + "00" * 20

# Registration duration is charged per second; a "year" in the UI is this many.
# 365 days (the ENS-app convention) — exactness doesn't matter for renewal since
# the oracle prices per second and the user sees the cost before signing.
SECONDS_PER_YEAR = 365 * 24 * 60 * 60

# ENSIP-9 coin types (SLIP-44). EVM chains use ENSIP-11: 0x80000000 | chain_id.
ETH_COIN_TYPE = 60
COIN_TYPES: dict[str, int] = {
    "ETH": 60, "BTC": 0, "LTC": 2, "DOGE": 3, "ETC": 61,
    "OP": 0x80000000 | 10, "ARB": 0x80000000 | 42161,
    "BASE": 0x80000000 | 8453, "MATIC": 0x80000000 | 137,
}

Tx = tuple[str, str]   # (to_addr, data_hex)


def _abi(types: list[str], args: list) -> bytes:
    from eth_abi import encode as abi_encode
    return abi_encode(types, args)


def _tx(to: str, selector: bytes, body: bytes) -> Tx:
    return to, "0x" + (selector + body).hex()


# --- contenthash (inverse of ens_app.decode_contenthash) ------------------

def encode_contenthash(url: str) -> bytes:
    """Encode an ``ipfs://b…`` / ``ipns://b…`` URL to EIP-1577 contenthash bytes
    (the inverse of ``ens_app.decode_contenthash``). Empty/blank → ``b""`` (clears
    the record). Raises ``ValueError`` on a malformed value."""
    url = (url or "").strip()
    if not url:
        return b""
    if url.startswith("ipfs://"):
        codec, body = b"\xe3\x01", url[len("ipfs://"):]
    elif url.startswith("ipns://"):
        codec, body = b"\xe5\x01", url[len("ipns://"):]
    else:
        raise ValueError("content must be an ipfs:// or ipns:// URL")
    if not body.startswith("b"):
        raise ValueError("expected a base32 CIDv1 (a 'b…' identifier)")
    b32 = body[1:].upper()
    b32 += "=" * (-len(b32) % 8)             # restore base32 padding
    try:
        return codec + b32decode(b32)
    except Exception as e:
        raise ValueError(f"invalid CID: {e}") from e


# --- record writes (to = the name's resolver) -----------------------------

def set_addr(resolver: str, name: str, address: str) -> Tx:
    """Set the ETH address record (legacy ``setAddr(node,address)``)."""
    body = _abi(["bytes32", "address"], [namehash(name), address])
    return _tx(resolver, _SEL_SET_ADDR, body)


def set_coin_addr(resolver: str, name: str, coin_type: int,
                  addr_bytes: bytes) -> Tx:
    """Set a multichain address (ENSIP-9 ``setAddr(node,coinType,bytes)``).
    ``addr_bytes`` is the raw address payload (20 bytes for EVM coins). Empty
    clears it."""
    body = _abi(["bytes32", "uint256", "bytes"],
                [namehash(name), coin_type, addr_bytes])
    return _tx(resolver, _SEL_SET_ADDR_COIN, body)


def set_text(resolver: str, name: str, key: str, value: str) -> Tx:
    """Set a text record (empty value clears it)."""
    body = _abi(["bytes32", "string", "string"], [namehash(name), key, value])
    return _tx(resolver, _SEL_SET_TEXT, body)


def set_contenthash(resolver: str, name: str, url: str) -> Tx:
    """Set the contenthash from an ``ipfs://`` / ``ipns://`` URL (empty clears)."""
    body = _abi(["bytes32", "bytes"], [namehash(name), encode_contenthash(url)])
    return _tx(resolver, _SEL_SET_CONTENTHASH, body)


def set_resolver(name: str, resolver: str = PUBLIC_RESOLVER) -> Tx:
    """Point the name at a resolver (registry write) — needed before records can
    be set on a name that has none."""
    body = _abi(["bytes32", "address"], [namehash(name), resolver])
    return _tx(ENS_REGISTRY, _SEL_SET_RESOLVER, body)


# --- subdomains -----------------------------------------------------------

def add_subnode(parent_name: str, label: str, owner: str, *, wrapped: bool,
                resolver: str = PUBLIC_RESOLVER, fuses: int = 0,
                expiry: int = 0, ttl: int = 0) -> Tx:
    """Create (or reassign) ``label.parent_name`` owned by ``owner``, with a
    resolver set in the same call. Wrapped parents go through the NameWrapper
    (string label + fuses + expiry); unwrapped through the registry (labelhash)."""
    parent_node = namehash(parent_name)
    if wrapped:
        body = _abi(
            ["bytes32", "string", "address", "address", "uint64", "uint32", "uint64"],
            [parent_node, label, owner, resolver, ttl, fuses, expiry])
        return _tx(ENS_NAME_WRAPPER, _SEL_WRAPPED_SUBNODE, body)
    body = _abi(
        ["bytes32", "bytes32", "address", "address", "uint64"],
        [parent_node, _labelhash(label), owner, resolver, ttl])
    return _tx(ENS_REGISTRY, _SEL_SUBNODE_RECORD, body)


# --- registration (renewal) -----------------------------------------------

def renew(label: str, duration_s: int) -> Tx:
    """Extend a ``label``.eth 2LD's registration by ``duration_s`` seconds, via
    ``ETHRegistrarController.renew(string,uint256)``. ``label`` is the bare label
    ('vitalik', not 'vitalik.eth'). The call is PAYABLE — the caller sets
    ``msg.value`` from ``ens_app.rent_price`` (plus a small buffer for ETH/USD
    oracle drift between pricing and mining; the controller refunds any
    overpayment). Anyone may renew any name, so this needs no ownership."""
    body = _abi(["string", "uint256"], [label, duration_s])
    return _tx(ENS_ETH_CONTROLLER, _SEL_RENEW, body)


def transfer_name(name: str, from_addr: str, to_addr: str, *,
                  wrapped: bool) -> Tx:
    """Transfer ownership of a ``.eth`` 2LD to ``to_addr`` by moving its NFT.

    Wrapped names move as an ERC-1155 on the NameWrapper (token id =
    ``uint256(namehash)``); unwrapped names as the ERC-721 on the BaseRegistrar
    (token id = ``uint256(labelhash(label))``). ``from_addr`` is the current
    registrant (the signing account). Only the registrant can transfer — a
    controller-only account's call reverts, which the sign dialog surfaces.

    Note for unwrapped names: this moves the *registrant* (the NFT/ownership).
    The registry *manager* (who sets records) is a separate role that stays put
    until the new owner reclaims it — standard ENS behaviour. Wrapped names
    carry both in the one ERC-1155 transfer."""
    if wrapped:
        token_id = int.from_bytes(namehash(name), "big")
        body = _abi(
            ["address", "address", "uint256", "uint256", "bytes"],
            [from_addr, to_addr, token_id, 1, b""])
        return _tx(ENS_NAME_WRAPPER, _SEL_SAFE_TRANSFER_1155, body)
    token_id = int.from_bytes(_labelhash(name.split(".")[0]), "big")
    body = _abi(["address", "address", "uint256"],
                [from_addr, to_addr, token_id])
    return _tx(ENS_ETH_REGISTRAR, _SEL_SAFE_TRANSFER_721, body)


def set_owner(name: str, manager: str) -> Tx:
    """Reassign a node's registry *manager* (owner) to ``manager``, via
    ``registry.setOwner(node, owner)``.

    This is the general "change manager" path: callable by the node's CURRENT
    manager (registry owner), for a 2LD **or** a subdomain alike. It's how a
    manager hands the role to another address — the case ``reclaim`` (registrant-
    only) and ``setSubnodeOwner`` (the parent's power) don't cover. Wrapped names
    hold their registry owner in the NameWrapper, so this is unwrapped-only; a
    wrapped node's ``setOwner`` from the caller reverts (the sign dialog's
    simulation surfaces it)."""
    body = _abi(["bytes32", "address"], [namehash(name), manager])
    return _tx(ENS_REGISTRY, _SEL_SET_OWNER, body)


def set_manager(name: str, manager: str) -> Tx:
    """Set the registry *manager* (controller) of an unwrapped ``.eth`` 2LD to
    ``manager``, via ``BaseRegistrar.reclaim(id, owner)`` (id = labelhash).

    The manager is the registry owner — the role that sets records, the
    resolver and subdomains. Only the *registrant* (the NFT owner) may reclaim;
    a manager-only account's call reverts. The common use is reclaiming the
    manager role to yourself so you can edit records on a name whose ownership
    (the NFT) you hold but whose manager is still someone else.

    Wrapped names hold both roles in the NameWrapper token and manage the
    controller through it, not through ``reclaim`` — this builder is for
    unwrapped names."""
    token_id = int.from_bytes(_labelhash(name.split(".")[0]), "big")
    body = _abi(["uint256", "address"], [token_id, manager])
    return _tx(ENS_ETH_REGISTRAR, _SEL_RECLAIM, body)


def set_subnode_manager(parent_name: str, label: str, manager: str) -> Tx:
    """Set the manager (registry owner) of an *unwrapped* subdomain
    ``label.parent_name`` to ``manager``, via
    ``registry.setSubnodeOwner(parentNode, labelHash, owner)``.

    This is how the owner of a name manages a subdomain someone else currently
    holds: only the PARENT's controller may call, so the name's owner can
    reassign / reclaim ``ops.foo.eth`` (e.g. back to themselves so they can then
    edit its records). Unlike ``setSubnodeRecord`` it leaves the subdomain's
    resolver and records untouched. It's the subdomain analogue of ``set_manager``
    (which reclaims a 2LD via the BaseRegistrar).

    Wrapped parents manage subnodes through the NameWrapper (string label +
    fuses + a parent-bounded expiry) — out of scope here; gate to unwrapped."""
    body = _abi(["bytes32", "bytes32", "address"],
                [namehash(parent_name), _labelhash(label), manager])
    return _tx(ENS_REGISTRY, _SEL_SUBNODE_OWNER, body)


def set_wrapped_subnode_manager(parent_name: str, label: str, manager: str) -> Tx:
    """Reassign a *wrapped* subdomain ``label.parent_name``'s owner (its manager)
    to ``manager``, via ``NameWrapper.setSubnodeOwner(parentNode, label, owner,
    0, 0)`` — the wrapped analogue of ``set_subnode_manager``.

    ``fuses=0`` adds nothing (fuses can only be burned, never cleared) and
    ``expiry=0`` is clamped up to the subnode's current expiry (the NameWrapper
    never reduces it), so both are preserved. Callable by the wrapped PARENT's
    token owner; reverts if the subname is emancipated (``PARENT_CANNOT_CONTROL``)
    — the sign dialog's simulation surfaces that before signing."""
    body = _abi(["bytes32", "string", "address", "uint32", "uint64"],
                [namehash(parent_name), label, manager, 0, 0])
    return _tx(ENS_NAME_WRAPPER, _SEL_WRAPPED_SUBNODE_OWNER, body)


def remove_subnode(parent_name: str, label: str) -> Tx:
    """Delete an *unwrapped* subdomain ``label.parent_name`` — clear its owner,
    resolver and TTL in the registry via
    ``setSubnodeRecord(parentNode, labelHash, 0, 0, 0)``.

    Setting the subnode's owner to the zero address makes ``recordExists``
    false, so the subdomain is gone (this is the ENS-app "Delete subname" for an
    unwrapped name). Only the PARENT's controller may call, so the owner of a
    name can remove a subdomain it granted — the deletion analogue of
    ``set_subnode_manager``. Records are removed too (resolver → 0).

    Wrapped parents delete subnodes through the NameWrapper — out of scope here;
    gate to unwrapped (like ``set_subnode_manager``)."""
    body = _abi(["bytes32", "bytes32", "address", "address", "uint64"],
                [namehash(parent_name), _labelhash(label),
                 ZERO_ADDRESS, ZERO_ADDRESS, 0])
    return _tx(ENS_REGISTRY, _SEL_SUBNODE_RECORD, body)


def relinquish_subnode(name: str) -> Tx:
    """Delete an *unwrapped* subdomain you MANAGE (are the registry controller
    of) — clear its own owner, resolver and TTL via
    ``registry.setRecord(node, 0, 0, 0)``.

    Reaches the SAME registry state as ``remove_subnode`` (owner = resolver =
    ttl = 0 → the node stops existing), but authorised against the SUBDOMAIN
    itself rather than its parent — so a subdomain's manager can give it up even
    when the parent name belongs to someone else. (``setSubnodeRecord`` on the
    parent and ``setRecord`` on the child compute the same node and write the
    same slots; only the authorised caller differs.) The parent's owner can
    recreate the subnode afterwards — inherent to subdomains. Wrapped names are
    held by the NameWrapper, not the caller, so this is unwrapped-only (mirrors
    ``remove_subnode``)."""
    body = _abi(["bytes32", "address", "address", "uint64"],
                [namehash(name), ZERO_ADDRESS, ZERO_ADDRESS, 0])
    return _tx(ENS_REGISTRY, _SEL_SET_RECORD, body)


def remove_wrapped_subnode(parent_name: str, label: str) -> Tx:
    """Delete a *wrapped* subdomain ``label.parent_name`` via the NameWrapper:
    ``setSubnodeOwner(parentNode, label, address(0), 0, 0)`` burns the subnode's
    wrapper token and clears its registry owner (``_unwrap`` to the zero
    address) — the wrapped analogue of ``remove_subnode`` (which is registry-
    only, for unwrapped names).

    Callable by the wrapped PARENT's token owner (``onlyTokenOwner(parentNode)``)
    — so the owner of a wrapped name removes a subdomain under it. Reverts if the
    subnode has ``CANNOT_UNWRAP`` burned or is emancipated
    (``PARENT_CANNOT_CONTROL``); the sign dialog's simulation surfaces that
    before signing rather than us reading fuses up front."""
    return set_wrapped_subnode_manager(parent_name, label, ZERO_ADDRESS)


def eth_addr_bytes(address: str) -> bytes:
    """The 20 raw bytes of a 0x address — for ``set_coin_addr`` on EVM coins."""
    a = address[2:] if address.startswith("0x") else address
    return bytes.fromhex(a)
