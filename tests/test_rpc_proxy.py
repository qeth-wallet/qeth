"""The JSON-RPC proxy's handling of bad upstream responses (qeth.rpc._proxy).

A flaky upstream (DRPC under load, a connection cut mid-body, a Cloudflare
error page) can return a non-JSON / truncated body. The proxy must turn that
into a clean JSON-RPC error + a host-failure cooldown — not crash the dispatch
with a JSONDecodeError traceback (the production bug this covers).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

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


def test_proxy_fails_over_to_fallback_on_transport_error():
    """A transport blip (e.g. a DNS timeout) on the primary RPC fails over to
    the chain's fallback_rpcs instead of failing the dapp — the DNS-outage fix."""
    from aiohttp import ServerDisconnectedError
    chain = DEFAULT_CHAINS[0]
    primary, fallback = chain.rpc_url, chain.fallback_rpcs[0]

    class _FailResp:
        async def __aenter__(self):
            raise ServerDisconnectedError()

        async def __aexit__(self, *exc):
            return False

    class _FailoverClient:
        closed = False

        def __init__(self):
            self.tried = []

        def post(self, url, *a, **k):
            self.tried.append(url)
            if url == primary:
                return _FailResp()
            return _FakeResp(200, '{"jsonrpc":"2.0","id":1,"result":"0xbeef"}')

    store = MagicMock()
    store.current_chain.return_value = chain
    store.chains = [chain]
    srv = RpcServer(store)
    client = _FailoverClient()
    srv._client = client

    assert asyncio.run(srv._proxy("eth_call", [])) == "0xbeef"
    assert client.tried == [primary, fallback]  # primary, then failed over


# --- provider-side failures behind a parseable body -------------------------
#
# Captured live from eth.drpc.org under load (2026-06-11): the free-tier
# limiter answers HTTP 408 with a VALID JSON-RPC error — the old proxy
# parsed it, saw "error", and forwarded DRPC's upsell to the dapp as a
# final answer instead of failing over.

DRPC_408_BODY = ('{"id":1,"jsonrpc":"2.0","error":{"message":"Request timeout'
                 ' on the free tier, please upgrade your tier to the paid one'
                 '","code":30}}')
OK_BODY = '{"jsonrpc":"2.0","id":1,"result":"0xbeef"}'


class _PerUrlClient:
    """Maps each URL to a fixed (status, body) response; records the order."""
    closed = False

    def __init__(self, responses):
        self._responses = responses
        self.tried: list = []

    def post(self, url, *a, **k):
        self.tried.append(url)
        status, body = self._responses[url]
        return _FakeResp(status, body)


def _per_url_server(responses):
    store = MagicMock()
    store.current_chain.return_value = DEFAULT_CHAINS[0]
    store.chains = [DEFAULT_CHAINS[0]]
    srv = RpcServer(store)
    srv._client = _PerUrlClient(responses)
    return srv


def test_proxy_fails_over_on_drpc_free_tier_408():
    chain = DEFAULT_CHAINS[0]
    srv = _per_url_server({chain.rpc_url: (408, DRPC_408_BODY),
                           chain.fallback_rpcs[0]: (200, OK_BODY)})
    assert asyncio.run(srv._proxy("eth_call", [])) == "0xbeef"
    assert srv._client.tried == [chain.rpc_url, chain.fallback_rpcs[0]]
    # The 408 lands after a server-side stall — the host must go on cooldown
    # so the next request doesn't re-eat it.
    assert chain.rpc_url in srv._host_last_fail


def test_proxy_fails_over_on_rate_limit_error_body_with_200():
    chain = DEFAULT_CHAINS[0]
    limited = ('{"jsonrpc":"2.0","id":1,'
               '"error":{"code":-32005,"message":"rate limit exceeded"}}')
    srv = _per_url_server({chain.rpc_url: (200, limited),
                           chain.fallback_rpcs[0]: (200, OK_BODY)})
    assert asyncio.run(srv._proxy("eth_call", [])) == "0xbeef"
    assert chain.rpc_url in srv._host_last_fail


