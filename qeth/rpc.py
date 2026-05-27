import asyncio
import json
import logging
import threading
from typing import Any, Optional

from aiohttp import ClientSession, WSMsgType, web

from .chains import Chain
from .signing import (
    SignerBridge, SignerError, parse_send_transaction_params,
)

log = logging.getLogger("qeth.rpc")


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@web.middleware
async def _cors(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
    resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
    resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    return resp


class RpcServer:
    """JSON-RPC server on 127.0.0.1:1248 — HTTP and WebSocket on the same port,
    matching Frame's endpoint so the Frame browser extension can connect.

    Runs on its own asyncio loop in a background thread."""

    def __init__(self, store, host: str = "127.0.0.1", port: int = 1248,
                 signer_bridge: Optional[SignerBridge] = None):
        self.store = store
        self.host = host
        self.port = port
        # Optional: when None, signing methods still return -32601
        # (keeps tests that don't need a UI bridge working).
        self.signer_bridge = signer_bridge
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._runner: Optional[web.AppRunner] = None
        self._client: Optional[ClientSession] = None
        self._ready = threading.Event()
        self._error: Optional[str] = None
        # Live WS clients — used to push EIP-1193 events
        # (accountsChanged, chainChanged) to connected dapps when
        # the user changes them in the qeth UI. Lives on the
        # asyncio loop; broadcast_* methods marshal Qt-thread
        # calls through asyncio.run_coroutine_threadsafe.
        self._ws_clients: set[web.WebSocketResponse] = set()
        # Per-WS subscription map: ws → {sub_type: sub_id}.
        # Populated when a dapp calls eth_subscribe('accountsChanged')
        # or eth_subscribe('chainChanged'); used by _broadcast_event
        # to address the matching eth_subscription notifications.
        # Frame's protocol — without an active subscription the
        # extension simply ignores the push.
        self._ws_subscriptions: dict[
            web.WebSocketResponse, dict[str, str]
        ] = {}
        # The chain the RPC server reports to / routes for dapps —
        # SEPARATE from the wallet UI's chain. A dapp calling
        # ``wallet_switchEthereumChain`` updates this, NOT the
        # user's UI selection: the user keeps looking at whatever
        # chain they picked in the toolbar combo while the dapp
        # transacts on its own chain. Defaults to the store's
        # current chain at startup (a natural first guess) and
        # never persisted — dapp chain is session-only.
        self._rpc_chain_id: int = store.current_chain().chain_id

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="qeth-rpc", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)

    @property
    def error(self) -> Optional[str]:
        return self._error

    async def _shutdown(self) -> None:
        try:
            if self._client:
                await self._client.close()
            if self._runner:
                await self._runner.cleanup()
        finally:
            self._loop.stop()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            self._error = str(e)
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    async def _serve(self) -> None:
        self._client = ClientSession()
        app = web.Application(middlewares=[_cors])
        app.router.add_route("OPTIONS", "/{tail:.*}", lambda r: web.Response())
        app.router.add_post("/", self._http_handler)
        app.router.add_get("/", self._root_or_ws)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("qeth JSON-RPC listening on http(s)/ws://%s:%d", self.host, self.port)

    async def _root_or_ws(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._ws_handler(request)
        return web.Response(text="qeth JSON-RPC — POST JSON-RPC 2.0 here, or connect via WebSocket")

    async def _http_handler(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            )
        # The HTTP Origin header on a browser-extension-mediated
        # request is the extension's own (chrome-extension://…),
        # not the dapp's. Frame ships the real dapp URL in a
        # custom JSON-RPC body field (``__frameOrigin``); prefer
        # that when present, fall back to the HTTP Origin header
        # otherwise. _handle_one applies the same precedence.
        origin = request.headers.get("Origin")
        if isinstance(body, list):
            return web.json_response(
                [await self._handle_one(r, origin) for r in body],
            )
        return web.json_response(await self._handle_one(body, origin))

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        # The Origin header here is the extension's; the real dapp
        # URL arrives per-message in ``__frameOrigin`` on the JSON-
        # RPC body. _handle_one picks the body field over this
        # fallback.
        origin = request.headers.get("Origin")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.add(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        req = json.loads(msg.data)
                    except Exception:
                        await ws.send_str(json.dumps(
                            {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}}
                        ))
                        continue
                    if isinstance(req, list):
                        resp = [
                            await self._handle_one(r, origin, ws=ws)
                            for r in req
                        ]
                    else:
                        resp = await self._handle_one(req, origin, ws=ws)
                    await ws.send_str(json.dumps(resp))
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self._ws_clients.discard(ws)
            self._ws_subscriptions.pop(ws, None)
        return ws

    # --- event push (Qt-thread → asyncio loop) ----------------------------

    def broadcast_accounts_changed(self, accounts: list[str]) -> None:
        """EIP-1193 ``accountsChanged`` event. Call from the Qt
        thread when the user changes the default account; reaches
        every WS-connected dapp that has subscribed via
        ``eth_subscribe('accountsChanged')`` as a JSON-RPC
        ``eth_subscription`` notification (Frame's wire format).
        The Frame extension translates this into a JS
        ``provider.emit('accountsChanged', accounts)`` on
        ``window.ethereum`` so dapps re-render without polling."""
        self._schedule_event("accountsChanged", accounts)

    def broadcast_chain_changed(self, chain_id: int) -> None:
        """EIP-1193 ``chainChanged`` event. Hex-encoded chainId per
        spec. Goes to every dapp that subscribed to either
        ``chainChanged`` or ``networkChanged``."""
        hex_id = hex(chain_id)
        self._schedule_event("chainChanged", hex_id)
        # Legacy alias some dapps still subscribe to.
        self._schedule_event("networkChanged", str(chain_id))

    def _schedule_event(self, sub_type: str, result) -> None:
        """Schedule an eth_subscription notification to every WS
        client that subscribed to ``sub_type``. Safe from any
        thread; no-op when the asyncio loop hasn't started yet
        (server bind failed) or has already stopped."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(
            self._broadcast_event(sub_type, result), loop,
        )

    def _register_subscription(self, ws, sub_type: str) -> str:
        """Allocate a subscription id and remember it under
        ``ws → sub_type``. Returns the id the dapp will see in
        ``eth_subscription.params.subscription``."""
        import uuid
        sub_id = "0x" + uuid.uuid4().hex
        self._ws_subscriptions.setdefault(ws, {})[sub_type] = sub_id
        return sub_id

    def _unregister_subscription(self, ws, sub_id: str) -> None:
        subs = self._ws_subscriptions.get(ws)
        if subs is None:
            return
        for sub_type, existing in list(subs.items()):
            if existing == sub_id:
                del subs[sub_type]

    async def _broadcast_event(self, sub_type: str, result) -> None:
        """Send an ``eth_subscription`` notification to every WS
        client subscribed to ``sub_type``. The payload's
        ``params.subscription`` is the per-client id minted at
        eth_subscribe time."""
        if not self._ws_subscriptions:
            return
        dead: list[web.WebSocketResponse] = []
        for ws, subs in list(self._ws_subscriptions.items()):
            sub_id = subs.get(sub_type)
            if sub_id is None:
                continue
            if ws.closed:
                dead.append(ws)
                continue
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_subscription",
                "params": {"subscription": sub_id, "result": result},
            }
            try:
                await ws.send_str(json.dumps(payload))
            except Exception as e:
                log.warning("ws subscription push failed (%s): %s",
                             sub_type, e)
                dead.append(ws)
        for ws in dead:
            self._ws_subscriptions.pop(ws, None)
            self._ws_clients.discard(ws)

    async def _handle_one(self, req: dict,
                           origin: Optional[str] = None,
                           ws: Optional[web.WebSocketResponse] = None,
                           ) -> dict:
        method = req.get("method")
        params = req.get("params") or []
        rid = req.get("id")
        # Frame attaches the real dapp URL as a top-level
        # ``__frameOrigin`` field on each JSON-RPC message — the
        # HTTP / WS Origin header is the extension's own. Other
        # wallet-extension wire formats may add their own field;
        # add them here as we learn the names.
        frame_origin = req.get("__frameOrigin")
        if isinstance(frame_origin, str) and frame_origin:
            origin = frame_origin
        try:
            result = await self._dispatch(method, params, origin, ws=ws)
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except RpcError as e:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": e.code, "message": e.message}}
        except Exception as e:
            # Demote transient network blips from ERROR/traceback
            # to a one-line WARNING. The dapp still gets a proper
            # JSON-RPC error response; this is only about how
            # loud the server log is. Suppress entirely when the
            # aiohttp session is already closed — that means
            # we're in app shutdown and the dozens of in-flight
            # requests racing past the close are pure noise.
            shutting_down = (
                self._client is None or self._client.closed
            )
            transient = (
                "ServerDisconnectedError",
                "ClientConnectorError",
                "ClientOSError",
                "TimeoutError",
            )
            looks_transient = (
                type(e).__name__ in transient
                or str(e) == "Session is closed"
            )
            if shutting_down:
                pass   # silent on shutdown
            elif looks_transient:
                log.warning("rpc %s: %s: %s",
                              method, type(e).__name__, e)
            else:
                log.exception("rpc dispatch failed: %s", method)
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32603, "message": str(e)}}

    async def _dispatch(self, method: str, params: list,
                         origin: Optional[str] = None,
                         ws: Optional[web.WebSocketResponse] = None,
                         ) -> Any:
        if method == "eth_subscribe":
            # Frame extends eth_subscribe with wallet-event types
            # (accountsChanged, chainChanged, networkChanged) on top
            # of the standard newHeads/logs/etc. Dapps subscribe
            # once and we push eth_subscription notifications when
            # the user changes state in the qeth UI.
            sub_type = params[0] if params else None
            if sub_type in (
                "accountsChanged", "chainChanged", "networkChanged",
            ):
                if ws is None:
                    raise RpcError(
                        -32600,
                        "eth_subscribe for wallet events requires WebSocket",
                    )
                return self._register_subscription(ws, sub_type)
            # newHeads / logs / newPendingTransactions / syncing fall
            # through to the upstream RPC — they need their own
            # subscription bookkeeping (forwarding upstream
            # notifications back here) which isn't wired up yet.
            return await self._proxy(method, params)

        if method == "eth_unsubscribe":
            sub_id = params[0] if params else None
            if ws is not None and sub_id:
                self._unregister_subscription(ws, sub_id)
            return True

        if method in ("eth_accounts", "eth_requestAccounts"):
            return [self.store.default_account] if self.store.default_account else []

        if method == "eth_chainId":
            return hex(self._rpc_chain_id)

        if method == "net_version":
            return str(self._rpc_chain_id)

        if method == "wallet_switchEthereumChain":
            cid = int(params[0]["chainId"], 16)
            if not any(c.chain_id == cid for c in self.store.chains):
                raise RpcError(4902, "Unrecognized chain")
            # Update the RPC's own chain only — the wallet UI's
            # chain (toolbar combo) is unaffected. The user keeps
            # browsing on their chosen chain while the dapp
            # transacts on its own.
            self._rpc_chain_id = cid
            # EIP-1193: emit chainChanged after a successful switch
            # so any subscribed dapps re-render. We're already on
            # the asyncio loop so push directly.
            await self._broadcast_event("chainChanged", hex(cid))
            await self._broadcast_event("networkChanged", str(cid))
            return None

        if method == "wallet_addEthereumChain":
            p = params[0]
            cid = int(p["chainId"], 16)
            # If we already know this chain, keep OUR rpc_url — dapps
            # often supply restricted relay URLs (e.g.
            # rpc.walletconnect.org) that 403 on calls from non-WC
            # clients. The user adding the chain via UI gets a
            # proper DRPC endpoint; the dapp's add request should
            # be a no-op when we already have a working entry.
            if any(c.chain_id == cid for c in self.store.chains):
                return None
            self.store.add_chain(Chain(
                name=p.get("chainName", "Custom"),
                chain_id=cid,
                rpc_url=p["rpcUrls"][0],
                symbol=(p.get("nativeCurrency") or {}).get("symbol", "ETH"),
                explorer=(p.get("blockExplorerUrls") or [""])[0],
            ))
            return None

        if method == "eth_sendTransaction":
            if self.signer_bridge is None:
                raise RpcError(-32601, "No signer wired up")
            try:
                req = parse_send_transaction_params(
                    params, self._rpc_chain_id,
                    origin=origin,
                )
            except SignerError as e:
                raise RpcError(-32602, str(e))
            try:
                tx_hash = await self.signer_bridge.submit_async(req)
            except SignerError as e:
                # User cancelled or signer failed — surface as a
                # JSON-RPC error so the dapp can react.
                raise RpcError(-32000, str(e))
            return tx_hash

        if method in (
            "eth_signTransaction",
            "personal_sign", "eth_sign",
            "eth_signTypedData", "eth_signTypedData_v3", "eth_signTypedData_v4",
        ):
            raise RpcError(-32601, "Signing not implemented in MVP")

        return await self._proxy(method, params)

    async def _proxy(self, method: str, params: list) -> Any:
        # Route reads to the dapp's chain (set via
        # wallet_switchEthereumChain), NOT the UI's chain. The
        # store has the chain dict; we look it up by id.
        chain = next(
            (c for c in self.store.chains
             if c.chain_id == self._rpc_chain_id),
            self.store.current_chain(),
        )
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with self._client.post(chain.rpc_url, json=payload, timeout=15) as r:
            data = await r.json()
        if "error" in data and data["error"]:
            err = data["error"]
            raise RpcError(err.get("code", -32603), err.get("message", "upstream error"))
        return data.get("result")
