"""The JSON-RPC proxy's handling of bad upstream responses (qeth.rpc._proxy).

A flaky upstream (DRPC under load, a connection cut mid-body, a Cloudflare
error page) can return a non-JSON / truncated body. The proxy must turn that
into a clean JSON-RPC error + a host-failure cooldown — not crash the dispatch
with a JSONDecodeError traceback (the production bug this covers).
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from qeth.chains import DEFAULT_CHAINS
from qeth.rpc import RpcError, RpcServer


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    closed = False

    def __init__(self, resp):
        self._resp = resp

    def post(self, *a, **k):
        return self._resp


def _server(body, status=200):
    store = MagicMock()
    store.current_chain.return_value = DEFAULT_CHAINS[0]
    store.chains = [DEFAULT_CHAINS[0]]
    srv = RpcServer(store)
    srv._client = _FakeClient(_FakeResp(status, body))
    return srv


def test_proxy_truncated_json_raises_rpcerror_not_decodeerror():
    # the production shape: a body cut off mid-JSON
    srv = _server('{"jsonrpc":"2.0","id":1,"resul')
    with pytest.raises(RpcError) as ei:
        asyncio.run(srv._proxy("eth_getLogs", []))
    assert ei.value.code == -32603
    assert srv._host_last_fail, "failing host should go on the fail-fast cooldown"


def test_proxy_html_error_page_raises_rpcerror():
    srv = _server("<html><body>error code: 1010</body></html>", status=403)
    with pytest.raises(RpcError):
        asyncio.run(srv._proxy("eth_getLogs", []))


def test_proxy_valid_json_returns_result_and_clears_cooldown():
    srv = _server('{"jsonrpc":"2.0","id":1,"result":"0xdead"}')
    assert asyncio.run(srv._proxy("eth_blockNumber", [])) == "0xdead"
    assert not srv._host_last_fail


def test_proxy_upstream_json_error_is_surfaced():
    srv = _server('{"jsonrpc":"2.0","id":1,'
                  '"error":{"code":-32000,"message":"boom"}}')
    with pytest.raises(RpcError) as ei:
        asyncio.run(srv._proxy("eth_call", []))
    assert ei.value.code == -32000