def test_proxy_surfaces_limiter_error_when_all_providers_limited():
    chain = DEFAULT_CHAINS[0]
    srv = _per_url_server({chain.rpc_url: (408, DRPC_408_BODY),
                           chain.fallback_rpcs[0]: (408, DRPC_408_BODY)})
    with pytest.raises(RpcError) as ei:
        asyncio.run(srv._proxy("eth_call", []))
    assert "free tier" in str(ei.value)


def test_proxy_request_level_error_does_not_fail_over():
    """A revert is the chain's answer, not the provider's failure — forward
    it from the FIRST provider; the fallback must not even be tried."""
    chain = DEFAULT_CHAINS[0]
    revert = ('{"jsonrpc":"2.0","id":1,'
              '"error":{"code":3,"message":"execution reverted"}}')
    srv = _per_url_server({chain.rpc_url: (200, revert),
                           chain.fallback_rpcs[0]: (200, OK_BODY)})
    with pytest.raises(RpcError) as ei:
        asyncio.run(srv._proxy("eth_call", []))
    assert ei.value.code == 3
    assert srv._client.tried == [chain.rpc_url]
    assert not srv._host_last_fail


def test_proxy_plain_4xx_fails_over_without_cooldown():
    """A 400 is often per-method brokenness (DRPC's free Gnosis endpoint
    400s eth_call while serving everything else) — fail over, but don't
    cooldown the host: its other methods still work."""
    chain = DEFAULT_CHAINS[0]
    bad = ('{"jsonrpc":"2.0","id":1,'
           '"error":{"code":-32601,"message":"can\'t route this method"}}')
    srv = _per_url_server({chain.rpc_url: (400, bad),
                           chain.fallback_rpcs[0]: (200, OK_BODY)})
    assert asyncio.run(srv._proxy("eth_call", [])) == "0xbeef"
    assert chain.rpc_url not in srv._host_last_fail


# --- broadcasts: pinned to the user's chosen RPC, never a fallback ---------

class _RecordingClient:
    """Records every POSTed url; raises a transport error for ``fail_urls``."""
    closed = False

    def __init__(self, body, fail_urls=()):
        self._body = body
        self._fail = set(fail_urls)
        self.tried: list = []

    def post(self, url, *a, **k):
        self.tried.append(url)
        if url in self._fail:
            class _Boom:
                async def __aenter__(self):
                    from aiohttp import ServerDisconnectedError
                    raise ServerDisconnectedError()

                async def __aexit__(self, *exc):
                    return False
            return _Boom()
        return _FakeResp(200, self._body)


def _recording_server(body, fail_urls=()):
    store = MagicMock()
    store.current_chain.return_value = DEFAULT_CHAINS[0]
    store.chains = [DEFAULT_CHAINS[0]]
    srv = RpcServer(store)
    srv._client = _RecordingClient(body, fail_urls)
    return srv


def test_proxy_broadcast_goes_only_to_primary():
    srv = _recording_server('{"jsonrpc":"2.0","id":1,"result":"0xhash"}')
    out = asyncio.run(srv._proxy(
        "eth_sendRawTransaction", ["0xraw"], broadcast=True))
    assert out == "0xhash"
    assert srv._client.tried == [DEFAULT_CHAINS[0].rpc_url]


def test_proxy_broadcast_does_not_fail_over():
    """Primary down → the broadcast ERRORS; the signed tx must never be
    relayed to a fallback (it would leak a private / MEV-protected tx into a
    public mempool and override the user's endpoint choice)."""
    chain = DEFAULT_CHAINS[0]
    srv = _recording_server('{"jsonrpc":"2.0","id":1,"result":"0xhash"}',
                            fail_urls={chain.rpc_url})
    with pytest.raises(Exception):
        asyncio.run(srv._proxy(
            "eth_sendRawTransaction", ["0xraw"], broadcast=True))
    assert srv._client.tried == [chain.rpc_url]   # fallbacks untouched


