"""Hermetic tests for the on-chain vault/LP pricer.

A ``FakeChain`` answers ``aggregate3`` eth_calls from a per-``(target,
selector)`` handler table, so the REAL ``Multicall`` + the module's real
decoders run without a node. Handlers are callables ``(calldata) -> bytes |
None`` (None = revert), so they can decode args (e.g. ``convertToAssets(x)``).
"""

from decimal import Decimal

from eth_abi import decode, encode
from eth_hash.auto import keccak

from qeth.pricing import onchain as M
from qeth.pricing.base import Price, PriceSource
from qeth.pricing.onchain import ChainedPriceSource, OnChainVaultPrices

_AGG3 = bytes.fromhex("82ad56cb")


# --- encoding helpers ------------------------------------------------------

def u256(v: int) -> bytes:
    return int(v).to_bytes(32, "big")


def addr32(a: str) -> bytes:
    return bytes(12) + bytes.fromhex(a[2:].lower())


def reserves(r0: int, r1: int, ts: int = 0) -> bytes:
    return u256(r0) + u256(r1) + u256(ts)


def addr_arr(addrs: list[str], n: int = 8) -> bytes:
    padded = list(addrs) + [M._ZERO_ADDR] * (n - len(addrs))
    return encode([f"address[{n}]"], [padded])


def uint_arr(vals: list[int], n: int = 8) -> bytes:
    padded = list(vals) + [0] * (n - len(vals))
    return encode([f"uint256[{n}]"], [padded])


def _arg_addr(cd: bytes) -> str:
    """The single address argument of a call (last 20 of the word after sel)."""
    return "0x" + cd[16:36].hex()


class Chain:
    def __init__(self, cid: int = 1):
        self.chain_id = cid


class FakeChain:
    """handlers: {(target_lower, selector_hex): callable(calldata)->bytes|None}"""

    def __init__(self, handlers: dict):
        self.handlers = handlers
        self.calls = 0

    def multicall(self, **kw):
        from qeth.chain import Multicall, _ensure_heavy_imports
        _ensure_heavy_imports()   # inject abi_encode/abi_decode (EthClient bypassed)
        return Multicall(self, **kw)

    def call(self, tx, block="latest"):
        self.calls += 1
        data = bytes.fromhex(tx["data"][2:])
        assert data[:4] == _AGG3
        inner = decode(["(address,bool,bytes)[]"], data[4:])[0]
        rets = []
        for target, _allow, cd in inner:
            cd = bytes(cd)
            h = self.handlers.get((target.lower(), cd[:4].hex()))
            r = h(cd) if h else None
            rets.append((True, r) if r is not None else (False, b""))
        return "0x" + encode(["(bool,bytes)[]"], [rets]).hex()


class _BaseLookup:
    """A base_lookup that prices from a fixed map and records its calls."""

    def __init__(self, prices: dict):
        # prices: {addr_lower or "": (usd, conf)}
        self.prices = prices
        self.calls: list[tuple[list[str], bool]] = []

    def __call__(self, addrs, include_native):
        addrs = list(addrs)
        self.calls.append((addrs, include_native))
        out: dict[str, Price] = {}
        for a in addrs:
            if a.lower() in self.prices:
                usd, conf = self.prices[a.lower()]
                out[a.lower()] = Price(Decimal(str(usd)), 1, "defillama", conf)
        if include_native and "" in self.prices:
            usd, conf = self.prices[""]
            out[""] = Price(Decimal(str(usd)), 1, "defillama", conf)
        return out


def _mk(handlers):
    """OnChainVaultPrices wired to a FakeChain; returns (pricer, fake)."""
    fake = FakeChain(handlers)
    return OnChainVaultPrices(client_factory=lambda ch: fake), fake


A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
C = "0x" + "c3" * 20
REG = "0x" + "d4" * 20


# --- 1. selector guard -----------------------------------------------------

