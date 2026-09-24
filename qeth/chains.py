from dataclasses import dataclass, asdict

# Chain families: how a network encodes addresses, builds + signs transactions
# and serves reads. Every EVM chain shares one pipeline (JSON-RPC, RLP txs,
# nonces, 0x addresses); Tron differs in all of those (base58 addresses,
# protobuf txs with a ref-block instead of a nonce, TronGrid's HTTP API).
# Plain strings, not an Enum, so they round-trip through the JSON config.
EVM = "evm"
TRON = "tron"
FAMILIES = (EVM, TRON)


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
    # polling. See qeth.plugins.transactions.live_watcher / qeth.async_chain.ws_urls_for.
    ws_url: tuple[str, ...] = ()
    # One of FAMILIES. Chains added by a dapp (wallet_addEthereumChain) or by
    # hand are EVM by construction.
    family: str = EVM
    # Decimals of the native asset: 18 on every EVM chain (wei), 6 on Tron
    # (sun per TRX).
    native_decimals: int = 18
    # Base URL of the family's own HTTP API where it has one (Tron's full-node
    # /wallet/* API), plus backups tried in order when it fails at the
    # transport level or rate-limits. EVM chains read everything over
    # ``rpc_url``.
    api_url: str = ""
    api_fallbacks: tuple[str, ...] = ()
    # Multicall3, when it isn't at the canonical 0xcA11… CREATE2 address (the
    # TVM derives contract addresses differently, so Tron's is elsewhere).
    # Empty = canonical.
    multicall_address: str = ""

    @property
    def is_evm(self) -> bool:
        return self.family == EVM

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
    # Robinhood Chain — an Arbitrum Orbit rollup paying gas in ETH. The
    # ArbSys block-height path in chain.py needs no chain list and the
    # precompile is present here, so the Orbit heritage is handled already;
    # Multicall3 is at the canonical address, so token balances work.
    # Named "Robinhood" (not "Robinhood Chain") deliberately: the chain-icon
    # fallback derives a Curve slug from the display name, and
    # curve-assets/chains/robinhood.png exists — so it gets a logo with no
    # bundled PNG. Reading it needs an Etherscan key: its Blockscout API is
    # behind a Cloudflare challenge (see ETHERSCAN_V2_CHAINS).
    Chain("Robinhood", 4663, "https://robinhood.drpc.org", "ETH",   "https://robin.etherscan.io",      "ethereum",
          fallback_rpcs=("https://robinhood-rpc.publicnode.com",
                         "https://rpc.mainnet.chain.robinhood.com"),
          ws_url=("wss://robinhood.drpc.org", "wss://robinhood-rpc.publicnode.com")),
    # Tron — the one non-EVM family (qeth/tron, docs/tron.md): T… addresses,
    # protobuf transactions built locally and broadcast over the full-node
    # HTTP API (``api_url``; keyless TronGrid allows ~3 req/s, so PublicNode
    # leads). ``rpc_url`` is Tron's read-only Ethereum JSON-RPC: eth_getBalance
    # (in sun), eth_call and Multicall3 — deployed at its own Tron address,
    # TEazPvZwDjDtFeJupyo7QunvnrnUjPH8ED — all work there, so balances and
    # token metadata read through EthClient; sending never does. No ws.
    Chain("Tron",     728126428, "https://tron-rpc.publicnode.com/jsonrpc", "TRX", "https://tronscan.org", "tron",
          eip1559=False,
          fallback_rpcs=("https://api.trongrid.io/jsonrpc",),
          family=TRON, native_decimals=6,
          api_url="https://tron-rpc.publicnode.com",
          api_fallbacks=("https://api.trongrid.io",),
          multicall_address="0x32a4F47A74a6810BD0bF861CABAb99656a75DE9E"),
]
