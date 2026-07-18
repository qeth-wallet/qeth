"""On-chain USD pricing for vault / LP tokens that market APIs don't cover.

When DefiLlama has no quote for a token — typically a vault share (ERC-4626 or
Yearn-style) or an LP token the user got from their own transactions — we price
it directly from chain state, recursing to a market price for the underlying:

- **ERC-4626 / Yearn share**: ``share_price × price(underlying)``, where the
  share price comes from ``convertToAssets`` / ``pricePerShare`` /
  ``getPricePerFullShare`` / ``exchangeRate`` etc.
- **Curve LP**: ``TVL / totalSupply`` where TVL sums each pooled coin's balance
  valued at that coin's price (registry-driven, no log scans).
- **Uniswap-V2-style LP**: ``TVL / totalSupply`` from ``getReserves``.

Every read is a plain ``eth_call`` at ``latest`` (batched through
``EthClient.multicall``) — no archive node, no ``eth_getLogs``. The recipes are
reimplemented from ypricemagic; the underlying/coin prices bottom out at the
primary source (DefiLlama), which covers WBTC / WETH / stables.

``ChainedPriceSource`` wires this behind the primary: it fetches from the
primary, then asks this module for whatever came back unpriced.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from eth_abi import decode as abi_decode

from ..chains import Chain
from .base import Price, PriceSource

log = logging.getLogger("qeth.pricing.onchain")

# --- function selectors (verified: keccak(sig)[:4]) ------------------------
_SEL_PRICE_PER_SHARE = bytes.fromhex("99530b06")           # pricePerShare()
_SEL_GET_PRICE_PER_SHARE = bytes.fromhex("3d68175c")       # getPricePerShare()
_SEL_GET_PRICE_PER_FULL_SHARE = bytes.fromhex("77c7b8fc")  # getPricePerFullShare()
_SEL_GET_SHARES_TO_UNDERLYING = bytes.fromhex("e613deb2")  # getSharesToUnderlying(uint256)
_SEL_EXCHANGE_RATE = bytes.fromhex("3ba0b9a9")             # exchangeRate()
_SEL_CONVERT_TO_ASSETS = bytes.fromhex("07a2d13a")         # convertToAssets(uint256)
_SEL_ASSET = bytes.fromhex("38d52e0f")                     # asset()
_SEL_ASSET_TOKEN = bytes.fromhex("d7062005")               # ASSET_TOKEN() — Yield Basis
_SEL_TOKEN = bytes.fromhex("fc0c546a")                     # token()
_SEL_UNDERLYING = bytes.fromhex("6f307dc3")                # underlying()
_SEL_WANT = bytes.fromhex("1f1fcd51")                      # want()
_SEL_INPUT = bytes.fromhex("eaed3f4f")                     # input()
_SEL_NATIVE = bytes.fromhex("11b0b42d")                    # native()
_SEL_TOKEN0 = bytes.fromhex("0dfe1681")                    # token0()
_SEL_TOKEN1 = bytes.fromhex("d21220a7")                    # token1()
_SEL_GET_RESERVES = bytes.fromhex("0902f1ac")              # getReserves()
_SEL_TOTAL_SUPPLY = bytes.fromhex("18160ddd")              # totalSupply()
_SEL_DECIMALS = bytes.fromhex("313ce567")                  # decimals()
_SEL_MINTER = bytes.fromhex("07546172")                    # minter()
_SEL_GET_REGISTRY = bytes.fromhex("a262904b")              # get_registry()
_SEL_GET_POOL_FROM_LP = bytes.fromhex("bdf475c3")          # get_pool_from_lp_token(address)
_SEL_GET_COINS = bytes.fromhex("9ac90d3d")                 # get_coins(address)
_SEL_GET_BALANCES = bytes.fromhex("92e3cc2d")              # get_balances(address)
_SEL_COINS_U256 = bytes.fromhex("c6610657")                # coins(uint256)
_SEL_BALANCES_U256 = bytes.fromhex("4903b0d1")             # balances(uint256)
_SEL_COINS_I128 = bytes.fromhex("23746eb8")                # coins(int128)
_SEL_BALANCES_I128 = bytes.fromhex("065a80d8")             # balances(int128)

# Curve's on-chain registry directory — same address on every chain.
CURVE_ADDRESS_PROVIDER = "0x0000000022D53366457F9d5E68Ec105046FC4383"
# Curve uses this sentinel for "the native coin" in a pool's coin list.
_ETH_PLACEHOLDER = "0x" + "ee" * 20
_ZERO_ADDR = "0x" + "00" * 20
# Underlying/coin candidates — the ``()(address)`` getters, ypricemagic order.
_UNDERLYING_SELECTORS = (
    ("token", _SEL_TOKEN),
    ("underlying", _SEL_UNDERLYING),
    ("want", _SEL_WANT),
    ("asset", _SEL_ASSET),
    ("input", _SEL_INPUT),
    ("native", _SEL_NATIVE),
)
# No-arg share-price getters, richest-signal first (the arg'd 4626 form is
# handled separately). ``getPricePerFullShare`` is the 1e18-scaled Yearn-v1
# convention; the rest scale to the underlying's decimals.
_NOARG_SHARE_SELECTORS = (
    ("getPricePerFullShare", _SEL_GET_PRICE_PER_FULL_SHARE),
    ("pricePerShare", _SEL_PRICE_PER_SHARE),
    ("getPricePerShare", _SEL_GET_PRICE_PER_SHARE),
    ("exchangeRate", _SEL_EXCHANGE_RATE),
)


@dataclass(frozen=True)
class VaultStructure:
    """A share token priced as ``share_price × price(underlying)``."""
    underlying: str            # lower-case ERC-20 address
    method: str                # which share-price call to read
    vault_decimals: int
    underlying_decimals: int


@dataclass(frozen=True)
class CurveLPStructure:
    """A Curve LP token priced as ``TVL / totalSupply``."""
    pool: str                  # lower; == the LP for factory pools
    coins: tuple[str, ...]     # lower; ``_ETH_PLACEHOLDER`` for the native coin
    coin_decimals: tuple[int, ...]
    lp_decimals: int


@dataclass(frozen=True)
class UniV2Structure:
    """A Uniswap-V2-style LP token priced as ``TVL / totalSupply``."""
    token0: str
    token1: str
    dec0: int
    dec1: int
    lp_decimals: int


Structure = VaultStructure | CurveLPStructure | UniV2Structure
PriceLookup = Callable[[Iterable[str], bool], dict[str, Price]]


@dataclass(frozen=True)
class IconMeta:
    """The immutable, icon-relevant shape of a vault/LP token — separate from
    its (mutable) USD value, so the UI can give it a derived icon even when the
    primary source already prices it (and the on-chain valuer never runs)."""
    underlying: str | None = None                 # single-underlying vault → asset
    pool_tokens: tuple[str, ...] | None = None    # LP → pooled coin addresses


_MISSING = object()   # memo sentinel: address never probed


# --- per-fetch dynamic reads (mutable inputs; re-read every call) -----------

@dataclass
class _VaultDyn:
    share_raw: int


@dataclass
class _CurveDyn:
    coins: tuple[str, ...]
    balances: tuple[int, ...]
    supply: int


@dataclass
class _UniV2Dyn:
    r0: int
    r1: int
    supply: int


Dynamic = _VaultDyn | _CurveDyn | _UniV2Dyn


# --- calldata + return decoders --------------------------------------------

def _is_addr(a: str) -> bool:
    return isinstance(a, str) and a.startswith("0x") and len(a) == 42


def _addr_arg(addr: str) -> bytes:
    """ABI-encode an address argument (left-padded to 32 bytes)."""
    h = addr[2:] if addr.startswith("0x") else addr
    return bytes(12) + bytes.fromhex(h.lower())


def _u256(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _dec_uint256(raw: bytes) -> int | None:
    return int.from_bytes(raw[:32], "big") if raw and len(raw) >= 32 else None


def _dec_address(raw: bytes) -> str | None:
    """Strict address decode: a 32-byte word, top-12 zero, nonzero. A no-code
    target returns success + empty data, which yields None here."""
    if not raw or len(raw) < 32:
        return None
    word = raw[:32]
    if word[:12] != bytes(12):
        return None
    body = word[12:32]
    if body == bytes(20):
        return None
    return "0x" + body.hex()


def _dec_reserves(raw: bytes) -> tuple[int, int] | None:
    if not raw or len(raw) < 96:
        return None
    return (int.from_bytes(raw[0:32], "big"), int.from_bytes(raw[32:64], "big"))


def _dec_addr_array(raw: bytes) -> tuple[str, ...] | None:
    """Curve ``get_coins`` returns ``address[8]`` (fixed) on the main registry,
    ``address[]`` (dynamic) on some factories — try both, trim zero padding."""
    for typ in ("address[8]", "address[]"):
        try:
            vals = tuple(str(v).lower() for v in abi_decode([typ], raw)[0])
        except Exception:
            continue
        return _trim_coins(vals)
    return None


def _dec_uint_array(raw: bytes) -> tuple[int, ...] | None:
    for typ in ("uint256[8]", "uint256[]"):
        try:
            return tuple(int(v) for v in abi_decode([typ], raw)[0])
        except Exception:
            continue
    return None


def _trim_coins(coins: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for c in coins:
        if c == _ZERO_ADDR:
            break
        out.append(c)
    return tuple(out)


def _ok(pending: Any) -> Any:
    """A pending's decoded value, or None if it reverted / decoded to None.
    Returns Any — the value's type depends on the call's decoder."""
    if pending is None or not pending.success or pending.value is None:
        return None
    return pending.value


class OnChainVaultPrices:
    """Prices vault/LP tokens by reading chain state. Structure resolution is
    immutable (memoized per session, positive AND negative); dynamic reads are
    re-fetched each call so prices stay fresh."""

    MAX_DEPTH = 4          # recursion levels (vault → LP → coins → …)
    MAX_CANDIDATES = 50    # tokens probed per fetch, bounding RPC cost
    ABSURD_SHARE_PRICE = Decimal(10) ** 6

    def __init__(self, client_factory: Callable[[Chain], object] | None = None):
        self._client_factory = client_factory or self._default_client
        self._memo: dict[tuple[int, str], Structure | None] = {}
        self._registry: dict[int, str | None] = {}

    @staticmethod
    def _default_client(chain: Chain) -> object:
        from ..chain import EthClient
        return EthClient(chain)

    # -- public -----------------------------------------------------------

    def price(self, chain: Chain, tokens: Iterable[str],
              base_lookup: PriceLookup) -> dict[str, Price]:
        cid = chain.chain_id
        want = list(dict.fromkeys(
            a.lower() for a in tokens
            if _is_addr(a)
            and self._memo.get((cid, a.lower()), _MISSING) is not None
        ))[:self.MAX_CANDIDATES]
        if not want:
            return {}   # all requested are known non-vault/LP → zero calls

        client = self._client_factory(chain)
        self._ensure_registry(client, cid)

        resolved: dict[str, Price] = {}
        structures: dict[str, Structure] = {}
        dynamics: dict[str, Dynamic] = {}
        frontier = list(want)
        for _ in range(self.MAX_DEPTH):
            if not frontier:
                break
            to_probe = [a for a in frontier
                        if self._memo.get((cid, a), _MISSING) is _MISSING]
            if to_probe:
                for a, st in self._probe(client, cid, to_probe).items():
                    self._memo[(cid, a)] = st

            # Finalize structures + read dynamics for frontier rows we haven't
            # read yet this call (memo may hold a finalized structure from a
            # prior call, but the mutable inputs still need re-reading).
            prelim = {a: self._memo[(cid, a)] for a in frontier
                      if self._memo.get((cid, a)) is not None and a not in dynamics}
            if prelim:
                fin, dyn = self._read_dynamics(
                    client, self._registry.get(cid), prelim)  # type: ignore[arg-type]
                for a, st in fin.items():
                    self._memo[(cid, a)] = st
                    if st is not None:
                        structures[a] = st
                dynamics.update(dyn)

            erc20_deps: set[str] = set()
            needs_native = False
            for a, st in structures.items():
                if a in resolved:
                    continue
                d, nat = _deps_of(st)
                erc20_deps |= d
                needs_native = needs_native or nat
            need_base = [d for d in erc20_deps if d not in resolved]
            if need_base or needs_native:
                for k, v in base_lookup(need_base, needs_native).items():
                    resolved.setdefault(k, v)

            _compute_fixpoint(structures, dynamics, resolved)
            frontier = [d for d in need_base
                        if d not in resolved and _is_addr(d)]

        out: dict[str, Price] = {}
        for a in want:
            p = resolved.get(a)
            if p is None:
                continue
            if p.underlying:
                # For the icon, report the TERMINAL recognizable asset: a vault
                # over another vault (a 4626 wrapping yb-WETH wrapping WETH) has
                # no icon for its immediate underlying, so walk down to the first
                # non-vault asset (WETH), whose icon we can actually show.
                term = self._terminal_underlying(cid, p.underlying)
                if term != p.underlying:
                    p = replace(p, underlying=term)
            out[a] = p
        return out

    def _terminal_underlying(self, cid: int, underlying: str) -> str:
        """Follow nested single-underlying vaults down to the first asset that
        isn't itself such a vault — the recognizable base for the vault icon."""
        cur = underlying.lower()
        for _ in range(self.MAX_DEPTH):
            st = self._memo.get((cid, cur))
            if isinstance(st, VaultStructure):
                cur = st.underlying
            else:
                break
        return cur

    def icon_metadata(self, chain: Chain, tokens: Iterable[str]) -> dict[str, IconMeta]:
        """The icon-relevant structure (single underlying, or pooled coins) of
        each vault/LP token, WITHOUT valuing it. Probes each token once and
        memoizes; later calls serve from the memo with no network. This is how a
        vault/LP the primary source already prices still gets its derived icon —
        the on-chain *valuer* would carry the same fields but never runs then."""
        cid = chain.chain_id
        addrs = list(dict.fromkeys(
            a.lower() for a in tokens if _is_addr(a)))[:self.MAX_CANDIDATES]
        unknown = [a for a in addrs
                   if self._memo.get((cid, a), _MISSING) is _MISSING]
        if unknown:
            client = self._client_factory(chain)
            self._ensure_registry(client, cid)
            for a, st in self._probe(client, cid, unknown).items():
                self._memo[(cid, a)] = st
            # A Curve structure only knows its coins after a dynamics read
            # (registry get_coins / per-index fallback); vault + UniV2 metadata
            # is already complete from the probe. Finalize the Curve ones once —
            # the memo then holds the coins for every later call.
            curve_prelim: dict[str, Structure] = {
                a: st for a in unknown
                if isinstance(st := self._memo[(cid, a)], CurveLPStructure)}
            if curve_prelim:
                fin, _dyn = self._read_dynamics(
                    client, self._registry.get(cid), curve_prelim)
                for a, st in fin.items():
                    self._memo[(cid, a)] = st
        out: dict[str, IconMeta] = {}
        for a in addrs:
            m = self._meta_of(cid, self._memo.get((cid, a)))
            if m is not None:
                out[a] = m
        return out

    def _meta_of(self, cid: int, st: object) -> IconMeta | None:
        if isinstance(st, VaultStructure):
            return IconMeta(underlying=self._terminal_underlying(cid, st.underlying))
        if isinstance(st, CurveLPStructure) and st.coins:
            return IconMeta(pool_tokens=st.coins)
        if isinstance(st, UniV2Structure):
            return IconMeta(pool_tokens=(st.token0, st.token1))
        return None

    # -- registry + probing ----------------------------------------------

    def _ensure_registry(self, client, cid: int) -> None:
        if cid in self._registry:
            return
        addr = None
        try:
            with client.multicall() as mc:
                p = mc.add(CURVE_ADDRESS_PROVIDER, _SEL_GET_REGISTRY,
                           decoder=_dec_address)
            addr = _ok(p)
        except Exception as e:
            log.debug("curve registry probe failed on %s: %s", cid, e)
        self._registry[cid] = addr

    def _probe(self, client, cid: int, addrs: list[str]) -> dict[str, Structure | None]:
        """Round 1: classify each address into a preliminary Structure (decimals
        of deps + coins filled in later) or None (not a vault/LP)."""
        registry = self._registry.get(cid)
        pend: dict[str, dict] = {}
        with client.multicall() as mc:
            for a in addrs:
                p: dict = {
                    "decimals": mc.add(a, _SEL_DECIMALS, decoder=_dec_uint256),
                    "supply": mc.add(a, _SEL_TOTAL_SUPPLY, decoder=_dec_uint256),
                    "convert": mc.add(a, _SEL_CONVERT_TO_ASSETS + _u256(10 ** 18),
                                      decoder=_dec_uint256),
                    "shares_arg": mc.add(a, _SEL_GET_SHARES_TO_UNDERLYING + _u256(10 ** 18),
                                         decoder=_dec_uint256),
                    "token0": mc.add(a, _SEL_TOKEN0, decoder=_dec_address),
                    "token1": mc.add(a, _SEL_TOKEN1, decoder=_dec_address),
                    "reserves": mc.add(a, _SEL_GET_RESERVES, decoder=_dec_reserves),
                    "minter": mc.add(a, _SEL_MINTER, decoder=_dec_address),
                    "asset_token": mc.add(a, _SEL_ASSET_TOKEN, decoder=_dec_address),
                }
                for name, sel in _NOARG_SHARE_SELECTORS:
                    p[name] = mc.add(a, sel, decoder=_dec_uint256)
                for name, sel in _UNDERLYING_SELECTORS:
                    p[f"u_{name}"] = mc.add(a, sel, decoder=_dec_address)
                if registry:
                    p["pool"] = mc.add(registry, _SEL_GET_POOL_FROM_LP + _addr_arg(a),
                                       decoder=_dec_address)
                pend[a] = p
        return {a: self._classify(a, pend[a]) for a in addrs}

    def _classify(self, addr: str, p: dict) -> Structure | None:
        vd = _ok(p["decimals"])
        vdec = int(vd) if vd is not None else 18
        underlying = next(
            (_ok(p[f"u_{n}"]) for n, _ in _UNDERLYING_SELECTORS if _ok(p[f"u_{n}"])),
            None)

        # --- vault / share token (most specific) ---
        method: str | None = None
        asset_token = _ok(p.get("asset_token"))
        if _ok(p["convert"]) is not None and _ok(p["u_asset"]):
            method, underlying = "convertToAssets", _ok(p["u_asset"])
        elif _ok(p["pricePerShare"]) is not None and asset_token:
            # Yield Basis leveraged vault: pricePerShare() is 1e18-scaled (the
            # net share value in ASSET_TOKEN terms) regardless of the asset's
            # own decimals — verified across yb-WETH/WBTC/cbBTC/tBTC. Checked
            # before generic pricePerShare (which scales by underlying decimals)
            # and keyed on ASSET_TOKEN(), the getter that tells the two apart.
            method, underlying = "pricePerShare1e18", asset_token
        elif _ok(p["getPricePerFullShare"]) is not None:
            method = "getPricePerFullShare"
        elif _ok(p["pricePerShare"]) is not None:
            method = "pricePerShare"
        elif _ok(p["getPricePerShare"]) is not None:
            method = "getPricePerShare"
        elif _ok(p["shares_arg"]) is not None:
            method = "getSharesToUnderlying"
        elif _ok(p["exchangeRate"]) is not None and underlying:
            method = "exchangeRate"
        if method and underlying:
            return VaultStructure(str(underlying).lower(), method, vdec, 0)

        # --- Curve LP (registry or minter names the pool) ---
        pool = _ok(p.get("pool")) or _ok(p["minter"])
        if pool:
            return CurveLPStructure(str(pool).lower(), (), (), vdec)

        # --- Uniswap-V2-style LP ---
        if (_ok(p["reserves"]) and _ok(p["token0"]) and _ok(p["token1"])
                and _ok(p["supply"]) is not None):
            return UniV2Structure(str(_ok(p["token0"])).lower(),
                                  str(_ok(p["token1"])).lower(), 0, 0, vdec)

        # --- tentative Curve factory pool: LP == pool, confirmed in round 2 by
        # coins() resolving; rejected (→ None) there otherwise. ---
        return CurveLPStructure(addr, (), (), vdec)

    # -- dynamic reads + structure finalization ---------------------------

    def _read_dynamics(
        self, client, registry: str | None, prelim: dict[str, Structure],
    ) -> tuple[dict[str, Structure | None], dict[str, Dynamic]]:
        vaults: dict[str, VaultStructure] = {
            a: s for a, s in prelim.items() if isinstance(s, VaultStructure)}
        curves: dict[str, CurveLPStructure] = {
            a: s for a, s in prelim.items() if isinstance(s, CurveLPStructure)}
        univ2: dict[str, UniV2Structure] = {
            a: s for a, s in prelim.items() if isinstance(s, UniV2Structure)}

        fin: dict[str, Structure | None] = {}
        dyn: dict[str, Dynamic] = {}

        vp: dict[str, dict] = {}
        cp: dict[str, dict] = {}
        up: dict[str, dict] = {}
        with client.multicall() as mc:
            for a, sv in vaults.items():
                vp[a] = {"und_dec": mc.add(sv.underlying, _SEL_DECIMALS,
                                           decoder=_dec_uint256),
                         "share": self._share_read(mc, a, sv)}
            for a, sc in curves.items():
                # get_coins / get_balances are REGISTRY methods (target the
                # registry, pass the pool). No registry → straight to the
                # per-index pool fallback.
                d = {"supply": mc.add(a, _SEL_TOTAL_SUPPLY, decoder=_dec_uint256)}
                if registry:
                    d["coins"] = mc.add(registry, _SEL_GET_COINS + _addr_arg(sc.pool),
                                        decoder=_dec_addr_array)
                    d["bals"] = mc.add(registry, _SEL_GET_BALANCES + _addr_arg(sc.pool),
                                       decoder=_dec_uint_array)
                cp[a] = d
            for a, su in univ2.items():
                up[a] = {
                    "d0": mc.add(su.token0, _SEL_DECIMALS, decoder=_dec_uint256),
                    "d1": mc.add(su.token1, _SEL_DECIMALS, decoder=_dec_uint256),
                    "reserves": mc.add(a, _SEL_GET_RESERVES, decoder=_dec_reserves),
                    "supply": mc.add(a, _SEL_TOTAL_SUPPLY, decoder=_dec_uint256),
                }

        for a, sv in vaults.items():
            ud = _ok(vp[a]["und_dec"])
            share = _ok(vp[a]["share"])
            fin[a] = VaultStructure(sv.underlying, sv.method, sv.vault_decimals,
                                    int(ud) if ud is not None else 18)
            dyn[a] = _VaultDyn(int(share) if share is not None else 0)

        curve_ok: dict[str, tuple[tuple[str, ...], tuple[int, ...], int]] = {}
        fallback: dict[str, CurveLPStructure] = {}
        for a, sc in curves.items():
            coins = _ok(cp[a].get("coins"))
            bals = _ok(cp[a].get("bals"))
            supply = _ok(cp[a]["supply"])
            if coins and bals:
                n = len(coins)
                curve_ok[a] = (tuple(coins), tuple(bals[:n]), int(supply or 0))
            else:
                fallback[a] = sc
        if fallback:
            self._curve_fallback(client, fallback, curve_ok)

        # Coin decimals for every resolved Curve pool, then finalize.
        need_dec = {c for cb in curve_ok.values() for c in cb[0]
                    if c != _ETH_PLACEHOLDER}
        coin_dec: dict[str, int] = {}
        if need_dec:
            with client.multicall() as mc:
                dp = {c: mc.add(c, _SEL_DECIMALS, decoder=_dec_uint256) for c in need_dec}
            coin_dec = {c: (int(_ok(dp[c])) if _ok(dp[c]) is not None else 18)
                        for c in need_dec}
        for a, sc in curves.items():
            if a not in curve_ok:
                fin[a] = None       # not actually a Curve pool
                continue
            coins, bals, supply = curve_ok[a]
            decs = tuple(18 if c == _ETH_PLACEHOLDER else coin_dec.get(c, 18)
                         for c in coins)
            fin[a] = CurveLPStructure(sc.pool, coins, decs, sc.lp_decimals)
            dyn[a] = _CurveDyn(coins, bals, supply)

        for a, su in univ2.items():
            d0 = _ok(up[a]["d0"])
            d1 = _ok(up[a]["d1"])
            fin[a] = UniV2Structure(su.token0, su.token1,
                                    int(d0) if d0 is not None else 18,
                                    int(d1) if d1 is not None else 18,
                                    su.lp_decimals)
            res = _ok(up[a]["reserves"])
            supply = _ok(up[a]["supply"])
            r0, r1 = res if res else (0, 0)
            dyn[a] = _UniV2Dyn(int(r0), int(r1), int(supply or 0))

        return fin, dyn

    def _share_read(self, mc, addr: str, st: VaultStructure):
        if st.method == "convertToAssets":
            return mc.add(addr, _SEL_CONVERT_TO_ASSETS + _u256(10 ** st.vault_decimals),
                          decoder=_dec_uint256)
        if st.method == "getSharesToUnderlying":
            return mc.add(addr, _SEL_GET_SHARES_TO_UNDERLYING + _u256(10 ** st.vault_decimals),
                          decoder=_dec_uint256)
        sel = {
            "getPricePerFullShare": _SEL_GET_PRICE_PER_FULL_SHARE,
            "pricePerShare": _SEL_PRICE_PER_SHARE,
            "pricePerShare1e18": _SEL_PRICE_PER_SHARE,   # YB: same call, 1e18 scale
            "getPricePerShare": _SEL_GET_PRICE_PER_SHARE,
            "exchangeRate": _SEL_EXCHANGE_RATE,
        }[st.method]
        return mc.add(addr, sel, decoder=_dec_uint256)

    def _curve_fallback(self, client, pools: dict[str, CurveLPStructure],
                        out: dict) -> None:
        """Per-index ``coins(i)`` / ``balances(i)`` (uint256 AND int128 variants)
        for pools the registry didn't serve; a pool whose ``coins(0)`` reverts
        is dropped (stays out of ``out`` → finalized None by the caller)."""
        pend: dict[str, dict] = {}
        with client.multicall() as mc:
            for a, st in pools.items():
                d: dict = {"supply": mc.add(a, _SEL_TOTAL_SUPPLY, decoder=_dec_uint256),
                           "slots": []}
                for i in range(8):
                    arg = _u256(i)
                    d["slots"].append((
                        mc.add(st.pool, _SEL_COINS_U256 + arg, decoder=_dec_address),
                        mc.add(st.pool, _SEL_COINS_I128 + arg, decoder=_dec_address),
                        mc.add(st.pool, _SEL_BALANCES_U256 + arg, decoder=_dec_uint256),
                        mc.add(st.pool, _SEL_BALANCES_I128 + arg, decoder=_dec_uint256)))
                pend[a] = d
        for a in pools:
            coins: list[str] = []
            bals: list[int] = []
            for cu, ci, bu, bi in pend[a]["slots"]:
                coin = _ok(cu) or _ok(ci)
                if coin is None:
                    break
                bal = _ok(bu)
                if bal is None:
                    bal = _ok(bi)
                coins.append(str(coin).lower())
                bals.append(int(bal) if bal is not None else 0)
            if coins:
                supply = _ok(pend[a]["supply"])
                out[a] = (tuple(coins), tuple(bals), int(supply or 0))


