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

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT_CHAINS: list[Chain] = [
    Chain("Ethereum", 1,     "https://eth.drpc.org",       "ETH",   "https://etherscan.io",            "ethereum"),
    Chain("Optimism", 10,    "https://optimism.drpc.org",  "ETH",   "https://optimistic.etherscan.io", "ethereum"),
    # Polygon's native id on CoinGecko is "polygon-ecosystem-token" since
    # the MATIC -> POL rebrand. The on-chain symbol is still MATIC.
    Chain("Polygon",  137,   "https://polygon.drpc.org",   "MATIC", "https://polygonscan.com",         "polygon-ecosystem-token"),
    Chain("Arbitrum", 42161, "https://arbitrum.drpc.org",  "ETH",   "https://arbiscan.io",             "ethereum"),
    Chain("Base",     8453,  "https://base.drpc.org",      "ETH",   "https://basescan.org",            "ethereum"),
]
