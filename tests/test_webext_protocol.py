"""End-to-end WS protocol contract the browser extension's background depends
on, exercised against the REAL ``RpcServer`` over a loopback socket (no
browser).

The extension multiplexes every tab and frame over a SINGLE WebSocket to
127.0.0.1:1248, tagging each JSON-RPC message with the dapp's ``__frameOrigin``.
This pins the behaviour that makes that safe:

- two frames on the same socket each subscribe to ``chainChanged`` and get
  DISTINCT subscription ids (a sub_type-keyed server map collapsed them);
- a ``wallet_switchEthereumChain`` from frame A pushes only to A's
  subscription; frame B is untouched;
- a UI-driven chain flip (``set_rpc_chain``) pushes only to the un-pinned
  frame B (A pinned itself by switching);
- ``eth_chainId`` diverges per origin over the one socket;
- ``eth_unsubscribe`` stops that frame's pushes.

Loopback only, so the hermeticity guard permits it without a ``network`` mark.
"""

import asyncio
from types import SimpleNamespace

import aiohttp
import pytest

from qeth.rpc import RpcServer

A = "https://a.example"
B = "https://b.example"


def _store():
    chains = [SimpleNamespace(chain_id=1, is_evm=True),
              SimpleNamespace(chain_id=10, is_evm=True)]
    return SimpleNamespace(
        current_chain=lambda: chains[0],
        dapp_chain=lambda: chains[0],
        chains=chains,
        default_account="0x" + "11" * 20,
    )


class _Client:
    """A single WS relaying several logical frames, like the extension's
    background. Reads are demultiplexed: responses by id, pushes collected."""

    def __init__(self, ws):
        self._ws = ws
        self._next = 0

    async def _send(self, method, params=None, *, origin=None, rid=None):
        self._next += 1
        msg = {"jsonrpc": "2.0", "id": self._next if rid is None else rid,
               "method": method, "params": params or []}
        if origin is not None:
            msg["__frameOrigin"] = origin
        await self._ws.send_json(msg)
        return msg["id"]

    async def call(self, method, params=None, *, origin=None, rid=None,
                   timeout=2.0):
        """Send a request; return (result, [pushes seen before the reply])."""
        want = await self._send(method, params, origin=origin, rid=rid)
        pushes = []
        while True:
            m = (await self._ws.receive(timeout=timeout)).json()
            if m.get("method") == "eth_subscription":
                pushes.append((m["params"]["subscription"], m["params"]["result"]))
                continue
            if m.get("id") == want:
                return m.get("result"), pushes

    async def next_push(self, *, timeout=2.0):
        m = (await self._ws.receive(timeout=timeout)).json()
        assert m.get("method") == "eth_subscription", m
        return m["params"]["subscription"], m["params"]["result"]

    async def expect_silence(self, *, timeout=0.4):
        with pytest.raises(asyncio.TimeoutError):
            await self._ws.receive(timeout=timeout)


@pytest.fixture
def running_server():
    server = RpcServer(_store(), port=0)
    server.start()
    assert server._error is None, server._error
    host, port = server._runner.addresses[0][:2]
    yield server, f"http://{host}:{port}/"
    server.stop()


def test_multiplexed_per_origin_scoping(running_server):
    server, url = running_server

    async def scenario():
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url) as ws:
                c = _Client(ws)

                # Two frames subscribe to chainChanged over one socket, with
                # COLLIDING page ids (rid=1) — the server must echo the id and
                # still hand out distinct subscription ids.
                sub_a, _ = await c.call(
                    "eth_subscribe", ["chainChanged"], origin=A, rid=1)
                sub_b, _ = await c.call(
                    "eth_subscribe", ["chainChanged"], origin=B, rid=1)
                assert sub_a and sub_b and sub_a != sub_b

                # (a) frame A switches chain → chainChanged pushed to A only.
                # The push precedes the switch response on the socket.
                res, pushes = await c.call(
                    "wallet_switchEthereumChain", [{"chainId": "0xa"}],
                    origin=A, rid=7)
                assert res is None
                assert pushes == [(sub_a, "0xa")]

                # (b) UI chain flip → only the un-pinned frame B is notified
                # (A pinned itself in step a). set_rpc_chain runs on the
                # server's own loop, like the Qt toolbar callback.
                server.set_rpc_chain(1)
                assert await c.next_push() == (sub_b, "0x1")
                await c.expect_silence()      # A gets nothing

                # (c) eth_chainId diverges per origin over the one socket.
                a_chain, _ = await c.call("eth_chainId", origin=A, rid=1)
                b_chain, _ = await c.call("eth_chainId", origin=B, rid=1)
                assert (a_chain, b_chain) == ("0xa", "0x1")

                # (d) B unsubscribes → a subsequent UI flip reaches nobody.
                await c.call("eth_unsubscribe", [sub_b], origin=B, rid=1)
                server.set_rpc_chain(1)
                await c.expect_silence()

    asyncio.run(scenario())
