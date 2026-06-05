"""Contract-ABI source + calldata decoder.

Two pieces here:

- ``BlockscoutAbiSource.fetch(chain_id, address)`` hits Blockscout's
  v2 ``/api/v2/smart-contracts/{address}`` endpoint, which not only
  returns the contract's own ABI but also enumerates any proxy
  implementations. Proxy ABIs are recursively resolved and merged
  with the proxy's own surface, so a ``transfer`` call to the USDC
  proxy decodes against the FiatTokenV2_2 implementation
  transparently. Returns the merged ABI list, ``False`` when no
  verified source is available anywhere in the chain, or raises
  ``AbiSourceError`` for transient failures so callers don't
  negative-cache an HTTP blip.
- ``decode_call(abi, input_data, address=None)`` runs the calldata
  through web3.py's ABI decoder.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from typing import Optional, Union

from . import USER_AGENT
from .tokens import (   # reuse the per-chain maps + Etherscan v2 endpoint
    BLOCKSCOUT_INSTANCES, ETHERSCAN_V2_BASE, ETHERSCAN_V2_CHAINS,
)


log = logging.getLogger("qeth.abi")

Abi = list[dict]


class AbiSourceError(Exception):
    pass


# Well-known storage slots holding a proxy's implementation address.
# Probed (in order) via the chain RPC when Blockscout's v2 endpoint —
# the only one that reports implementations — is unavailable, so proxy
# contracts still resolve to their implementation ABI:
#   - EIP-1967
#   - legacy OpenZeppelin/zeppelinos (Circle's FiatTokenProxy, e.g. USDC)
#   - EIP-1822 / UUPS
#   - Polygon PoS UpgradableProxy, keccak256("matic.network.proxy.
#     implementation") — used by every bridged PoS token (USDT, WETH, …)
_PROXY_IMPL_SLOTS = (
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
    "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3",
    "0xc5f16f0fcc639fa48a6947836d9850f504798523bf8c9a3a87d5876cf622bcf7",
    "0xbaab7dbf64751104133af04abc7d9979f0fda3b059a322a8333f533d3f32bf7f",
)


def _impl_from_storage(storage_reader, chain_id: int,
                       address: str) -> Optional[str]:
    """Probe the well-known proxy implementation slots via
    ``storage_reader`` (callable ``(chain_id, address, slot) -> 0x-hex``);
    return the first non-zero implementation address, or None. Shared by
    every ABI source so proxy resolution works regardless of explorer."""
    if storage_reader is None:
        return None
    for slot in _PROXY_IMPL_SLOTS:
        try:
            raw = storage_reader(chain_id, address, slot)
        except Exception as e:
            log.warning("proxy-slot read failed: %s", e)
            return None
        if not raw:
            continue
        try:
            val = int(raw, 16) if isinstance(raw, str) else int(raw)
        except (TypeError, ValueError):
            continue
        if val:
            # Low 20 bytes are the implementation address.
            return "0x" + format(val & ((1 << 160) - 1), "040x")
    return None


class BlockscoutAbiSource:
    """Blockscout v2 ``/api/v2/smart-contracts/{address}`` with proxy
    resolution. Falls back to the v1 ``getabi`` endpoint for
    instances or contracts where v2 doesn't have what we need."""

    def __init__(self, instances=None, timeout: float = 20.0,
                 transport=None, max_proxy_depth: int = 4,
                 storage_reader=None):
        self.instances = instances if instances is not None else BLOCKSCOUT_INSTANCES
        self.timeout = timeout
        # Optional callable (chain_id, address, slot_hex) -> 0x-hex value,
        # used to read proxy implementation slots from the chain when
        # Blockscout v2 is down. Wired by the host (so the source can stay
        # network-free in tests). None → no RPC proxy resolution.
        self.storage_reader = storage_reader
        # Same injection seam used elsewhere — callable (url, timeout)
        # → bytes so tests can return canned JSON without HTTP.
        self._transport = transport or _urllib_transport
        # Hard guard against pathological proxy chains (proxy→proxy→…)
        # or APIs that loop back. 4 levels covers every real pattern.
        self.max_proxy_depth = max_proxy_depth

    def supports(self, chain_id: int) -> bool:
        return chain_id in self.instances

    def fetch(self, chain_id: int, address: str) -> Union[Abi, bool]:
        if chain_id not in self.instances:
            raise AbiSourceError(
                f"No Blockscout instance configured for chain {chain_id}"
            )
        return self._fetch_recursive(chain_id, address, depth=0, seen=set())

    def _fetch_recursive(self, chain_id: int, address: str,
                          depth: int,
                          seen: set[str]) -> Union[Abi, bool]:
        addr_l = address.lower()
        if addr_l in seen or depth >= self.max_proxy_depth:
            return False
        seen.add(addr_l)

        v2 = self._fetch_v2(chain_id, address)
        if v2 is None:
            # v2 unavailable (older Blockscout, or e.g. polygon.blockscout
            # .com 500ing). v1 has no proxy info, so detect a proxy from
            # the chain (well-known impl slots) and merge the
            # implementation's ABI — without this, every proxy contract
            # decodes as just its bare proxy stub.
            v1 = self._fetch_v1(chain_id, address)
            return self._merge_rpc_proxy(chain_id, address, v1, depth, seen)
        own_abi = v2.get("own_abi") or []
        impls = v2.get("implementations") or []
        verified = v2.get("is_verified")

        # Recursively resolve any proxy implementations and merge.
        merged: Abi = list(own_abi)
        for impl in impls:
            impl_addr = impl.get("address_hash") or impl.get("address")
            if not impl_addr:
                continue
            impl_abi = self._fetch_recursive(
                chain_id, impl_addr, depth + 1, seen,
            )
            if isinstance(impl_abi, list):
                merged.extend(impl_abi)

        if not merged:
            # Nothing verified at this address, and no implementations
            # contributed either. Distinguish "v2 was OK but contract
            # is unverified" from transient failure: v2 returning a
            # well-formed payload with is_verified=False is reliable.
            return False if verified is False else False
        return _dedup_by_selector(merged)

    # --- chain-native proxy resolution (v2-unavailable fallback) ---------

    def set_storage_reader(self, reader) -> None:
        self.storage_reader = reader

    def _read_proxy_impl(self, chain_id: int, address: str) -> Optional[str]:
        return _impl_from_storage(self.storage_reader, chain_id, address)

    def _merge_rpc_proxy(self, chain_id: int, address: str, own,
                         depth: int, seen: set[str]) -> Union[Abi, bool]:
        """If ``address`` is a proxy (per the chain's impl slots), fetch +
        merge the implementation's ABI onto ``own`` (the proxy's own ABI,
        which may be ``False`` for an unverified proxy)."""
        if depth >= self.max_proxy_depth:
            return own
        impl = self._read_proxy_impl(chain_id, address)
        if not impl or impl.lower() in seen:
            return own
        impl_abi = self._fetch_recursive(chain_id, impl, depth + 1, seen)
        merged: Abi = list(own) if isinstance(own, list) else []
        if isinstance(impl_abi, list):
            merged.extend(impl_abi)
        if not merged:
            return own
        return _dedup_by_selector(merged)

    # --- HTTP layers -----------------------------------------------------

    def _fetch_v2(self, chain_id: int, address: str) -> Optional[dict]:
        """Returns {"own_abi": [...], "implementations": [...],
        "is_verified": bool} or None when v2 isn't usable."""
        base = self.instances[chain_id]
        url = (
            f"{base.rstrip('/')}/api/v2/smart-contracts/"
            f"{urllib.parse.quote(address)}"
        )
        try:
            raw = self._transport(url, self.timeout)
        except Exception as e:
            log.warning("v2 smart-contracts fetch failed: %s", e)
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        # Blockscout returns 404 / error payloads as dicts too —
        # detect them by the absence of the verified flag.
        if "is_verified" not in data:
            return None
        return {
            "own_abi": data.get("abi") if isinstance(data.get("abi"), list) else [],
            "implementations": data.get("implementations") or [],
            "is_verified": data.get("is_verified"),
        }

    def _fetch_v1(self, chain_id: int, address: str) -> Union[Abi, bool]:
        """Fallback for instances that don't serve the v2 endpoint."""
        base = self.instances[chain_id]
        url = (
            f"{base.rstrip('/')}/api?module=contract&action=getabi"
            f"&address={urllib.parse.quote(address)}"
        )
        try:
            raw = self._transport(url, self.timeout)
        except Exception as e:
            raise AbiSourceError(f"v1 getabi fetch failed: {e}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise AbiSourceError("v1 getabi payload was not JSON")
        if data.get("status") != "1":
            msg = (data.get("result") or data.get("message") or "").lower()
            if "not verified" in msg or "no abi" in msg:
                return False
            raise AbiSourceError(
                data.get("message") or "blockscout abi fetch error"
            )
        abi_str = data.get("result")
        if not abi_str or not isinstance(abi_str, str):
            return False
        try:
            abi = json.loads(abi_str)
        except json.JSONDecodeError:
            raise AbiSourceError("ABI payload was not valid JSON")
        return abi if isinstance(abi, list) else False


class EtherscanV2AbiSource:
    """ABI via the Etherscan v2 multichain ``getabi`` endpoint — reliable
    where a Blockscout instance is flaky (notably polygon.blockscout.com,
    which 500s). Etherscan ``getabi`` returns a proxy's *own* ABI, so we
    do the same chain-native proxy resolution as Blockscout (impl slots
    read via ``storage_reader``) and merge the implementation's ABI."""

    def __init__(self, get_api_key, timeout: float = 20.0, transport=None,
                 storage_reader=None, max_proxy_depth: int = 4,
                 supported_chains=None):
        self._get_api_key = get_api_key
        self.timeout = timeout
        self._transport = transport or _urllib_transport
        self.storage_reader = storage_reader
        self.max_proxy_depth = max_proxy_depth
        self._supported = (
            supported_chains if supported_chains is not None
            else ETHERSCAN_V2_CHAINS
        )

    def set_storage_reader(self, reader) -> None:
        self.storage_reader = reader

    def supports(self, chain_id: int) -> bool:
        return chain_id in self._supported and bool(self._get_api_key())

    def fetch(self, chain_id: int, address: str) -> Union[Abi, bool]:
        return self._fetch_recursive(chain_id, address, depth=0, seen=set())

    def _fetch_recursive(self, chain_id, address, depth, seen):
        addr_l = address.lower()
        if addr_l in seen or depth >= self.max_proxy_depth:
            return False
        seen.add(addr_l)
        own = self._getabi(chain_id, address)
        # Resolve + merge a proxy implementation from the chain.
        impl = _impl_from_storage(self.storage_reader, chain_id, address)
        if impl and impl.lower() not in seen:
            impl_abi = self._fetch_recursive(chain_id, impl, depth + 1, seen)
            merged: Abi = list(own) if isinstance(own, list) else []
            if isinstance(impl_abi, list):
                merged.extend(impl_abi)
            if merged:
                return _dedup_by_selector(merged)
        return own

    def _getabi(self, chain_id: int, address: str) -> Union[Abi, bool]:
        key = self._get_api_key()
        if not key:
            raise AbiSourceError("No Etherscan API key configured")
        params = urllib.parse.urlencode([
            ("chainid", str(chain_id)),
            ("module", "contract"),
            ("action", "getabi"),
            ("address", address),
            ("apikey", key),
        ])
        try:
            raw = self._transport(f"{ETHERSCAN_V2_BASE}?{params}", self.timeout)
            data = json.loads(raw)
        except Exception as e:
            raise AbiSourceError(f"etherscan getabi failed: {e}")
        if data.get("status") != "1":
            msg = (data.get("result") or data.get("message") or "").lower()
            if "not verified" in msg or "no abi" in msg:
                return False
            raise AbiSourceError(data.get("result") or "etherscan abi error")
        abi_str = data.get("result")
        if not abi_str or not isinstance(abi_str, str):
            return False
        try:
            abi = json.loads(abi_str)
        except json.JSONDecodeError:
            raise AbiSourceError("etherscan ABI payload was not valid JSON")
        return abi if isinstance(abi, list) else False


class RoutedAbiSource:
    """Prefer ``primary`` (Etherscan v2) where it supports the chain, fall
    back to ``secondary`` (Blockscout) on an unusable result or an error —
    so a flaky explorer on one side doesn't kill decoding."""

    def __init__(self, primary, secondary):
        self._primary = primary
        self._secondary = secondary

    def set_storage_reader(self, reader) -> None:
        for s in (self._primary, self._secondary):
            if hasattr(s, "set_storage_reader"):
                s.set_storage_reader(reader)

    def supports(self, chain_id: int) -> bool:
        return self._primary.supports(chain_id) or self._secondary.supports(chain_id)

    def fetch(self, chain_id: int, address: str) -> Union[Abi, bool]:
        secondary_ok = self._secondary.supports(chain_id)
        if self._primary.supports(chain_id):
            try:
                res = self._primary.fetch(chain_id, address)
                if isinstance(res, list) or not secondary_ok:
                    return res
            except Exception as e:
                if not secondary_ok:
                    raise
                log.warning("primary ABI source failed (%s); trying fallback", e)
        if secondary_ok:
            return self._secondary.fetch(chain_id, address)
        raise AbiSourceError(f"No ABI source supports chain {chain_id}")


def _dedup_by_selector(abi: Abi) -> Abi:
    """Drop function entries whose 4-byte selector we've already
    seen. Order matters: the proxy's own ABI comes first, so its
    entries win over implementation overrides (proxies often expose
    admin methods the implementation has differently). For non-
    functions (constructor, fallback, receive, event, error) we just
    pass everything through — they don't collide by selector."""
    seen: set[str] = set()
    out: Abi = []
    for entry in abi:
        if entry.get("type") != "function":
            out.append(entry)
            continue
        sig = _function_signature(entry)
        if sig and sig in seen:
            continue
        if sig:
            seen.add(sig)
        out.append(entry)
    return out


def _function_signature(entry: dict) -> Optional[str]:
    """Canonical "name(t1,t2,…)" — what keccak256 of which becomes
    the 4-byte selector. We don't actually need the bytes, just a
    uniqueness key."""
    name = entry.get("name")
    if not name:
        return None
    inputs = entry.get("inputs") or []
    types = ",".join(inp.get("type", "") for inp in inputs)
    return f"{name}({types})"


def _urllib_transport(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --- 4-byte signature fallback (Etherscan-style) -----------------------------
# When no contract ABI decodes a call — an unverified contract, or a proxy
# whose implementation isn't verified — recover the function from its 4-byte
# selector via 4byte.directory, exactly as block explorers do. Parameter
# names are unavailable (the signature is name + types only), so args are
# positional arg0/arg1/…

_FOURBYTE_URL = "https://www.4byte.directory/api/v1/signatures/?hex_signature="


def _split_top_level(s: str) -> list[str]:
    """Split a comma list at the top level only — parens (tuple types)
    keep their commas. ``address,(uint256,bytes),uint8`` → 3 parts."""
    out: list[str] = []
    depth = 0
    cur = ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [t.strip() for t in out if t.strip()]


def _sig_type_to_input(name: str, type_str: str) -> dict:
    """One signature type string → an ABI input spec, recursing into
    tuple ``(...)`` components (with array suffixes preserved)."""
    type_str = type_str.strip()
    base, suffix = type_str, ""
    while base.endswith("]"):
        i = base.rindex("[")
        suffix = base[i:] + suffix
        base = base[:i]
    if base.startswith("(") and base.endswith(")"):
        comps = _split_top_level(base[1:-1])
        return {
            "name": name, "type": "tuple" + suffix,
            "components": [_sig_type_to_input(f"f{i}", c)
                           for i, c in enumerate(comps)],
        }
    return {"name": name, "type": type_str}


def decode_with_signature(signature: str,
                          input_data: str) -> Optional[dict]:
    """Decode ``input_data`` from a ``name(t1,t2,…)`` signature string,
    reusing ``decode_call`` so the tree shape is identical (args named
    positionally). Adds ``via_signature: True``. None if it doesn't fit."""
    m = re.match(r"^([A-Za-z0-9_$]+)\s*\((.*)\)\s*$", signature.strip())
    if not m:
        return None
    name, types_str = m.group(1), m.group(2)
    inputs = [_sig_type_to_input(f"arg{i}", t)
              for i, t in enumerate(_split_top_level(types_str))]
    frag = [{"type": "function", "name": name,
             "stateMutability": "nonpayable", "inputs": inputs, "outputs": []}]
    decoded = decode_call(frag, input_data)
    if decoded is not None:
        decoded["via_signature"] = True
    return decoded


def fetch_signatures(selector: str, *, transport=None,
                     timeout: float = 8.0) -> list[str]:
    """Candidate function signatures for a 4-byte selector from
    4byte.directory, oldest first (the canonical one usually leads).
    [] on miss/error — multiple results means hash collisions."""
    sel = (selector if selector.startswith("0x") else "0x" + selector)[:10]
    try:
        raw = (transport or _urllib_transport)(_FOURBYTE_URL + sel, timeout)
        data = json.loads(raw)
    except Exception as e:
        log.warning("4byte lookup failed for %s: %s", sel, e)
        return []
    return [r["text_signature"] for r in (data.get("results") or [])
            if isinstance(r, dict) and r.get("text_signature")]


def decode_via_4byte(input_data: str, *, transport=None) -> Optional[dict]:
    """Last-resort decode when no ABI matched: look the selector up in
    the 4-byte database and try each candidate signature, returning the
    first that decodes cleanly."""
    if not input_data or len(input_data) < 10:
        return None
    for sig in fetch_signatures(input_data[:10], transport=transport):
        decoded = decode_with_signature(sig, input_data)
        if decoded is not None:
            return decoded
    return None


def decode_call(abi: Optional[Abi], input_data: str,
                address: Optional[str] = None) -> Optional[dict]:
    """Decode ``input_data`` against ``abi``. Returns a tree like

        {"function": "register",
         "args": [
            {"name": "registration", "type": "tuple", "children": [
                {"name": "label",   "type": "string", "value": "qeth"},
                {"name": "secret",  "type": "bytes32", "value": "0x99…"},
                ...
            ]},
         ]}

    or ``None`` when there's no ABI, the calldata is empty, or
    decoding fails. Leaf nodes have ``value`` (stringified); tuple
    nodes have ``children``. The UI walks the tree to render
    indented, type-annotated output."""
    if not abi or not input_data or input_data in ("0x", "0X"):
        return None
    try:
        from web3 import Web3
        w3 = Web3()
        addr = Web3.to_checksum_address(address) if address else None
        contract = w3.eth.contract(address=addr, abi=abi)
        func, args = contract.decode_function_input(input_data)
    except Exception:
        return None
    inputs = (getattr(func, "abi", None) or {}).get("inputs", []) or []
    args_list: list[dict] = []
    for inp in inputs:
        name = inp.get("name", "")
        # web3.py keys args by parameter name. Anonymous inputs are
        # rare but possible — fall back to positional index.
        if name in args:
            value = args[name]
        else:
            try:
                value = list(args.values())[len(args_list)]
            except IndexError:
                value = None
        args_list.append(_describe(value, inp))
    return {
        "function": getattr(func, "fn_name", None) or str(func),
        "args": args_list,
    }


def _describe(value, inp: dict) -> dict:
    """Build one tree node pairing ``value`` with the ABI input
    spec ``inp``. Recurses into struct components so each inner
    field carries its Solidity type alongside its decoded value, and
    into arrays so each element does too — the UI uses the element
    list to render long arrays Python-style instead of one wide line.
    Elements get empty ``name`` (positional in the array)."""
    type_ = inp.get("type", "")
    name = inp.get("name", "")
    components = inp.get("components") or []
    if type_ == "tuple" and components:
        children = []
        for comp in components:
            comp_name = comp.get("name", "")
            child_value = None
            if value is not None:
                try:
                    child_value = value[comp_name]
                except (KeyError, TypeError):
                    child_value = None
            children.append(_describe(child_value, comp))
        return {"name": name, "type": type_, "children": children}
    if type_.endswith("]"):
        # ``address[]`` → element type ``address``;
        # ``uint256[][2]`` → element type ``uint256[]``.
        elem_type = type_[:type_.rfind("[")]
        elem_inp = {"name": "", "type": elem_type}
        # Arrays of tuples share the parent's ``components`` ABI for
        # each element — without forwarding it the element-level
        # _describe would see ``type=tuple`` with no components and
        # fall through to the leaf branch.
        if components:
            elem_inp["components"] = components
        children = []
        if isinstance(value, (list, tuple)):
            for elem in value:
                children.append(_describe(elem, elem_inp))
        return {"name": name, "type": type_, "children": children}
    # Primitives — single-line.
    return {"name": name, "type": type_, "value": _stringify(value, type_)}


def _stringify(value, type_hint: Optional[str] = None) -> str:
    """Coerce a decoded value to a string. web3.py hands back:

      - bytes / bytearray for bytesN and bytes      → 0x<hex>
      - int for uintN/intN, bool for bool, str for  → str(value)
        address and string
      - tuple/list for arrays                       → [v, v, …]
      - AttributeDict (a dict subclass) for structs → {k: v, k: v}

    ``type_hint`` carries the Solidity type so we can format
    type-distinctive forms — currently just quoting ``string``
    values so they don't look like bare identifiers. Recursive
    calls for array elements strip the ``[N]`` / ``[]`` suffix so
    each element gets the element-type hint."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if isinstance(value, (list, tuple)):
        # "string[]" → element hint "string"; "uint256[3]" → "uint256".
        inner_hint = None
        if type_hint and type_hint.endswith("]"):
            i = type_hint.rfind("[")
            if i > 0:
                inner_hint = type_hint[:i]
        return "[" + ", ".join(_stringify(v, inner_hint) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(f"{k}: {_stringify(v)}" for k, v in value.items())
            + "}"
        )
    if type_hint == "string" and isinstance(value, str):
        return '"' + value + '"'
    return str(value)


# --- event-log decoding ---------------------------------------------------
#
# Decode a receipt log into the same {name, type, value} arg shape that
# decode_call produces, so the UI can render events with the very same
# Python-style renderer. The common ERC-20/721 Transfer/Approval events
# decode from their well-known signatures with no ABI; anything else
# decodes only when the emitting contract's ABI is supplied.


def _event_topic0(signature: str) -> str:
    from eth_utils import keccak
    return "0x" + keccak(text=signature).hex()


_TRANSFER_TOPIC = _event_topic0("Transfer(address,address,uint256)")
_APPROVAL_TOPIC = _event_topic0("Approval(address,address,uint256)")
_APPROVAL_FOR_ALL_TOPIC = _event_topic0("ApprovalForAll(address,address,bool)")

# Events the default (filtered) view recognises without an ABI.
KNOWN_EVENT_NAMES = frozenset({"Transfer", "Approval", "ApprovalForAll"})


def _norm_topic(t) -> str:
    if hasattr(t, "hex"):
        h = t.hex()
        return (h if h.startswith("0x") else "0x" + h).lower()
    return str(t).lower()


def _addr_from_topic(t: str) -> str:
    from eth_utils import to_checksum_address
    return to_checksum_address("0x" + t[-40:])


def _u256(data: str) -> int:
    d = data[2:] if data.startswith("0x") else data
    return int(d[:64], 16) if d else 0


def _ev(address, name, args) -> dict:
    return {
        "contract": address,
        "event": name,
        "args": [{"name": n, "type": t, "value": v} for n, t, v in args],
    }


def _decode_known_event(address, topics: list, data: str) -> Optional[dict]:
    t0 = topics[0]
    if t0 == _TRANSFER_TOPIC and len(topics) == 3:        # ERC-20
        return _ev(address, "Transfer", [
            ("from", "address", _addr_from_topic(topics[1])),
            ("to", "address", _addr_from_topic(topics[2])),
            ("value", "uint256", str(_u256(data)))])
    if t0 == _TRANSFER_TOPIC and len(topics) == 4:        # ERC-721
        return _ev(address, "Transfer", [
            ("from", "address", _addr_from_topic(topics[1])),
            ("to", "address", _addr_from_topic(topics[2])),
            ("tokenId", "uint256", str(int(topics[3], 16)))])
    if t0 == _APPROVAL_TOPIC and len(topics) == 3:        # ERC-20
        return _ev(address, "Approval", [
            ("owner", "address", _addr_from_topic(topics[1])),
            ("spender", "address", _addr_from_topic(topics[2])),
            ("value", "uint256", str(_u256(data)))])
    if t0 == _APPROVAL_TOPIC and len(topics) == 4:        # ERC-721
        return _ev(address, "Approval", [
            ("owner", "address", _addr_from_topic(topics[1])),
            ("approved", "address", _addr_from_topic(topics[2])),
            ("tokenId", "uint256", str(int(topics[3], 16)))])
    if t0 == _APPROVAL_FOR_ALL_TOPIC and len(topics) == 3:
        return _ev(address, "ApprovalForAll", [
            ("owner", "address", _addr_from_topic(topics[1])),
            ("operator", "address", _addr_from_topic(topics[2])),
            ("approved", "bool", str(bool(_u256(data))))])
    return None


def _decode_event_with_abi(address, log, abi: Abi) -> Optional[dict]:
    try:
        from web3 import Web3
        from hexbytes import HexBytes
        w3 = Web3()
        events_abi = [e for e in abi if e.get("type") == "event"]
        if not events_abi:
            return None
        contract = w3.eth.contract(abi=events_abi)
        entry = {
            "address": address,
            "topics": [HexBytes(t) for t in (log.get("topics") or [])],
            "data": HexBytes(log.get("data") or "0x"),
            "logIndex": 0, "transactionIndex": 0,
            "transactionHash": HexBytes(b""), "blockHash": HexBytes(b""),
            "blockNumber": 0,
        }
        for ev in contract.events:  # type: ignore[attr-defined]  # web3 ContractEvents iteration isn't typed
            try:
                decoded = ev().process_log(entry)
            except Exception:
                continue
            name = decoded["event"]
            ev_abi = next(
                (e for e in events_abi if e.get("name") == name), None
            )
            inputs = (ev_abi or {}).get("inputs", []) or []
            args = [
                _describe(decoded["args"].get(inp.get("name")), inp)
                for inp in inputs
            ]
            return {"contract": address, "event": name, "args": args}
    except Exception:
        return None
    return None


def decode_event(log, abi: Optional[Abi] = None) -> Optional[dict]:
    """Decode one receipt ``log`` into

        {"contract": "0x…", "event": "Transfer",
         "args": [{"name": "from", "type": "address", "value": "0x…"}, …]}

    Transfer / Approval / ApprovalForAll decode from their well-known
    signatures with no ABI. Any other event decodes only when ``abi``
    (the emitting contract's ABI) is supplied. Returns ``None`` when it
    can't be decoded (caller can fall back to a raw display)."""
    topics = [_norm_topic(t) for t in (log.get("topics") or [])]
    if not topics:
        return None
    data = log.get("data") or "0x"
    if hasattr(data, "hex"):
        data = "0x" + data.hex()
    address = log.get("address")
    known = _decode_known_event(address, topics, data)
    if known is not None:
        return known
    if abi:
        return _decode_event_with_abi(address, log, abi)
    return None
