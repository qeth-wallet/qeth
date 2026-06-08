from dataclasses import dataclass, asdict


@dataclass
class Chain:
    name: str
    chain_id: int
    rpc_url: str
    symbol: str = "ETH"
    explorer: str = ""
    # CoinGecko id for the native asset, used by price sources that key
    # natives by coin id (e.g. DefiLlama). Defaults to "ethereum" since
    # ETH is the native asset on most chains we support.
    coingecko_id: str = "ethereum"
    # Whether the chain accepts EIP-1559 (type 2) transactions. All
    # five DEFAULT_CHAINS do; the flag is here so future legacy-only
    # additions (BSC, Fantom, niche L2s) can opt out and the gas
    # suggestion logic picks the right path automatically.
    eip1559: bool = True
    # Backup RPC endpoints, tried in order when the primary errors at the
    # transport level. EthClient fails over to these so a flaky provider
    # doesn't blank out balances — DRPC's free Gnosis endpoint, for one,
    # 400s on every eth_call ("can't route") while routing eth_getBalance
    # fine, which silently dropped all ERC-20 tokens until the multicall
    # gave up. publicnode covers all these chains and is a solid default.
    fallback_rpcs: tuple[str, ...] = ()
    # WebSocket endpoints for the live-update watcher (newHeads + ERC-20
    # Transfer logs), tried in order. Each is validated to accept
    # eth_subscribe. Empty falls back to deriving wss from the http URLs
    # (works when the host serves ws on the same origin), then to http
    # polling. See qeth.live_watcher / qeth.async_chain.ws_urls_for.
    ws_url: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT_CHAINS: list[Chain] = [
    Chain("Ethereum", 1,     "https://eth.drpc.org",       "ETH",   "https://etherscan.io",            "ethereum",
          fallback_rpcs=("https://ethereum-rpc.publicnode.com",),
          ws_url=("wss://eth.drpc.org", "wss://ethereum-rpc.publicnode.com")),
    Chain("Optimism", 10,    "https://optimism.drpc.org",  "ETH",   "https://optimistic.etherscan.io", "ethereum",
          fallback_rpcs=("https://optimism-rpc.publicnode.com",),
          ws_url=("wss://optimism.drpc.org", "wss://optimism-rpc.publicnode.com")),
    # Polygon's native id on CoinGecko is "polygon-ecosystem-token" since
    # the MATIC -> POL rebrand. The on-chain symbol is still MATIC.
    Chain("Polygon",  137,   "https://polygon.drpc.org",   "MATIC", "https://polygonscan.com",         "polygon-ecosystem-token",
          fallback_rpcs=("https://polygon-bor-rpc.publicnode.com",),
          ws_url=("wss://polygon.drpc.org", "wss://polygon-bor-rpc.publicnode.com")),
    Chain("Arbitrum", 42161, "https://arbitrum.drpc.org",  "ETH",   "https://arbiscan.io",             "ethereum",
          fallback_rpcs=("https://arbitrum-one-rpc.publicnode.com",),
          ws_url=("wss://arbitrum.drpc.org", "wss://arbitrum-one-rpc.publicnode.com")),
    Chain("Base",     8453,  "https://base.drpc.org",      "ETH",   "https://basescan.org",            "ethereum",
          fallback_rpcs=("https://base-rpc.publicnode.com",),
          ws_url=("wss://base.drpc.org", "wss://base-rpc.publicnode.com")),
    # xDai / chiado has its own native; the CoinGecko id is "xdai".
    # gnosis.drpc.org can't route eth_call (only eth_getBalance), so it's
    # demoted to a last-resort fallback behind two endpoints that can.
    # ws: only publicnode answers eth_subscribe (rpc.gnosischain.com ws times
    # out), so it's the sole ws endpoint.
    Chain("Gnosis",   100,   "https://gnosis-rpc.publicnode.com", "XDAI", "https://gnosisscan.io",     "xdai",
          fallback_rpcs=("https://rpc.gnosischain.com", "https://gnosis.drpc.org"),
          ws_url=("wss://gnosis-rpc.publicnode.com",)),
    # BNB Smart Chain — PoA consensus (validator sigs in extraData),
    # but EthClient injects ExtraDataToPOAMiddleware so the standard
    # block-reading paths just work. EIP-1559 is supported since
    # BEP-336 (2024) though baseFee is typically 0; gas_price
    # fallback in the suggestion worker handles that case cleanly.
    Chain("BNB Smart Chain", 56, "https://bsc.drpc.org",   "BNB",   "https://bscscan.com",             "binancecoin",
          fallback_rpcs=("https://bsc-rpc.publicnode.com",),
          ws_url=("wss://bsc-rpc.publicnode.com",)),
]
