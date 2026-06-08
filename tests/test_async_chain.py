"""Unit tests for the async transport layer (qeth.async_chain).

Pure logic — scheme detection, ws-URL derivation, provider selection, and
the transport-failover rotation — tested without a live endpoint. The async
failover is exercised by driving the patched ``make_request`` through
``asyncio.run`` (no pytest-asyncio needed)."""

import asyncio

from qeth.chains import DEFAULT_CHAINS
from qeth import async_chain as ac


def test_is_ws_url():
    assert ac.is_ws_url("wss://eth.drpc.org")
    assert ac.is_ws_url("ws://localhost:8546")
    assert ac.is_ws_url("WSS://Eth.Example")          # case-insensitive
    assert not ac.is_ws_url("https://eth.drpc.org")
    assert not ac.is_ws_url("http://localhost:8545")


def test_ws_urls_for_derives_wss_from_http():
    gnosis = next(c for c in DEFAULT_CHAINS if c.chain_id == 100)
    urls = ac.ws_urls_for(gnosis)
    assert urls, "expected at least one derived ws endpoint"
    assert all(u.startswith(("wss://", "ws://")) for u in urls)
    # https primary -> wss on the same host, order preserved, deduped
    assert urls[0] == "wss://" + gnosis.rpc_url.split("://", 1)[1]
    assert len(urls) == len(set(urls))


def test_make_async_web3_picks_provider_by_scheme():
    ac._ensure_async_imports()
    w3_ws = ac.make_async_web3("wss://example.org")
    assert isinstance(w3_ws.provider, ac.WebSocketProvider)   # type: ignore[attr-defined]
    w3_http = ac.make_async_web3("https://example.org")
    assert isinstance(w3_http.provider, ac.AsyncHTTPProvider)  # type: ignore[attr-defined]


def test_single_url_http_provider_is_unwrapped():
    ac._ensure_async_imports()
    prov = ac._http_provider(["https://only.example"], timeout=5.0)
    # one URL -> plain AsyncHTTPProvider, no failover monkeypatch on the instance
    assert "make_request" not in prov.__dict__


def test_http_failover_rotates_on_transport_error(monkeypatch):
    ac._ensure_async_imports()
    calls: list[int] = []

    async def fake_make_request(self, method, params):
        calls.append(1)
        if len(calls) == 1:                       # first endpoint is "down"
            raise ac.aiohttp.ClientError("boom")  # type: ignore[attr-defined]
        return {"jsonrpc": "2.0", "id": 1, "result": "0x1"}

    monkeypatch.setattr(ac.AsyncHTTPProvider, "make_request", fake_make_request)  # type: ignore[attr-defined]
    prov = ac._http_provider(["https://a.example", "https://b.example"], timeout=5.0)
    resp = asyncio.run(prov.make_request("eth_blockNumber", []))
    assert resp["result"] == "0x1"
    assert len(calls) == 2                          # rotated past the dead one


def test_http_failover_reraises_when_all_dead(monkeypatch):
    ac._ensure_async_imports()

    async def always_down(self, method, params):
        raise ac.aiohttp.ClientError("down")        # type: ignore[attr-defined]

    monkeypatch.setattr(ac.AsyncHTTPProvider, "make_request", always_down)  # type: ignore[attr-defined]
    prov = ac._http_provider(["https://a.example", "https://b.example"], timeout=5.0)
    try:
        asyncio.run(prov.make_request("eth_blockNumber", []))
        assert False, "expected the transport error to propagate"
    except ac.aiohttp.ClientError:                  # type: ignore[attr-defined]
        pass