def test_every_selector_matches_keccak():
    sigs = {
        "_SEL_PRICE_PER_SHARE": "pricePerShare()",
        "_SEL_GET_PRICE_PER_SHARE": "getPricePerShare()",
        "_SEL_GET_PRICE_PER_FULL_SHARE": "getPricePerFullShare()",
        "_SEL_GET_SHARES_TO_UNDERLYING": "getSharesToUnderlying(uint256)",
        "_SEL_EXCHANGE_RATE": "exchangeRate()",
        "_SEL_CONVERT_TO_ASSETS": "convertToAssets(uint256)",
        "_SEL_ASSET": "asset()", "_SEL_TOKEN": "token()",
        "_SEL_UNDERLYING": "underlying()", "_SEL_WANT": "want()",
        "_SEL_INPUT": "input()", "_SEL_NATIVE": "native()",
        "_SEL_TOKEN0": "token0()", "_SEL_TOKEN1": "token1()",
        "_SEL_GET_RESERVES": "getReserves()", "_SEL_TOTAL_SUPPLY": "totalSupply()",
        "_SEL_DECIMALS": "decimals()", "_SEL_MINTER": "minter()",
        "_SEL_GET_REGISTRY": "get_registry()",
        "_SEL_GET_POOL_FROM_LP": "get_pool_from_lp_token(address)",
        "_SEL_GET_COINS": "get_coins(address)",
        "_SEL_GET_BALANCES": "get_balances(address)",
        "_SEL_COINS_U256": "coins(uint256)", "_SEL_BALANCES_U256": "balances(uint256)",
        "_SEL_COINS_I128": "coins(int128)", "_SEL_BALANCES_I128": "balances(int128)",
        "_SEL_ASSET_TOKEN": "ASSET_TOKEN()",
    }
    for const, sig in sigs.items():
        assert getattr(M, const) == keccak(sig.encode())[:4], sig


# --- 2. ERC-4626 with vault decimals != underlying decimals -----------------