def test_proxy_broadcast_ignores_fail_fast_cooldown():
    """With no alternative allowed, a broadcast must TRY a primary that's on
    the fail-fast cooldown rather than skip straight to 'unreachable'."""
    chain = DEFAULT_CHAINS[0]
    srv = _recording_server('{"jsonrpc":"2.0","id":1,"result":"0xhash"}')
    srv._host_last_fail[chain.rpc_url] = 1e18     # permanently "just failed"
    out = asyncio.run(srv._proxy(
        "eth_sendRawTransaction", ["0xraw"], broadcast=True))
    assert out == "0xhash"
    assert srv._client.tried == [chain.rpc_url]


def test_handle_one_routes_raw_broadcast_with_broadcast_flag():
    """The dispatch special-cases eth_sendRawTransaction → broadcast=True."""
    srv = _recording_server('{"jsonrpc":"2.0","id":1,"result":"0xhash"}')
    calls: dict = {}

    async def fake_proxy(method, params, origin=None, broadcast=False):
        calls.update(method=method, broadcast=broadcast)
        return "0xhash"

    srv._proxy = fake_proxy
    asyncio.run(srv._handle_one(
        {"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction",
         "params": ["0xraw"]}, None))
    assert calls == {"method": "eth_sendRawTransaction", "broadcast": True}


class TestGetLogsChunking:
    """eth_getLogs wider than a free RPC's range cap is split into
    <=_MAX_LOG_BLOCKS-block chunks (each still failed-over via _proxy), and the
    per-chunk log lists are concatenated (chunks are disjoint + ascending)."""

    def _server(self):
        store = MagicMock()
        store.chains = DEFAULT_CHAINS
        store.current_chain.return_value = DEFAULT_CHAINS[0]
        return RpcServer(store)

    def test_narrow_range_is_forwarded_unchunked(self):
        server = self._server()
        server._proxy = AsyncMock(return_value=[])
        asyncio.run(server._get_logs_chunked(
            [{"fromBlock": "0x64", "toBlock": "0xc8"}], None))  # 100..200
        assert server._proxy.await_count == 1
        assert server._proxy.await_args.args[0] == "eth_getLogs"

    def test_wide_range_is_chunked_and_merged(self):
        server = self._server()
        seen = []

        async def fake_proxy(method, params, origin=None, **kw):
            f, t = (int(params[0]["fromBlock"], 16),
                    int(params[0]["toBlock"], 16))
            seen.append((f, t))
            return [{"range": [f, t]}]
        server._proxy = fake_proxy

        out = asyncio.run(server._get_logs_chunked(
            [{"fromBlock": "0x0", "toBlock": hex(25000)}], None))
        assert seen == [(0, 9999), (10000, 19999), (20000, 25000)]
        assert all(t - f <= 9999 for f, t in seen)   # never over 10000 blocks
        assert len(out) == 3                          # merged

    def test_latest_bound_is_resolved_then_chunked(self):
        server = self._server()
        seen = []

        async def fake_proxy(method, params, origin=None, **kw):
            if method == "eth_blockNumber":
                return hex(15000)
            seen.append((int(params[0]["fromBlock"], 16),
                         int(params[0]["toBlock"], 16)))
            return []
        server._proxy = fake_proxy

        asyncio.run(server._get_logs_chunked(
            [{"fromBlock": "0x0", "toBlock": "latest"}], None))
        assert seen == [(0, 9999), (10000, 15000)]

    def test_blockhash_filter_is_passthrough(self):
        server = self._server()
        server._proxy = AsyncMock(return_value=[])
        asyncio.run(server._get_logs_chunked(
            [{"blockHash": "0x" + "ab" * 32}], None))
        assert server._proxy.await_count == 1

    def test_absurd_range_is_forwarded_as_is(self):
        server = self._server()
        calls = []

        async def fake_proxy(method, params, origin=None, **kw):
            calls.append((method, params))
            return []
        server._proxy = fake_proxy

        # 0 .. 10_000_000 → past the 50-chunk cap → single forward, unchanged.
        asyncio.run(server._get_logs_chunked(
            [{"fromBlock": "0x0", "toBlock": hex(10_000_000)}], None))
        getlogs = [c for c in calls if c[0] == "eth_getLogs"]
        assert len(getlogs) == 1
        assert getlogs[0][1][0]["toBlock"] == hex(10_000_000)
