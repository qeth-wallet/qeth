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


def test_ws_slow_request_does_not_head_of_line_block(monkeypatch):
    """A slow ws handler (an unbounded signing prompt) must not stall other
    requests on the same socket: each message is dispatched as its own task, so
    a later FAST request replies while the slow one is still pending (5a). With
    the old serial loop the read loop blocks on the slow handler and
    _ws_handler never returns — wait_for turns that revert into a clean fail."""
    import json
    from types import SimpleNamespace
    from aiohttp import WSMsgType

    server = RpcServer(MagicMock(), port=0)
    sent: list = []

    async def scenario():
        slow_release = asyncio.Event()

        async def fake_handle_one(req, origin=None, ws=None):
            if req.get("id") == "slow":
                await slow_release.wait()      # blocks until we let it go
                return {"jsonrpc": "2.0", "id": "slow", "result": "S"}
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": "F"}
        monkeypatch.setattr(server, "_handle_one", fake_handle_one)

        messages = [
            SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(
                {"jsonrpc": "2.0", "id": "slow", "method": "m"})),
            SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(
                {"jsonrpc": "2.0", "id": "fast", "method": "m"})),
        ]

        class FakeWS:
            closed = False

            async def prepare(self, request):
                pass

            def __aiter__(self):
                return self

            async def __anext__(self):
                if messages:
                    return messages.pop(0)
                raise StopAsyncIteration

            async def send_str(self, s):
                sent.append(json.loads(s))

        monkeypatch.setattr("qeth.rpc.web.WebSocketResponse", FakeWS)
        req = make_mocked_request("GET", "/", headers={})
        await asyncio.wait_for(server._ws_handler(req), timeout=5)
        for _ in range(10):               # let the dispatched fast task run
            await asyncio.sleep(0)
        ids = [s.get("id") for s in sent]
        assert "fast" in ids and "slow" not in ids   # fast replied while slow blocked
        slow_release.set()
        await asyncio.gather(*server._ws_tasks)       # let the slow one finish
        assert "slow" in [s.get("id") for s in sent]

    asyncio.run(scenario())
