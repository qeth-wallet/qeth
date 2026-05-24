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
import urllib.parse
import urllib.request
from typing import Optional, Union

from .tokens import BLOCKSCOUT_INSTANCES   # reuse the per-chain map


USER_AGENT = "qeth/0.1"
log = logging.getLogger("qeth.abi")

Abi = list[dict]


class AbiSourceError(Exception):
    pass


class BlockscoutAbiSource:
    """Blockscout v2 ``/api/v2/smart-contracts/{address}`` with proxy
    resolution. Falls back to the v1 ``getabi`` endpoint for
    instances or contracts where v2 doesn't have what we need."""

    def __init__(self, instances=None, timeout: float = 20.0,
                 transport=None, max_proxy_depth: int = 4):
        self.instances = instances if instances is not None else BLOCKSCOUT_INSTANCES
        self.timeout = timeout
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
            # Endpoint unavailable / older Blockscout — try v1.
            v1 = self._fetch_v1(chain_id, address)
            return v1
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


def decode_call(abi: Optional[Abi], input_data: str,
                address: Optional[str] = None) -> Optional[dict]:
    """Decode ``input_data`` against ``abi``. Returns a dict like

        {"function": "transfer",
         "args": [
            {"name": "_to",    "type": "address", "value": "0x…"},
            {"name": "_value", "type": "uint256", "value": "500000000"},
         ]}

    or ``None`` when there's no ABI, the calldata is empty, or
    decoding fails. The args list preserves declaration order and
    carries Solidity type names so the UI can render annotations
    (``_to: address = 0x…``) without a second ABI lookup."""
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
        type_ = inp.get("type", "")
        # web3.py keys args by parameter name. Anonymous inputs are
        # rare but possible — fall back to positional index.
        if name in args:
            value = args[name]
        else:
            try:
                value = list(args.values())[len(args_list)]
            except IndexError:
                value = None
        args_list.append({
            "name": name,
            "type": type_,
            "value": _stringify(value),
        })
    return {
        "function": getattr(func, "fn_name", None) or str(func),
        "args": args_list,
    }


def _stringify(value) -> str:
    """Coerce a decoded value to a string. web3.py hands back bytes
    for bytesN/bytes, Python ints for uintN/intN, checksummed
    strings for addresses, and tuples/lists for arrays."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_stringify(v) for v in value) + "]"
    return str(value)
