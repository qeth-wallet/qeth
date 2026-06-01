"""CORS / Private Network Access headers on the JSON-RPC server.

The Falkon connector (integrations/falkon/) injects its provider into the
page's own origin, so a dapp served over public HTTPS reaches the loopback
server directly — and Chromium's Private Network Access blocks that unless
the response grants it. These tests lock the granting headers in.
"""

import asyncio

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from qeth.rpc import _cors


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