def _vault_4626(h, vault, *, vdec, underlying, udec, assets_per_share):
    scale = int(Decimal(str(assets_per_share)) * (10 ** udec))   # assets-raw per whole share

    def convert(cd):
        shares = int.from_bytes(cd[4:36], "big")
        return u256(shares * scale // (10 ** vdec))
    h[(vault, M._SEL_DECIMALS.hex())] = lambda cd: u256(vdec)
    h[(vault, M._SEL_TOTAL_SUPPLY.hex())] = lambda cd: u256(10 ** 24)
    h[(vault, M._SEL_CONVERT_TO_ASSETS.hex())] = convert
    h[(vault, M._SEL_ASSET.hex())] = lambda cd: addr32(underlying)
    h[(underlying, M._SEL_DECIMALS.hex())] = lambda cd: u256(udec)


def test_4626_vault_decimals_differ_from_underlying():
    h: dict = {}
    _vault_4626(h, A, vdec=18, underlying=B, udec=6, assets_per_share="2.0")
    pricer, fake = _mk(h)
    base = _BaseLookup({B: ("1.0", 0.99)})     # underlying (USDC) = $1
    out = pricer.price(Chain(), [A], base)
    assert out[A].price_usd == Decimal("2.0")   # 2 underlying/share × $1
    assert out[A].source == "onchain-4626"
    assert out[A].underlying == B               # surfaced for the vault icon
    assert out[A].confidence == 0.9 * 0.99


# --- 3. Yearn v1 (getPricePerFullShare /1e18) + pricePerShare + want() ------

def test_get_price_per_full_share_scales_by_1e18():
    h: dict = {}
    h[(A, M._SEL_DECIMALS.hex())] = lambda cd: u256(18)
    h[(A, M._SEL_TOTAL_SUPPLY.hex())] = lambda cd: u256(10 ** 24)
    h[(A, M._SEL_GET_PRICE_PER_FULL_SHARE.hex())] = lambda cd: u256(int(Decimal("1.05") * 10 ** 18))
    h[(A, M._SEL_TOKEN.hex())] = lambda cd: addr32(B)      # underlying via token()
    h[(B, M._SEL_DECIMALS.hex())] = lambda cd: u256(18)
    pricer, _ = _mk(h)
    out = pricer.price(Chain(), [A], _BaseLookup({B: ("3.0", 1.0)}))
    assert out[A].price_usd == Decimal("3.15")            # 1.05 × $3


def test_price_per_share_scales_by_underlying_decimals():
    h: dict = {}
    # pricePerShare returns underlying-raw per whole share; underlying via want().
    h[(A, M._SEL_DECIMALS.hex())] = lambda cd: u256(18)
    h[(A, M._SEL_TOTAL_SUPPLY.hex())] = lambda cd: u256(10 ** 24)
    h[(A, M._SEL_PRICE_PER_SHARE.hex())] = lambda cd: u256(int(Decimal("1.5") * 10 ** 6))
    h[(A, M._SEL_WANT.hex())] = lambda cd: addr32(B)
    h[(B, M._SEL_DECIMALS.hex())] = lambda cd: u256(6)     # USDC-like underlying
    pricer, _ = _mk(h)
    out = pricer.price(Chain(), [A], _BaseLookup({B: ("1.0", 1.0)}))
    assert out[A].price_usd == Decimal("1.5")


def test_yield_basis_vault_pricePerShare_is_1e18_over_asset_token():
    """A Yield Basis vault (ASSET_TOKEN() + pricePerShare()) scales
    pricePerShare by 1e18 regardless of the asset's decimals — here an 8-decimal
    WBTC asset, the case where the generic Yearn-v2 rule would be off by 1e10."""
    h: dict = {}
    h[(A, M._SEL_DECIMALS.hex())] = lambda cd: u256(18)               # yb-WBTC: 18 dec
    h[(A, M._SEL_TOTAL_SUPPLY.hex())] = lambda cd: u256(10 ** 22)
    h[(A, M._SEL_PRICE_PER_SHARE.hex())] = lambda cd: u256(int(Decimal("1.05") * 10 ** 18))
    h[(A, M._SEL_ASSET_TOKEN.hex())] = lambda cd: addr32(B)           # WBTC
    h[(B, M._SEL_DECIMALS.hex())] = lambda cd: u256(8)                # 8-dec asset
    pricer, _ = _mk(h)
    out = pricer.price(Chain(), [A], _BaseLookup({B: ("100000", 1.0)}))  # WBTC $100k
    assert out[A].price_usd == Decimal("105000.00")   # 1.05 WBTC × $100k
    assert out[A].source == "onchain-yb"
    assert out[A].underlying == B                     # WBTC, for the vault icon


# --- 4. guards: zero supply / absurd / empty-returndata EOA ----------------

def test_zero_total_supply_gives_no_price():
    h: dict = {}
    _vault_4626(h, A, vdec=18, underlying=B, udec=18, assets_per_share="1.0")
    h[(A, M._SEL_TOTAL_SUPPLY.hex())] = lambda cd: u256(0)   # empty vault
    # a share_raw of 0 is what an empty vault reports for convertToAssets too:
    h[(A, M._SEL_CONVERT_TO_ASSETS.hex())] = lambda cd: u256(0)
    pricer, _ = _mk(h)
    assert pricer.price(Chain(), [A], _BaseLookup({B: ("1.0", 1.0)})) == {}


def test_absurd_share_price_rejected():
    h: dict = {}
    _vault_4626(h, A, vdec=18, underlying=B, udec=18, assets_per_share="10000000")  # 1e7
    pricer, _ = _mk(h)
    assert pricer.price(Chain(), [A], _BaseLookup({B: ("1.0", 1.0)})) == {}


def test_plain_eoa_is_not_classified():
    # No handlers → every probe reverts (empty returndata). Not a vault/LP.
    pricer, _ = _mk({})
    assert pricer.price(Chain(), [A], _BaseLookup({})) == {}


# --- 5. Curve LP via registry (incl. ETH placeholder coin) -----------------

def _curve_via_registry(h, lp, pool, coins, bals, decs, supply, *, registry=REG):
    h[(M.CURVE_ADDRESS_PROVIDER.lower(), M._SEL_GET_REGISTRY.hex())] = lambda cd: addr32(registry)
    h[(lp, M._SEL_DECIMALS.hex())] = lambda cd: u256(18)
    h[(lp, M._SEL_TOTAL_SUPPLY.hex())] = lambda cd: u256(supply)
    h[(registry.lower(), M._SEL_GET_POOL_FROM_LP.hex())] = \
        lambda cd: addr32(pool) if _arg_addr(cd) == lp else None
    h[(registry.lower(), M._SEL_GET_COINS.hex())] = \
        lambda cd: addr_arr(coins) if _arg_addr(cd) == pool else None
    h[(registry.lower(), M._SEL_GET_BALANCES.hex())] = \
        lambda cd: uint_arr(bals) if _arg_addr(cd) == pool else None
    for c, d in zip(coins, decs):
        if c != M._ETH_PLACEHOLDER:
            h[(c, M._SEL_DECIMALS.hex())] = (lambda dd: lambda cd: u256(dd))(d)


def test_curve_lp_tvl_over_supply():
    h: dict = {}
    # 1M DAI (18) + 1M USDC (6); 2M LP @ 1e18 → $1.00
    _curve_via_registry(h, A, A, [B, C], [10 ** 6 * 10 ** 18, 10 ** 6 * 10 ** 6],
                        [18, 6], 2 * 10 ** 6 * 10 ** 18)
    pricer, _ = _mk(h)
    out = pricer.price(Chain(), [A], _BaseLookup({B: ("1.0", 1.0), C: ("1.0", 1.0)}))
    assert out[A].price_usd == Decimal("1")
    assert out[A].source == "onchain-curve-lp"
    assert out[A].pool_tokens == (B.lower(), C.lower())   # for the stacked icon


def test_curve_lp_with_eth_placeholder_coin_uses_native():
    h: dict = {}
    eth = M._ETH_PLACEHOLDER
    # 100 ETH + 100 token(18), each valued $2000; 200 LP @ 1e18 → $2000
    _curve_via_registry(h, A, A, [eth, B], [100 * 10 ** 18, 100 * 10 ** 18],
                        [18, 18], 200 * 10 ** 18)
    pricer, _ = _mk(h)
    base = _BaseLookup({"": ("2000", 1.0), B: ("2000", 1.0)})
    out = pricer.price(Chain(), [A], base)
    assert out[A].price_usd == Decimal("2000")
    assert any(incl for _addrs, incl in base.calls)   # asked for native


# --- 6. Curve factory pool: registry misses, coins(i)/balances(i) fallback -

def test_curve_factory_pool_per_index_fallback():
    h: dict = {}
    # registry exists but doesn't know this LP; pool == lp (factory).
    h[(M.CURVE_ADDRESS_PROVIDER.lower(), M._SEL_GET_REGISTRY.hex())] = lambda cd: addr32(REG)
    h[(REG.lower(), M._SEL_GET_POOL_FROM_LP.hex())] = lambda cd: None   # registry miss
    h[(A, M._SEL_DECIMALS.hex())] = lambda cd: u256(18)
    h[(A, M._SEL_TOTAL_SUPPLY.hex())] = lambda cd: u256(2 * 10 ** 6 * 10 ** 18)
    h[(A, M._SEL_MINTER.hex())] = lambda cd: None
    # registry.get_coins/get_balances revert for this pool → fallback path.
    h[(REG.lower(), M._SEL_GET_COINS.hex())] = lambda cd: None
    h[(REG.lower(), M._SEL_GET_BALANCES.hex())] = lambda cd: None
    # per-index: coins(0)=B via int128 variant; coins(1)=C via uint256 variant.
    coins = {0: B, 1: C}
    bals = {0: 10 ** 6 * 10 ** 18, 1: 10 ** 6 * 10 ** 18}

    def coins_i128(cd):
        i = int.from_bytes(cd[4:36], "big")
        return addr32(coins[i]) if i in coins and i == 0 else None

    def coins_u256(cd):
        i = int.from_bytes(cd[4:36], "big")
        return addr32(coins[i]) if i in coins and i == 1 else None

    def bals_u256(cd):
        i = int.from_bytes(cd[4:36], "big")
        return u256(bals[i]) if i in bals else None
    h[(A, M._SEL_COINS_I128.hex())] = coins_i128
    h[(A, M._SEL_COINS_U256.hex())] = coins_u256
    h[(A, M._SEL_BALANCES_U256.hex())] = bals_u256
    h[(B, M._SEL_DECIMALS.hex())] = lambda cd: u256(18)
    h[(C, M._SEL_DECIMALS.hex())] = lambda cd: u256(18)
    pricer, _ = _mk(h)
    out = pricer.price(Chain(), [A], _BaseLookup({B: ("1.0", 1.0), C: ("1.0", 1.0)}))
    assert out[A].price_usd == Decimal("1")


# --- 7. Uniswap-V2-style LP: both / one / zero priced legs -----------------

def _univ2(h, lp, t0, t1, r0, r1, d0, d1, supply):
    h[(lp, M._SEL_DECIMALS.hex())] = lambda cd: u256(18)
    h[(lp, M._SEL_TOTAL_SUPPLY.hex())] = lambda cd: u256(supply)
    h[(lp, M._SEL_TOKEN0.hex())] = lambda cd: addr32(t0)
    h[(lp, M._SEL_TOKEN1.hex())] = lambda cd: addr32(t1)
    h[(lp, M._SEL_GET_RESERVES.hex())] = lambda cd: reserves(r0, r1)
    h[(t0, M._SEL_DECIMALS.hex())] = lambda cd: u256(d0)
    h[(t1, M._SEL_DECIMALS.hex())] = lambda cd: u256(d1)


def test_univ2_both_legs():
    h: dict = {}
    # 100 WETH ($2000) + 200000 USDC ($1); 100 LP @ 1e18 → (200000+200000)/100
    _univ2(h, A, B, C, 100 * 10 ** 18, 200000 * 10 ** 6, 18, 6, 100 * 10 ** 18)
    pricer, _ = _mk(h)
    out = pricer.price(Chain(), [A], _BaseLookup({B: ("2000", 1.0), C: ("1.0", 1.0)}))
    assert out[A].price_usd == Decimal("4000")
    assert out[A].source == "onchain-univ2-lp"
    assert out[A].pool_tokens == (B.lower(), C.lower())


def test_univ2_one_priced_leg_doubles():
    h: dict = {}
    _univ2(h, A, B, C, 100 * 10 ** 18, 200000 * 10 ** 6, 18, 6, 100 * 10 ** 18)
    pricer, _ = _mk(h)
    out = pricer.price(Chain(), [A], _BaseLookup({B: ("2000", 1.0)}))   # only WETH
    assert out[A].price_usd == Decimal("4000")   # 200000 × 2 / 100


def test_univ2_no_priced_legs_gives_nothing():
    h: dict = {}
    _univ2(h, A, B, C, 100 * 10 ** 18, 200000 * 10 ** 6, 18, 6, 100 * 10 ** 18)
    pricer, _ = _mk(h)
    assert pricer.price(Chain(), [A], _BaseLookup({})) == {}


# --- 8. nested 4626-over-CurveLP, one base_lookup call per level ------------

def test_nested_vault_reports_terminal_underlying_for_icon():
    """A vault over a vault (a 4626 wrapping a YB vault wrapping WETH): the
    price recurses, and Price.underlying is the TERMINAL asset (WETH) — not the
    intermediate vault, which has no icon of its own."""
    h: dict = {}
    outer, inner, weth = A, B, C
    _vault_4626(h, outer, vdec=18, underlying=inner, udec=18, assets_per_share="1.0")
    # inner: a YB vault (pricePerShare + ASSET_TOKEN) over WETH
    h[(inner, M._SEL_DECIMALS.hex())] = lambda cd: u256(18)
    h[(inner, M._SEL_TOTAL_SUPPLY.hex())] = lambda cd: u256(10 ** 24)
    h[(inner, M._SEL_PRICE_PER_SHARE.hex())] = lambda cd: u256(int(Decimal("1.05") * 10 ** 18))
    h[(inner, M._SEL_ASSET_TOKEN.hex())] = lambda cd: addr32(weth)
    h[(weth, M._SEL_DECIMALS.hex())] = lambda cd: u256(18)
    pricer, _ = _mk(h)
    out = pricer.price(Chain(), [outer], _BaseLookup({weth: ("3000", 1.0)}))
    assert out[outer].price_usd == Decimal("3150")   # 1.0 × (1.05 × $3000)
    assert out[outer].underlying == weth             # terminal WETH, not inner vault
    assert out[outer].confidence == 0.9 * (0.9 * 1.0)


def test_nested_vault_over_curve_lp():
    h: dict = {}
    vault, lp, coin0, coin1 = A, B, C, "0x" + "e5" * 20
    # LP: Curve factory pool over [coin0 $1, coin1 $1], price $1.
    _curve_via_registry(h, lp, lp, [coin0, coin1],
                        [10 ** 6 * 10 ** 18, 10 ** 6 * 10 ** 18], [18, 18],
                        2 * 10 ** 6 * 10 ** 18)
    # Vault: 1 share = 1.1 LP (both 18 dec).
    _vault_4626(h, vault, vdec=18, underlying=lp, udec=18, assets_per_share="1.1")
    pricer, _ = _mk(h)
    base = _BaseLookup({coin0: ("1.0", 1.0), coin1: ("1.0", 1.0)})
    out = pricer.price(Chain(), [vault], base)
    assert out[vault].price_usd == Decimal("1.1")     # 1.1 LP × $1
    assert out[vault].confidence == 0.9 * (0.9 * 1.0)  # compounds one level
    assert len(base.calls) == 2                        # one lookup per level


# --- 9. depth cap / self-referential vault terminates ----------------------

def test_self_referential_vault_terminates():
    h: dict = {}
    _vault_4626(h, A, vdec=18, underlying=A, udec=18, assets_per_share="1.0")
    pricer, _ = _mk(h)
    assert pricer.price(Chain(), [A], _BaseLookup({})) == {}   # never resolves, no hang


# --- 10. negative memo → zero calls on the second fetch --------------------

def test_negative_memo_skips_second_fetch():
    pricer, fake = _mk({})            # plain token, all probes revert
    pricer.price(Chain(), [A], _BaseLookup({}))
    first = fake.calls
    assert first > 0
    pricer.price(Chain(), [A], _BaseLookup({}))   # A now memoized negative
    assert fake.calls == first                     # zero additional calls


# --- 11. ChainedPriceSource: primary first, on-chain for misses ------------

class _FakePrimary(PriceSource):
    name = "primary"

    def __init__(self, prices):
        self.prices = prices

    def fetch(self, chain, contracts, include_native=False):
        out = {}
        for c in contracts:
            if c.lower() in self.prices:
                usd, conf = self.prices[c.lower()]
                out[c.lower()] = Price(Decimal(str(usd)), 1, "primary", conf)
        if include_native and "" in self.prices:
            usd, conf = self.prices[""]
            out[""] = Price(Decimal(str(usd)), 1, "primary", conf)
        return out


def test_chained_source_prices_primary_hits_and_onchain_misses():
    h: dict = {}
    _vault_4626(h, A, vdec=18, underlying=B, udec=18, assets_per_share="1.2")
    fake = FakeChain(h)
    onchain = OnChainVaultPrices(client_factory=lambda ch: fake)
    primary = _FakePrimary({B: ("1.0", 1.0), C: ("5.0", 1.0)})  # knows B (underlying) + C
    chained = ChainedPriceSource(primary, onchain)
    out = chained.fetch(Chain(), [A, C])   # C priced by primary; A is the vault
    assert out[C].price_usd == Decimal("5.0") and out[C].source == "primary"
    assert out[A].price_usd == Decimal("1.2") and out[A].source == "onchain-4626"


# --- icon metadata (structure without valuation) ---------------------------

def test_icon_metadata_curve_lp_and_plain():
    h: dict = {}
    _curve_via_registry(h, A, A, [B, C], [10 ** 24, 10 ** 24], [18, 18], 2 * 10 ** 24)
    pricer, _ = _mk(h)
    meta = pricer.icon_metadata(Chain(), [A, C])   # A is the LP; C a plain coin
    assert meta[A].pool_tokens == (B.lower(), C.lower())
    assert meta[A].underlying is None
    assert C.lower() not in meta                    # a pooled coin isn't itself an LP


def test_icon_metadata_vault_reports_underlying():
    h: dict = {}
    _vault_4626(h, A, vdec=18, underlying=B, udec=18, assets_per_share="1.2")
    pricer, _ = _mk(h)
    meta = pricer.icon_metadata(Chain(), [A])
    assert meta[A].underlying == B.lower() and meta[A].pool_tokens is None


def test_icon_metadata_memoized_no_second_probe():
    h: dict = {}
    _curve_via_registry(h, A, A, [B, C], [10 ** 24, 10 ** 24], [18, 18], 2 * 10 ** 24)
    pricer, fake = _mk(h)
    pricer.icon_metadata(Chain(), [A])
    n = fake.calls
    again = pricer.icon_metadata(Chain(), [A])   # served from memo
    assert fake.calls == n                        # zero further eth_calls
    assert again[A].pool_tokens == (B.lower(), C.lower())


def test_chained_source_enriches_primary_priced_lp():
    """A Curve LP the primary already prices keeps that price but gains its
    pool_tokens (for the stacked icon) — the on-chain valuer never runs."""
    h: dict = {}
    _curve_via_registry(h, A, A, [B, C], [10 ** 24, 10 ** 24], [18, 18], 2 * 10 ** 24)
    fake = FakeChain(h)
    onchain = OnChainVaultPrices(client_factory=lambda ch: fake)
    primary = _FakePrimary({A: ("1.01", 0.97)})   # primary prices the LP itself
    out = ChainedPriceSource(primary, onchain).fetch(Chain(), [A])
    assert out[A].price_usd == Decimal("1.01") and out[A].source == "primary"
    assert out[A].pool_tokens == (B.lower(), C.lower())


def test_chained_source_plain_token_gains_no_metadata():
    fake = FakeChain({})   # no handlers → nothing classifies
    onchain = OnChainVaultPrices(client_factory=lambda ch: fake)
    primary = _FakePrimary({A: ("2.0", 1.0)})
    out = ChainedPriceSource(primary, onchain).fetch(Chain(), [A])
    assert out[A].pool_tokens is None and out[A].underlying is None
