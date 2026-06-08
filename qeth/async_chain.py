"""Async chain access for the live-update watcher.

The sync ``EthClient`` (``chain.py``) stays the workhorse for ordinary
request/response RPC. This module is the *async* transport behind the
WebSocket-driven watcher: ``AsyncWeb3`` over a ``WebSocketProvider`` (for
``eth_subscribe`` push) or an ``AsyncHTTPProvider`` (the http-poll
fallback), carrying the same plumbing the sync side hardened — the
``qeth/<version>`` User-Agent (DRPC's Cloudflare front needs it),
multi-RPC failover on transport errors, and the PoA extraData middleware.

web3.py 7.x async facts this relies on (validated live against Gnosis):
  - ``await w3.eth.subscribe("newHeads")`` returns a ``str`` id;
  - ``async for msg in w3.socket.process_subscriptions()`` yields
    ``{"subscription": id, "result": AttributeDict}`` — already *formatted*
    (block number is an ``int``, hashes are ``bytes``);
  - subscription results are formatted, but ``provider.make_request`` still
    returns *raw* hex — use it for receipts so they match the sync
    ``_confirmed_from_receipt`` handler rather than a web3 ``AttributeDict``.

Like ``chain.py``, the heavy web3/aiohttp imports are deferred to first use
(``_ensure_async_imports``) and the names are declared under TYPE_CHECKING
so mypy resolves them.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    import aiohttp
    from web3 import AsyncWeb3
    from web3.middleware import ExtraDataToPOAMiddleware
    from web3.providers.persistent import WebSocketProvider
    from web3.providers.rpc import AsyncHTTPProvider

from . import USER_AGENT
from .chain import _rpc_urls  # primary + ordered fallbacks (shared resolution)
from .chains import Chain, DEFAULT_CHAINS

log = logging.getLogger("qeth.async_chain")


def _ensure_async_imports() -> None:
    """Pull in the async web3/aiohttp stack on first use and stash the
    symbols in module globals, mirroring ``chain._ensure_heavy_imports``."""
    g = globals()
    if "AsyncWeb3" in g:
        return
    import aiohttp
    from web3 import AsyncWeb3
    from web3.middleware import ExtraDataToPOAMiddleware
    from web3.providers.persistent import WebSocketProvider
    from web3.providers.rpc import AsyncHTTPProvider
    g["aiohttp"] = aiohttp
    g["AsyncWeb3"] = AsyncWeb3
    g["ExtraDataToPOAMiddleware"] = ExtraDataToPOAMiddleware
    g["WebSocketProvider"] = WebSocketProvider
    g["AsyncHTTPProvider"] = AsyncHTTPProvider


def is_ws_url(url: str) -> bool:
    """True for ``ws://`` / ``wss://`` — the subscription transport."""
    return url.lower().startswith(("ws://", "wss://"))


def _http_provider(urls: list[str], *, timeout: float) -> "AsyncHTTPProvider":
    """An ``AsyncHTTPProvider`` over ``urls`` (primary + fallbacks) carrying
    the qeth UA, whose ``make_request`` rotates through the endpoints on a
    *transport* error and then sticks to whichever answered. The async/
    aiohttp mirror of ``chain._failover_provider``. A JSON-RPC *error*
    response (a revert, a rejected request) is a valid answer, not a
    transport failure, so it does NOT trigger failover. With one URL it's a
    plain ``AsyncHTTPProvider``."""
    request_kwargs = {
        "headers": {"User-Agent": USER_AGENT},
        "timeout": aiohttp.ClientTimeout(total=timeout),
    }
    members = [AsyncHTTPProvider(u, request_kwargs=request_kwargs) for u in urls]
    provider = members[0]
    if len(members) == 1:
        return provider
    state = {"i": 0}

    async def make_request(method: Any, params: Any) -> Any:
        last: Optional[Exception] = None
        for offset in range(len(members)):
            i = (state["i"] + offset) % len(members)
            try:
                # Call the *class* coroutine on the member so the primary's
                # patched make_request (this function) isn't re-entered.
                resp = await AsyncHTTPProvider.make_request(
                    members[i], method, params)
                if i != state["i"]:
                    log.debug("async rpc failover -> %s", urls[i])
                state["i"] = i
                return resp
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last = exc
        assert last is not None  # len(members) >= 2, so the loop ran
        raise last

    setattr(provider, "make_request", make_request)
    return provider


def _ws_provider(url: str) -> "WebSocketProvider":
    """A ``WebSocketProvider`` with the qeth UA on the upgrade handshake —
    DRPC's Cloudflare front rejects default UAs on the ws upgrade too.

    web3.py 7.x drives the *legacy* ``websockets`` client, whose header
    kwarg is ``extra_headers`` (a dict); the modern client renamed it
    ``additional_headers``, which here falls through to the asyncio loop's
    ``create_connection`` and raises. Pinned to ``extra_headers`` for the
    locked web3/websockets; revisit if web3 moves off the legacy client."""
    return WebSocketProvider(
        url,
        websocket_kwargs={"extra_headers": {"User-Agent": USER_AGENT}},
    )


def make_async_web3(
    url: str, *, fallbacks: Optional[list[str]] = None, timeout: float = 15.0,
) -> "AsyncWeb3":
    """Build an ``AsyncWeb3`` for ``url``: a ``WebSocketProvider`` for a
    ws/wss URL (so ``eth_subscribe`` works), else an ``AsyncHTTPProvider``
    failover stack over ``url`` + ``fallbacks``. The PoA extraData
    middleware is injected unconditionally, exactly as on the sync side
    (harmless on non-PoA chains, required on BSC/Polygon).

    Note the ws path is a single connection — for ws the *watcher* retries
    alternative endpoints across reconnects; failover-within-make_request is
    an http-only concern."""
    _ensure_async_imports()
    if is_ws_url(url):
        provider: Any = _ws_provider(url)
    else:
        provider = _http_provider([url, *(fallbacks or [])], timeout=timeout)
    w3 = AsyncWeb3(provider)
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def ws_urls_for(chain: Chain) -> list[str]:
    """The chain's ws endpoints to try, in order:

      1. the chain's explicit ``ws_url`` — or, for a custom-RPC override of a
         known chain (one with no ``ws_url`` of its own), the matching
         ``DEFAULT_CHAINS`` entry's, so overriding Ethereum's http RPC still
         gets ws;
      2. else derived from the http primary+fallbacks (``https://host`` →
         ``wss://host``), which holds when the host serves ws on the same
         origin (DRPC, publicnode).

    A chain with no resolvable / working ws simply fails to connect and the
    watcher falls back to http polling (the legacy timers)."""
    explicit = tuple(chain.ws_url)
    if not explicit:
        default = next((c for c in DEFAULT_CHAINS
                        if c.chain_id == chain.chain_id), None)
        explicit = tuple(default.ws_url) if default else ()
    if explicit:
        candidates: list[str] = list(explicit)
    else:
        candidates = []
        for u in _rpc_urls(chain):
            if u.startswith("https://"):
                candidates.append("wss://" + u[len("https://"):])
            elif u.startswith("http://"):
                candidates.append("ws://" + u[len("http://"):])
    seen: set[str] = set()
    out: list[str] = []
    for ws in candidates:
        if ws and ws not in seen:
            seen.add(ws)
            out.append(ws)
    return out
