# ============================================================
# qeth connector — wallet status probe (shared, Qt-free)
#
# The pure logic behind "is qeth reachable, on which chain, as which
# account?": the request body, the friendly chain names, and the response
# parser. Kept free of Falkon/Qt so it's unit-testable (tests/test_falkon_
# provider.py) and shared by both the status dialog (settings.py) and the
# toolbar button's poller (toolbar.py).
# ============================================================

import json
from typing import NamedTuple

ENDPOINT = "http://127.0.0.1:1248/"
POLL_INTERVAL_MS = 5000     # toolbar poll cadence
REQUEST_TIMEOUT_MS = 4000

# Friendly names for the chains qeth ships with; anything else falls back to
# "Chain <id>". Purely cosmetic. Kept in step with the webext popup's list.
CHAIN_NAMES = {
    1: "Ethereum", 10: "Optimism", 56: "BNB Chain", 100: "Gnosis",
    137: "Polygon", 8453: "Base", 42161: "Arbitrum", 43114: "Avalanche",
}


def chain_name(hex_id):
    """Friendly name for a 0x-hex chain id (e.g. ``"0x1"`` → ``"Ethereum"``)."""
    try:
        cid = int(hex_id, 16)
    except (TypeError, ValueError):
        return str(hex_id)
    return CHAIN_NAMES.get(cid, f"Chain {cid}")


def batch_body() -> bytes:
    """One batched JSON-RPC request carrying both the chain id (id 1) and the
    account (id 2). A single round-trip is more robust than two separate
    requests through Qt's connection-reusing network manager."""
    return json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
        {"jsonrpc": "2.0", "id": 2, "method": "eth_accounts", "params": []},
    ]).encode("utf-8")


class Status(NamedTuple):
    connected: bool = False
    chain: object = None       # 0x-hex chain id, or None
    account: object = None     # 0x address, or None (none selected)
    error: object = None       # short failure detail, or None


def parse_status(text) -> Status:
    """Parse qeth's batched ``[chainId, accounts]`` response text into a
    Status. ``connected`` is True only when the chain id came back without
    error — a failed id-1 means the link is broken; a missing account just
    means none is selected in qeth."""
    try:
        envs = json.loads(text)
    except (ValueError, TypeError) as e:
        return Status(error=str(e))
    if not isinstance(envs, list):
        envs = [envs]
    chain = account = error = None
    for env in envs:
        if not isinstance(env, dict):
            continue
        rid = env.get("id")
        if env.get("error"):
            if rid == 1:       # chain id failing means the link is broken
                error = env["error"].get("message", "error")
            continue
        result = env.get("result")
        if rid == 1:
            chain = result
        elif rid == 2 and isinstance(result, list) and result:
            account = result[0]
    return Status(connected=error is None and chain is not None,
                  chain=chain, account=account, error=error)
