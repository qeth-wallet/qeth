"""CORS / Private Network Access headers on the JSON-RPC server.

The Falkon connector (extensions/falkon/) injects its provider into the
page's own origin, so a dapp served over public HTTPS reaches the loopback
server directly — and Chromium's Private Network Access blocks that unless
the response grants it. These tests lock the granting headers in.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from qeth.rpc import RpcError, RpcServer, _cors


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
    server._ws_subscriptions = {
        ws: {"0x1": ("chainChanged", "https://app.example")}
    }
    server._client = None
    server._runner = None

    asyncio.run(server._shutdown())

    ws.close.assert_awaited_once()
    assert server._ws_clients == set()
    assert server._ws_subscriptions == {}


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


class _CaptureWS:
    """Minimal WS stand-in that records the JSON payloads sent to it."""

    closed = False

    def __init__(self):
        self.sent: list = []

    async def send_str(self, s):
        import json
        self.sent.append(json.loads(s))


def _pushes(ws):
    """The (subscription id, result) pairs pushed to a capture ws."""
    return [
        (m["params"]["subscription"], m["params"]["result"])
        for m in ws.sent
        if m.get("method") == "eth_subscription"
    ]


class TestPerSubscriptionScoping:
    """One multiplexed socket (a browser extension relaying every tab over a
    single WS) must hold many same-type subscriptions, each scoped to its own
    dapp origin. Keying by sub_type — or scoping on the socket's handshake
    origin — collapsed them / mis-routed every push."""

    def _server(self):
        store = MagicMock()
        store.current_chain.return_value = SimpleNamespace(chain_id=1)
        store.dapp_chain.return_value = SimpleNamespace(chain_id=1)
        return RpcServer(store, port=0)

    def test_same_type_subscriptions_coexist_on_one_socket(self):
        server = self._server()
        ws = _CaptureWS()
        a = server._register_subscription(ws, "chainChanged", "https://a.example")
        b = server._register_subscription(ws, "chainChanged", "https://b.example")
        assert a != b
        # Both survive — a sub_type-keyed map would have dropped the first.
        assert set(server._ws_subscriptions[ws]) == {a, b}

    def test_only_origin_reaches_just_that_subscription(self):
        server = self._server()
        ws = _CaptureWS()
        a = server._register_subscription(ws, "chainChanged", "https://a.example")
        server._register_subscription(ws, "chainChanged", "https://b.example")
        _run(server._broadcast_event(
            "chainChanged", "0xa", only_origin="https://a.example"))
        # Only origin A's subscription id is pushed; B is untouched.
        assert _pushes(ws) == [(a, "0xa")]

    def test_only_unscoped_skips_a_pinned_origin(self):
        server = self._server()
        ws = _CaptureWS()
        pinned = server._register_subscription(
            ws, "chainChanged", "https://pinned.example")
        free = server._register_subscription(
            ws, "chainChanged", "https://free.example")
        # pinned.example switched its own chain → has an override.
        server._rpc_chain_id_by_origin["https://pinned.example"] = 10
        _run(server._broadcast_event(
            "chainChanged", "0x1", only_unscoped=True))
        # The UI-driven push reaches only the un-pinned subscription.
        assert _pushes(ws) == [(free, "0x1")]
        assert pinned not in [sid for sid, _ in _pushes(ws)]

    def test_unregister_removes_only_that_sub(self):
        server = self._server()
        ws = _CaptureWS()
        a = server._register_subscription(ws, "chainChanged", "https://a.example")
        b = server._register_subscription(ws, "accountsChanged", "https://a.example")
        server._unregister_subscription(ws, a)
        assert set(server._ws_subscriptions[ws]) == {b}
        _run(server._broadcast_event("chainChanged", "0x5"))
        assert _pushes(ws) == []      # the removed sub gets nothing


class TestWalletMethodsNeverReachTheChain:
    """A wallet-namespaced method addresses the WALLET, not the node.

    The Frame Companion extension made the leak concrete: on every
    (re)connect it asks for ``wallet_getEthereumChains`` to build its chain
    menu, and a ``chrome.alarms`` timer pings ``web3_clientVersion`` every
    30 s as a liveness check. Neither was handled here, so both fell through
    to _proxy and hit the user's Ethereum RPC — which can't answer either
    (and, on a 4xx, only after walking every fallback endpoint for that
    chain). MV3 service workers restart constantly, so "on connect" is often.
    """

    def _server(self, proxied):
        from qeth.chains import DEFAULT_CHAINS
        store = MagicMock()
        store.current_chain.return_value = DEFAULT_CHAINS[0]
        store.dapp_chain.return_value = DEFAULT_CHAINS[0]
        store.chains = DEFAULT_CHAINS

        server = RpcServer(store, port=0)

        async def fake_proxy(method, params, origin=None, broadcast=False):
            proxied.append((method, params))
            return "UPSTREAM"

        server._proxy = fake_proxy
        return server

    def _dispatch(self, server, method, params):
        return _run(server._dispatch(
            method, params, origin="chrome-extension://frame", ws=_CaptureWS(),
        ))

    def test_get_ethereum_chains_answered_locally_in_frames_shape(self):
        proxied = []
        server = self._server(proxied)
        chains = self._dispatch(server, "wallet_getEthereumChains", [])
        assert proxied == []
        ids = [c["chainId"] for c in chains]
        assert 1 in ids and 10 in ids
        eth = next(c for c in chains if c["chainId"] == 1)
        # Frame's field set — connectors read these by name.
        assert eth["networkId"] == 1
        assert eth["name"] == "Ethereum"
        assert eth["nativeCurrency"] == {
            "name": "ETH", "symbol": "ETH", "decimals": 18}
        assert eth["explorers"] == [{"url": "https://etherscan.io"}]
        assert eth["connected"] is True

    def test_chain_on_fail_fast_cooldown_reports_disconnected(self):
        server = self._server([])
        server._loop = asyncio.new_event_loop()
        try:
            # Every endpoint Ethereum has just failed → not connected.
            eth = server.store.chains[0]
            now = server._loop.time()
            for url in (eth.rpc_url, *eth.fallback_rpcs):
                server._host_last_fail[url] = now
            chains = server._ethereum_chains()
            assert next(c for c in chains if c["chainId"] == 1)["connected"] is False
            # A chain whose endpoints are fine still reads connected.
            assert next(c for c in chains if c["chainId"] == 10)["connected"] is True
        finally:
            server._loop.close()
            server._loop = None

    def test_client_version_reports_qeth_not_the_node(self):
        import qeth
        proxied = []
        server = self._server(proxied)
        assert self._dispatch(server, "web3_clientVersion", []) == \
            f"qeth/v{qeth.__version__}"
        assert proxied == []

    def test_no_wallet_namespaced_method_is_ever_proxied(self):
        proxied = []
        server = self._server(proxied)
        # A deliberately broad sweep: handled or refused, never forwarded.
        for method in (
            "wallet_getEthereumChains", "wallet_getAssets", "wallet_watchAsset",
            "wallet_getCapabilities", "wallet_sendCalls", "wallet_getCallsStatus",
            "wallet_showCallsStatus", "wallet_invented_tomorrow",
            "frame_summon", "metamask_getProviderState",
        ):
            try:
                self._dispatch(server, method, [])
            except RpcError as e:
                assert e.code == -32601, method
        assert proxied == [], "wallet-namespaced call reached the chain RPC"

    def test_frame_wallet_event_subscriptions_are_registered_not_proxied(self):
        proxied = []
        server = self._server(proxied)
        # ethereum-provider subscribes to each event it has a listener for;
        # the Frame extension listens for chainsChanged. Proxied, that spent
        # a chain request per connect and never delivered an event.
        for sub_type in ("chainsChanged", "assetsChanged",
                         "accountsChanged", "chainChanged", "networkChanged"):
            sub_id = self._dispatch(server, "eth_subscribe", [sub_type])
            assert sub_id.startswith("0x"), sub_type
        assert proxied == []

    def test_node_subscriptions_still_proxy(self):
        proxied = []
        server = self._server(proxied)
        assert self._dispatch(server, "eth_subscribe", ["newHeads"]) == "UPSTREAM"
        assert proxied == [("eth_subscribe", ["newHeads"])]

    def test_unknown_subscription_type_is_refused_not_proxied(self):
        proxied = []
        server = self._server(proxied)
        with pytest.raises(RpcError) as e:
            self._dispatch(server, "eth_subscribe", ["notAThing"])
        assert e.value.code == -32601
        assert proxied == []


def test_chains_changed_broadcast_carries_the_chain_list():
    """``chainsChanged`` fires from the Qt thread (no running asyncio loop
    there), so building the payload must not reach for get_event_loop()."""
    from qeth.chains import DEFAULT_CHAINS
    store = MagicMock()
    store.current_chain.return_value = DEFAULT_CHAINS[0]
    store.dapp_chain.return_value = DEFAULT_CHAINS[0]
    store.chains = DEFAULT_CHAINS
    server = RpcServer(store, port=0)

    ws = _CaptureWS()
    sub = server._register_subscription(ws, "chainsChanged", None)
    # _schedule_event no-ops without a running loop; drive the push directly.
    _run(server._broadcast_event("chainsChanged", server._ethereum_chains()))
    (sent_sub, chains), = _pushes(ws)
    assert sent_sub == sub
    assert [c["chainId"] for c in chains] == [c.chain_id for c in DEFAULT_CHAINS]


class TestOurOwnExtensionNeverMakesQethCallTheChain:
    """Every method qeth's own connectors originate on their OWN behalf —
    not relayed from a dapp — must be answerable locally.

    The Frame extension's leak (see TestWalletMethodsNeverReachTheChain) came
    from its liveness alarm picking ``web3_clientVersion``, a method the
    wallet server didn't handle. Ours picks ``eth_chainId`` instead, which
    qeth answers itself. This test keeps it that way: extensions/webext and
    extensions/falkon poll on a timer, so anything they send that qeth
    proxies becomes a permanent background load on the user's RPC.
    """

    # Self-originated (grep the connectors for `method:`):
    #   provider.js  _refreshState()  -> eth_chainId, eth_accounts (poll)
    #   provider.js  _startPush()     -> eth_subscribe x3
    #   background.js keepalive alarm -> eth_chainId
    #   background.js tab teardown    -> eth_unsubscribe
    #   probe.py     (Falkon)         -> eth_chainId, eth_accounts
    # Plus the wallet-state methods the provider forwards rather than
    # answering in-page.
    CONNECTOR_METHODS = [
        ("eth_chainId", []),
        ("eth_accounts", []),
        ("eth_requestAccounts", []),
        ("eth_coinbase", []),
        ("net_version", []),
        ("eth_subscribe", ["chainChanged"]),
        ("eth_subscribe", ["accountsChanged"]),
        ("eth_subscribe", ["networkChanged"]),
        ("eth_unsubscribe", ["0xdeadbeef"]),
        ("wallet_getPermissions", []),
        ("wallet_requestPermissions", [{"eth_accounts": {}}]),
    ]

    def test_none_of_them_reach_the_proxy(self):
        from qeth.chains import DEFAULT_CHAINS
        proxied = []
        store = MagicMock()
        store.current_chain.return_value = DEFAULT_CHAINS[0]
        store.dapp_chain.return_value = DEFAULT_CHAINS[0]
        store.chains = DEFAULT_CHAINS
        store.default_account = "0x" + "ab" * 20
        server = RpcServer(store, port=0)

        async def fake_proxy(method, params, origin=None, broadcast=False):
            proxied.append((method, params))
            return "UPSTREAM"

        server._proxy = fake_proxy

        for method, params in self.CONNECTOR_METHODS:
            result = _run(server._dispatch(
                method, params, origin="https://dapp.example", ws=_CaptureWS(),
            ))
            assert result != "UPSTREAM", f"{method} was proxied to the chain"

        assert proxied == [], (
            "our own connector's traffic reached the user's RPC provider: "
            f"{proxied}"
        )

    def test_eth_coinbase_is_the_users_account_not_the_nodes(self):
        from qeth.chains import DEFAULT_CHAINS
        store = MagicMock()
        store.current_chain.return_value = DEFAULT_CHAINS[0]
        store.dapp_chain.return_value = DEFAULT_CHAINS[0]
        store.chains = DEFAULT_CHAINS
        store.default_account = "0x" + "ab" * 20
        server = RpcServer(store, port=0)
        assert _run(server._dispatch("eth_coinbase", [])) == "0x" + "ab" * 20
        store.default_account = None
        assert _run(server._dispatch("eth_coinbase", [])) is None

    def test_personal_keystore_admin_never_reaches_the_users_provider(self):
        proxied = []
        store = MagicMock()
        store.current_chain.return_value = SimpleNamespace(chain_id=1)
        store.dapp_chain.return_value = SimpleNamespace(chain_id=1)
        store.chains = []
        server = RpcServer(store, port=0)

        async def fake_proxy(method, params, origin=None, broadcast=False):
            proxied.append(method)
            return "UPSTREAM"

        server._proxy = fake_proxy
        for method in ("personal_unlockAccount", "personal_newAccount",
                       "personal_listAccounts", "personal_ecRecover"):
            with pytest.raises(RpcError) as e:
                _run(server._dispatch(method, []))
            assert e.value.code == -32601
        assert proxied == []
