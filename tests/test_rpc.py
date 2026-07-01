"""CORS / Private Network Access headers on the JSON-RPC server.

The Falkon connector (integrations/falkon/) injects its provider into the
page's own origin, so a dapp served over public HTTPS reaches the loopback
server directly — and Chromium's Private Network Access blocks that unless
the response grants it. These tests lock the granting headers in.
"""

import asyncio
from unittest.mock import MagicMock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from qeth.rpc import RpcServer, _cors


def _run(coro):
    return asyncio.run(coro)


async def _ok_handler(request):
    return web.Response(text="ok")


class TestCorsHeaders:
    def test_options_preflight_grants_private_network(self):
        req = make_mocked_request(
            "OPTIONS", "/",
            headers={
                "Origin": "https://app.uniswap.org",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        resp = _run(_cors(req, _ok_handler))
        assert resp.headers["Access-Control-Allow-Private-Network"] == "true"
        # Echoes the requesting origin (not "*"), which is what lets the
        # credentialed/specific-origin path work and what qeth keys its
        # per-origin chain tracking on.
        assert resp.headers["Access-Control-Allow-Origin"] == "https://app.uniswap.org"

    def test_actual_request_carries_private_network_header(self):
        req = make_mocked_request(
            "POST", "/", headers={"Origin": "https://app.aave.com"},
        )
        resp = _run(_cors(req, _ok_handler))
        assert resp.headers["Access-Control-Allow-Private-Network"] == "true"
        assert resp.headers["Access-Control-Allow-Origin"] == "https://app.aave.com"

    def test_origin_falls_back_to_wildcard_when_absent(self):
        req = make_mocked_request("POST", "/")
        resp = _run(_cors(req, _ok_handler))
        assert resp.headers["Access-Control-Allow-Origin"] == "*"
        assert resp.headers["Access-Control-Allow-Private-Network"] == "true"


def test_eth_accounts_never_returns_a_null_entry():
    """eth_accounts reads default_account ONCE. Reading it twice let the GUI
    thread null it (account removed) between the truthiness check and the
    build, yielding [None] — dapps treat entries as address strings and choke
    on a null. Empty must be []. Regression for 1f."""
    store = MagicMock()
    server = RpcServer(store, port=0)

    store.default_account = None
    assert _run(server._dispatch("eth_accounts", [])) == []

    store.default_account = "0x" + "11" * 20
    assert _run(server._dispatch("eth_accounts", [])) == ["0x" + "11" * 20]


def test_rpc_stop_joins_background_thread():
    """stop() now blocks until the server is actually down: the asyncio
    loop stops and the background thread is joined (not left dangling)."""
    server = RpcServer(MagicMock(), port=0)
    server.start()
    thread = server._thread
    assert thread is not None
    assert thread.is_alive()

    server.stop()

    assert not thread.is_alive()


def test_shutdown_closes_ws_clients_so_cleanup_does_not_block():
    """A connected WS client is closed up front during shutdown — otherwise
    the site's cleanup waits its shutdown_timeout for the open socket, which
    is the slow-close hang. _shutdown also clears the WS bookkeeping."""
    from unittest.mock import AsyncMock, MagicMock

    server = RpcServer(MagicMock())
    ws = MagicMock(close=AsyncMock())
    server._ws_clients = {ws}
    server._ws_subscriptions = {ws: {"chainChanged": "0x1"}}
    server._ws_origin = {ws: "https://app.example"}
    server._client = None
    server._runner = None

    asyncio.run(server._shutdown())

    ws.close.assert_awaited_once()
    assert server._ws_clients == set()
    assert server._ws_subscriptions == {}
    assert server._ws_origin == {}
