from dataclasses import dataclass, asdict


@dataclass
class Chain:
    name: str
    chain_id: int
    rpc_url: str
    symbol: str = "ETH"
    explorer: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT_CHAINS: list[Chain] = [
    Chain("Ethereum", 1, "https://eth.drpc.org", "ETH", "https://etherscan.io"),
    Chain("Optimism", 10, "https://optimism.drpc.org", "ETH", "https://optimistic.etherscan.io"),
    Chain("Polygon", 137, "https://polygon.drpc.org", "MATIC", "https://polygonscan.com"),
    Chain("Arbitrum", 42161, "https://arbitrum.drpc.org", "ETH", "https://arbiscan.io"),
    Chain("Base", 8453, "https://base.drpc.org", "ETH", "https://basescan.org"),
]
