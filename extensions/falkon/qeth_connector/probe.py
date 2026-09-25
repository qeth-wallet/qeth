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
from urllib.parse import urlparse

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


def batch_body(origin=None) -> bytes:
    """One batched JSON-RPC request: the chain id (id 1) and account (id 2) —
    all a qeth before 0.25 answers — and ``qeth_status`` (id 3): the network
    selected in qeth and its account, and what the site at ``origin`` (the
    current tab) is presented. A single round-trip is more robust than
    separate requests through Qt's connection-reusing network manager."""
    return json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
        {"jsonrpc": "2.0", "id": 2, "method": "eth_accounts", "params": []},
        {"jsonrpc": "2.0", "id": 3, "method": "qeth_status",
         "params": [{"origin": origin}] if origin else []},
    ]).encode("utf-8")


class Status(NamedTuple):
    connected: bool = False
    chain: object = None       # 0x-hex chain id, or None
    account: object = None     # 0x address, or None (none selected)
    error: object = None       # short failure detail, or None
    wallet: object = None      # qeth_status's answer (dict), or None


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
    chain = account = error = wallet = None
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
        elif rid == 3 and isinstance(result, dict):
            # An error from an older qeth: the views fall back to 1 + 2.
            wallet = result
    return Status(connected=error is None and chain is not None,
                  chain=chain, account=account, error=error, wallet=wallet)


def short(addr):
    """``0xbabe…9f67`` / ``TSzc…tfFL``."""
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else addr


def _network_name(chain):
    return (chain or {}).get("name") or chain_name((chain or {}).get("chainId"))


def selected(st):
    """``(network, account)`` selected in qeth — the account in that network's
    form (``T…`` on Tron). An older qeth only reports its EVM dapp chain."""
    wallet = st.wallet or {}
    if wallet.get("chain"):
        return _network_name(wallet["chain"]), wallet.get("account")
    return chain_name(st.chain), st.account


def site_line(st):
    """"This site (sun.io): Tron · TSzc…tfFL" — what the current tab is
    presented — or None (no http(s) tab, or an older qeth)."""
    site = (st.wallet or {}).get("site")
    if not isinstance(site, dict):
        return None
    host = urlparse(site.get("origin") or "").netloc
    account = short(site.get("account")) or "no account connected"
    return f"This site ({host}): {_network_name(site.get('chain'))} · {account}"


def origin_of(url):
    """The http(s) origin of a URL string, else None."""
    parsed = urlparse(url or "")
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return None