class ChainedPriceSource(PriceSource):
    """Primary source first; on-chain vault/LP pricing for whatever it missed."""

    name = "defillama+onchain"

    def __init__(self, primary: PriceSource, onchain: OnChainVaultPrices):
        self._primary = primary
        self._onchain = onchain

    def fetch(self, chain, contracts, include_native=False):
        contracts = list(contracts)
        out = self._primary.fetch(chain, contracts, include_native)
        addrs = [c.lower() for c in contracts if _is_addr(c)]
        missing = [a for a in addrs if a not in out]
        if missing:
            # Primary had no quote → value it on-chain (carries icon metadata).
            out.update(self._onchain.price(
                chain, missing,
                lambda a, incl: self._primary.fetch(chain, a, incl)))
        # A vault/LP the primary DID price still needs its derived icon: attach
        # the immutable structure metadata (underlying / pooled coins) without
        # re-valuing it. Tokens just valued on-chain already carry it — skip.
        priced = [a for a in addrs
                  if (p := out.get(a)) is not None
                  and not p.underlying and not p.pool_tokens]
        if priced:
            for a, meta in self._onchain.icon_metadata(chain, priced).items():
                p = out.get(a)
                if p is not None:
                    out[a] = replace(p, underlying=meta.underlying,
                                     pool_tokens=meta.pool_tokens)
        return out


# --- dependency + computation ----------------------------------------------

def _deps_of(st: Structure) -> tuple[set[str], bool]:
    """The ERC-20 dependency addresses of a structure, and whether it needs the
    native price (a Curve pool holding the ETH placeholder)."""
    if isinstance(st, VaultStructure):
        return {st.underlying}, False
    if isinstance(st, UniV2Structure):
        return {st.token0, st.token1}, False
    deps = {c for c in st.coins if c != _ETH_PLACEHOLDER}
    return deps, (_ETH_PLACEHOLDER in st.coins)


def _compute_fixpoint(structures: dict[str, Structure], dynamics: dict[str, Dynamic],
                      resolved: dict[str, Price]) -> None:
    """Price every structured token whose dependencies are all priced, repeating
    until nothing new resolves (bottom-up through nesting)."""
    changed = True
    while changed:
        changed = False
        for a, st in structures.items():
            if a in resolved:
                continue
            p = _compute(st, dynamics.get(a), resolved)
            if p is not None:
                resolved[a] = p
                changed = True


def _compute(st: Structure | None, dyn, resolved: dict[str, Price]) -> Price | None:
    now = int(time.time())
    if isinstance(st, VaultStructure) and isinstance(dyn, _VaultDyn):
        up = resolved.get(st.underlying)
        if up is None or dyn.share_raw <= 0:
            return None
        # 1e18-scaled ratio methods (Yearn-v1 getPricePerFullShare, Yield Basis
        # pricePerShare) vs underlying-decimals-scaled (Yearn-v2 pricePerShare,
        # exchangeRate, the arg'd convertToAssets/getSharesToUnderlying).
        if st.method in ("getPricePerFullShare", "pricePerShare1e18"):
            share_price = Decimal(dyn.share_raw) / (Decimal(10) ** 18)
        else:
            share_price = Decimal(dyn.share_raw) / (Decimal(10) ** st.underlying_decimals)
        if share_price <= 0 or share_price > OnChainVaultPrices.ABSURD_SHARE_PRICE:
            return None
        src = "onchain-yb" if st.method == "pricePerShare1e18" else "onchain-4626"
        return Price(share_price * up.price_usd, now, src, 0.9 * up.confidence,
                     underlying=st.underlying)
    if isinstance(st, CurveLPStructure) and isinstance(dyn, _CurveDyn):
        if dyn.supply <= 0 or not st.coin_decimals:
            return None
        tvl = Decimal(0)
        confs: list[float] = []
        for coin, bal, dec in zip(st.coins, dyn.balances, st.coin_decimals):
            if bal == 0:
                continue
            cp = resolved.get("") if coin == _ETH_PLACEHOLDER else resolved.get(coin)
            if cp is None:
                return None      # every held coin must be priced, else underprice
            tvl += (Decimal(bal) / (Decimal(10) ** dec)) * cp.price_usd
            confs.append(cp.confidence)
        if tvl <= 0:
            return None
        usd = tvl / (Decimal(dyn.supply) / (Decimal(10) ** st.lp_decimals))
        return Price(usd, now, "onchain-curve-lp",
                     0.9 * (min(confs) if confs else 1.0), pool_tokens=st.coins)
    if isinstance(st, UniV2Structure) and isinstance(dyn, _UniV2Dyn):
        if dyn.supply <= 0:
            return None
        legs: list[tuple[Decimal, float]] = []
        for reserve, dec, tok in ((dyn.r0, st.dec0, st.token0),
                                  (dyn.r1, st.dec1, st.token1)):
            tp = resolved.get(tok)
            if tp is not None:
                legs.append(((Decimal(reserve) / (Decimal(10) ** dec)) * tp.price_usd,
                             tp.confidence))
        if not legs:
            return None
        tvl = sum((v for v, _ in legs), Decimal(0))
        if len(legs) == 1:
            tvl *= 2   # one priced leg → assume 50/50
        usd = tvl / (Decimal(dyn.supply) / (Decimal(10) ** st.lp_decimals))
        return Price(usd, now, "onchain-univ2-lp", 0.9 * min(c for _, c in legs),
                     pool_tokens=(st.token0, st.token1))
    return None
