import asyncio
import concurrent.futures
import json
import logging
import threading
from typing import Any

from aiohttp import (
    ClientConnectorError, ClientOSError, ClientSession, TCPConnector,
    ServerDisconnectedError, WSMsgType, web,
)

from .chain import is_provider_limit_error
from .chains import Chain
from .signing import (
    SignerBridge, SignerError,
    parse_personal_sign_params, parse_send_transaction_params,
    parse_typed_data_params,
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
    # Private Network Access (Chromium): a page served over public
    # HTTPS (any dapp) that fetches this loopback server triggers a
    # CORS preflight carrying ``Access-Control-Request-Private-Network``;
    # the browser blocks the request unless the response grants it.
    # Frame's own extension sidesteps this by talking from an
    # extension context, but our Falkon connector injects the
    # provider into the page's main world, so its fetch / WebSocket
    # comes straight from the dapp origin and is subject to PNA.
    # See integrations/falkon/.
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


class RpcServer:
    """JSON-RPC server on 127.0.0.1:1248 — HTTP and WebSocket on the same port,
    matching Frame's endpoint so the Frame browser extension can connect.

    Runs on its own asyncio loop in a background thread."""

    def __init__(self, store, host: str = "127.0.0.1", port: int = 1248,
                 signer_bridge: SignerBridge | None = None):
        self.store = store
        self.host = host
        self.port = port
        # Optional: when None, signing methods still return -32601
        # (keeps tests that don't need a UI bridge working).
        self.signer_bridge = signer_bridge
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._runner: web.AppRunner | None = None
        self._client: ClientSession | None = None
        self._ready = threading.Event()
        self._error: str | None = None
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
        # Per-origin chain override. Each dapp (identified by its
        # Origin header / Frame's ``__frameOrigin``) can call
        # ``wallet_switchEthereumChain`` to pin itself to a chain;
        # other dapps see the wallet UI's current chain.
        # Previously this was a single global value, so 1inch
        # switching to zkSync Era pulled every other open tab onto
        # zkSync until the user restarted. Origins that haven't
        # overridden fall back to ``store.current_chain()`` at
        # read time, so a UI toolbar flip automatically reaches
        # unscoped dapps.
        self._rpc_chain_id_by_origin: dict[str, int] = {}
        # WS connection → origin captured at handshake. Lets
        # _broadcast_event scope chainChanged pushes correctly:
        # an override-driven chainChanged goes only to that
        # origin's sockets, while a UI-driven one goes only to
        # sockets whose origin doesn't carry an override.
        self._ws_origin: dict[web.WebSocketResponse, str | None] = {}
        # Per-upstream-host fail-fast cool-down. See _proxy below.
        self._host_last_fail: dict[str, float] = {}
        # In-flight wallet_addEthereumChain approvals, keyed by chain id.
        # A new chain isn't persisted until the user approves, so a dapp
        # (or Frame) re-firing the add while the prompt is open would
        # otherwise pass the "already known?" check and spawn a second
        # modal — the user then has to dismiss a stack of identical
        # dialogs. Concurrent requests for the same id share one prompt.
        self._pending_chain_add: dict[int, asyncio.Future[bool]] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="qeth-rpc", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    def stop(self, timeout: float = 5.0) -> None:
        """Shut the server down and BLOCK until it's actually down (or the
        timeout elapses), so callers — e.g. ``main()``'s finally — know the
        loop/runner/session are released before the process exits. Safe to
        call from any thread (won't join itself)."""
        loop = self._loop
        thread = self._thread
        if loop and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            try:
                future.result(timeout=timeout)
            except concurrent.futures.TimeoutError:
                log.warning("timed out waiting for qeth JSON-RPC shutdown")
            except Exception:
                log.exception("qeth JSON-RPC shutdown failed")
            finally:
                if loop.is_running():
                    loop.call_soon_threadsafe(loop.stop)
        if (thread is not None and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout=timeout)
        if thread is not None and not thread.is_alive():
            self._thread = None

    @property
    def error(self) -> str | None:
        return self._error

    async def _shutdown(self) -> None:
        # Null the handles BEFORE closing them: in-flight request handlers
        # read ``self._client`` (the "are we shutting down?" check) and a
        # half-closed-but-non-None client would race. ``stop`` stops the
        # loop afterwards (not here).
        client = self._client
        runner = self._runner
        self._client = None
        self._runner = None
        # Close live WS clients up front. Otherwise the site's shutdown
        # (inside runner.cleanup) waits its shutdown_timeout for these
        # still-open connections to drain — which, with a dapp/browser
        # connected, is exactly the multi-second "timed out waiting for
        # shutdown" hang on app close. Bounded so a dead socket can't itself
        # stall the close.
        if self._ws_clients:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(ws.close() for ws in list(self._ws_clients)),
                        return_exceptions=True,
                    ),
                    timeout=1.0,
                )
            except asyncio.TimeoutError:
                pass
        self._ws_clients.clear()
        self._ws_subscriptions.clear()
        self._ws_origin.clear()
        if client:
            await client.close()
        if runner:
            await runner.cleanup()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as e:
            self._error = str(e)
            self._ready.set()
        else:
            self._ready.set()
            loop.run_forever()
        finally:
            # Defensive cleanup if stop() never ran (or the loop fell out of
            # run_forever unexpectedly): release the session/runner and close
            # the loop so we don't leak it or log "Task was destroyed".
            try:
                if self._client is not None or self._runner is not None:
                    loop.run_until_complete(self._shutdown())
            except Exception:
                log.exception("qeth JSON-RPC cleanup failed")
            finally:
                asyncio.set_event_loop(None)
                loop.close()
                if self._loop is loop:
                    self._loop = None

    async def _serve(self) -> None:
        self._client = ClientSession(
            connector=TCPConnector(ttl_dns_cache=self._DNS_CACHE_TTL_S),
        )
        async def _preflight(_request: web.Request) -> web.Response:
            # CORS preflight. aiohttp requires a coroutine handler — a
            # plain lambda returning a Response is not awaitable and
            # raises if this route is ever hit.
            return web.Response()

        app = web.Application(middlewares=[_cors])
        app.router.add_route("OPTIONS", "/{tail:.*}", _preflight)
        app.router.add_post("/", self._http_handler)
        app.router.add_get("/", self._root_or_ws)
        # access_log=None silences aiohttp's per-request access logging —
        # otherwise every dapp poll (the Falkon connector hits eth_chainId +
        # eth_accounts every 4 s to detect chain/account changes) spams an
        # INFO line. Our own log.info("listening …") and per-method warnings
        # stay.
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        # shutdown_timeout caps how long runner.cleanup() waits for open
        # connections (default 60s) — the backstop behind closing WS clients
        # in _shutdown, so close stays prompt even if a connection lingers.
        site = web.TCPSite(self._runner, self.host, self.port,
                           shutdown_timeout=1.0)
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
        self._ws_origin[ws] = origin
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
                    resp: Any
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
            self._ws_origin.pop(ws, None)
        return ws

    # --- event push (Qt-thread → asyncio loop) ----------------------------

    def _chain_for_origin(self, origin: str | None) -> int:
        """The chain id this origin should see. Per-origin override
        if it has one, otherwise the wallet UI's current chain.
        ``None`` and ``""`` share the same "origin-less" slot so
        direct callers (curl, tests) can also switch chains and
        see the override on subsequent reads."""
        cid = self._rpc_chain_id_by_origin.get(origin or "")
        if cid is not None:
            return cid
        return self.store.current_chain().chain_id

    def set_rpc_chain(self, chain_id: int) -> None:
        """Called when the wallet UI's chain combo flips. The
        store has already been updated (it's the source of truth
        for the default-chain-for-dapps); here we only need to
        push chainChanged / networkChanged to subscribers whose
        origin hasn't pinned itself via wallet_switchEthereumChain.
        Safe from any thread: the broadcast is scheduled on the
        asyncio loop via ``_schedule_event``."""
        self._schedule_event(
            "chainChanged", hex(chain_id), only_unscoped=True,
        )
        self._schedule_event(
            "networkChanged", str(chain_id), only_unscoped=True,
        )

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

    def _schedule_event(
        self, sub_type: str, result,
        *, only_origin: str | None = None,
        only_unscoped: bool = False,
    ) -> None:
        """Schedule an eth_subscription notification to every WS
        client that subscribed to ``sub_type``. Safe from any
        thread; no-op when the asyncio loop hasn't started yet
        (server bind failed) or has already stopped.

        Filter kwargs are forwarded to ``_broadcast_event`` — see
        its docstring for the scoping semantics."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        asyncio.run_coroutine_threadsafe(
            self._broadcast_event(
                sub_type, result,
                only_origin=only_origin,
                only_unscoped=only_unscoped,
            ),
            loop,
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

    async def _broadcast_event(
        self, sub_type: str, result,
        *, only_origin: str | None = None,
        only_unscoped: bool = False,
    ) -> None:
        """Send an ``eth_subscription`` notification to subscribed
        WS clients. Filters:

        - ``only_origin``: deliver only to sockets whose handshake
          origin matched. Used for origin-scoped events like the
          chainChanged that follows a ``wallet_switchEthereumChain``
          — other dapps must not be yanked onto the new chain.
        - ``only_unscoped``: deliver only to sockets whose origin
          has NOT set a per-origin chain override. Used for the
          UI-driven chainChanged that fires when the user flips
          the toolbar combo — dapps that have explicitly switched
          their chain stay on the chain they picked.
        """
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
            ws_origin = self._ws_origin.get(ws)
            if only_origin is not None and ws_origin != only_origin:
                continue
            if (only_unscoped
                    and (ws_origin or "") in self._rpc_chain_id_by_origin):
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
                           origin: str | None = None,
                           ws: web.WebSocketResponse | None = None,
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
            if not isinstance(method, str):
                raise RpcError(-32600, "Invalid Request: 'method' must be a string")
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
            #
            # ``isinstance`` not ``type(e).__name__ in ...`` —
            # ClientConnectorError has a family of more specific
            # subclasses (ClientConnectorDNSError,
            # ClientConnectorSSLError, ClientConnectorCertificateError, …)
            # that the string check silently missed, so when DNS
            # died for eth.drpc.org we dumped a 40-line traceback
            # for every dapp poll → multiple per second.
            shutting_down = (
                self._client is None or self._client.closed
            )
            looks_transient = (
                isinstance(e, (
                    ClientConnectorError,
                    ClientOSError,
                    ServerDisconnectedError,
                    asyncio.TimeoutError,
                ))
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
                         origin: str | None = None,
                         ws: web.WebSocketResponse | None = None,
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
            return await self._proxy(method, params, origin=origin)

        if method == "eth_unsubscribe":
            sub_id = params[0] if params else None
            if ws is not None and sub_id:
                self._unregister_subscription(ws, sub_id)
            return True

        if method in ("eth_accounts", "eth_requestAccounts"):
            return [self.store.default_account] if self.store.default_account else []

        if method == "eth_chainId":
            return hex(self._chain_for_origin(origin))

        if method == "net_version":
            return str(self._chain_for_origin(origin))

        if method == "wallet_switchEthereumChain":
            cid = int(params[0]["chainId"], 16)
            if not any(c.chain_id == cid for c in self.store.chains):
                raise RpcError(4902, "Unrecognized chain")
            # Update only the calling origin's chain — the wallet
            # UI and other open dapps are unaffected. Origin-less
            # requests (e.g. direct curl, no header) get treated
            # as a per-empty-string override so a subsequent call
            # from the same client sees the same chain.
            self._rpc_chain_id_by_origin[origin or ""] = cid
            # EIP-1193: emit chainChanged after a successful switch
            # so any subscribed dapps re-render. Scoped to the
            # requesting origin so we don't yank other dapps onto
            # this chain.
            await self._broadcast_event(
                "chainChanged", hex(cid), only_origin=origin,
            )
            await self._broadcast_event(
                "networkChanged", str(cid), only_origin=origin,
            )
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
            new_chain = Chain(
                name=p.get("chainName", "Custom"),
                chain_id=cid,
                rpc_url=p["rpcUrls"][0],
                symbol=(p.get("nativeCurrency") or {}).get("symbol", "ETH"),
                explorer=(p.get("blockExplorerUrls") or [""])[0],
            )
            # Adding a genuinely new chain means trusting the
            # *site-supplied* RPC URL for every future read, preview,
            # and broadcast on it — and not just for this dapp: once
            # persisted, the endpoint serves whatever app later uses
            # that chain. A matching chain id is no protection (the id
            # can be honest while the node lies or front-runs). So ask
            # the user before the endpoint lands; declining returns the
            # EIP-1193 user-rejected code. No bridge wired up (headless
            # / tests) keeps the old silent add — there's no one to ask.
            if self.signer_bridge is not None:
                approved = await self._confirm_new_chain(cid, {
                    "chain_id": cid,
                    "name": new_chain.name,
                    "rpc_url": new_chain.rpc_url,
                    "symbol": new_chain.symbol,
                    "explorer": new_chain.explorer,
                    "origin": origin,
                })
                if not approved:
                    raise RpcError(4001, "User rejected adding the network")
            self.store.add_chain(new_chain)
            # Notify the UI so the new chain appears in the
            # toolbar combo (with icon discovery kicked off) and
            # the user isn't stuck restarting just to switch to it.
            # Cross-thread emit — Signal delivery is auto-queued
            # since the bridge lives on the Qt main thread.
            if self.signer_bridge is not None:
                self.signer_bridge.chain_added.emit(cid)
            return None

        if method == "eth_sendTransaction":
            if self.signer_bridge is None:
                raise RpcError(-32601, "No signer wired up")
            try:
                req = parse_send_transaction_params(
                    params, self._chain_for_origin(origin),
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

        if method == "eth_sign":
            # ``eth_sign`` lets a dapp pass an arbitrary 32-byte
            # hash for signing — dangerous because that hash
            # could be a transaction digest in disguise. Modern
            # wallets refuse it (Frame, MetaMask default off).
            # Tell the dapp to use ``personal_sign``.
            raise RpcError(
                -32601,
                "eth_sign refused (unsafe). Use personal_sign instead.",
            )

        if method in ("personal_sign", "personal_signMessage"):
            if self.signer_bridge is None:
                raise RpcError(-32601, "No signer wired up")
            try:
                msg_req = parse_personal_sign_params(params, origin=origin)
            except SignerError as e:
                raise RpcError(-32602, str(e))
            try:
                sig_hex = await self.signer_bridge.submit_async(msg_req)
            except SignerError as e:
                raise RpcError(-32000, str(e))
            return sig_hex

        if method in (
            "eth_signTypedData",
            "eth_signTypedData_v3",
            "eth_signTypedData_v4",
        ):
            if self.signer_bridge is None:
                raise RpcError(-32601, "No signer wired up")
            try:
                td_req = parse_typed_data_params(params, origin=origin)
            except SignerError as e:
                raise RpcError(-32602, str(e))
            try:
                sig_hex = await self.signer_bridge.submit_async(td_req)
            except SignerError as e:
                raise RpcError(-32000, str(e))
            return sig_hex

        if method == "eth_signTransaction":
            raise RpcError(
                -32601,
                "eth_signTransaction not supported (use eth_sendTransaction)",
            )

        if method == "eth_sendRawTransaction":
            # A dapp-submitted, pre-signed tx. Broadcast ONLY via the user's
            # chosen RPC — never a fallback (the same policy as
            # ``EthClient.send_raw_transaction``): relaying a signed tx to a
            # fallback would leak a private / MEV-protected transaction into a
            # public mempool and override the user's explicit endpoint choice.
            return await self._proxy(method, params, origin=origin,
                                     broadcast=True)

        return await self._proxy(method, params, origin=origin)

    async def _confirm_new_chain(self, cid: int, info: dict) -> bool:
        """Ask the user (via the signer bridge) to approve adding a new
        chain, coalescing duplicate in-flight requests for the same chain
        id onto a single prompt. ``_dispatch`` runs on the one asyncio
        loop, so the dict read/insert needs no lock. ``asyncio.shield``
        keeps a second dapp's disconnect from cancelling the shared
        future out from under the first."""
        assert self.signer_bridge is not None
        pending = self._pending_chain_add.get(cid)
        if pending is not None:
            return await asyncio.shield(pending)
        fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        self._pending_chain_add[cid] = fut
        try:
            approved = await self.signer_bridge.confirm_chain_async(info)
            if not fut.done():
                fut.set_result(approved)
            return approved
        except BaseException as e:
            if not fut.done():
                fut.set_exception(e)
            raise
        finally:
            self._pending_chain_add.pop(cid, None)

    # Recent transient failures per upstream host. When a host has
    # failed within the last ``_FAIL_FAST_S`` seconds we short-
    # circuit subsequent requests instead of grinding through
    # another full DNS / connect timeout. Browser dapps poll
    # multiple methods per second, so without this each one would
    # add 1-15 s of wasted asyncio-thread time + a log line, which
    # is what made the app appear to hang when DRPC's DNS went out.
    _FAIL_FAST_S = 5.0

    # Cache resolved upstream IPs for this long (aiohttp's default is 10 s).
    # Once a host is resolved, a DNS outage shorter than this is invisible —
    # the cached IP is reused with no lookup, which is what rides out the
    # "Timeout while contacting DNS servers" blips. RPC provider IPs are
    # stable, and _proxy fails over (re-resolving the next host) if a cached
    # IP ever goes stale, so a long TTL is safe.
    _DNS_CACHE_TTL_S = 3600

    async def _proxy(
        self, method: str, params: list,
        origin: str | None = None,
        broadcast: bool = False,
    ) -> Any:
        # Route reads to the requesting origin's chain (per
        # wallet_switchEthereumChain), falling back to the wallet
        # UI's chain when this origin hasn't pinned itself.
        cid = self._chain_for_origin(origin)
        chain = next(
            (c for c in self.store.chains if c.chain_id == cid),
            self.store.current_chain(),
        )
        # Try the chain's primary RPC, then its fallbacks — the same list
        # EthClient fails over but the proxy previously ignored. A transport
        # blip on one provider (DNS hiccup, host down, a garbage body) now
        # fails over to the next instead of failing the dapp's request; we only
        # give up once every provider is exhausted (or on its fail-fast cooldown).
        # EXCEPT broadcasts: a signed tx goes ONLY to the user's chosen RPC —
        # never a fallback, and never skipped for a cooldown (with no
        # alternative allowed, trying a recently-failed primary beats failing
        # outright).
        urls = [chain.rpc_url] if broadcast else [chain.rpc_url, *chain.fallback_rpcs]
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        assert self._client is not None  # set in _serve() before any request is handled
        now = asyncio.get_event_loop().time()
        last_err: Exception | None = None
        for url in urls:
            last_fail = self._host_last_fail.get(url)
            if (not broadcast and last_fail is not None
                    and (now - last_fail) < self._FAIL_FAST_S):
                continue  # on cooldown — skip rather than pile on a 15 s timeout
            try:
                async with self._client.post(
                    url, json=payload, timeout=15,
                ) as r:
                    status = r.status
                    body = await r.text()
            except (
                ClientConnectorError,
                ClientOSError,
                ServerDisconnectedError,
                asyncio.TimeoutError,
            ) as e:
                self._host_last_fail[url] = now
                last_err = e
                continue  # transport failure — fail over to the next provider
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                # Non-JSON / truncated body (DRPC under load, a cut connection,
                # a Cloudflare error page). Treat as a host failure and fail
                # over; surface it only if every provider is exhausted.
                self._host_last_fail[url] = now
                log.warning(
                    "proxy %s: non-JSON response from %s (HTTP %s): %r",
                    method, url, status, body[:200],
                )
                last_err = RpcError(
                    -32603, f"upstream returned an invalid response (HTTP {status})",
                )
                continue
            # A parseable body can still be the PROVIDER failing rather than
            # the chain answering. Probed live (2026-06-11): DRPC under load
            # answers HTTP 408 + {"code":30,"message":"Request timeout on the
            # free tier…"} for most requests, and occasionally HTTP 500 +
            # {"code":19,"message":"Temporary internal error…"} — valid JSON,
            # so the old "parsed and has an error" path forwarded the
            # provider's own error to the dapp as a final answer instead of
            # failing over. Treat any HTTP error status, or a rate-limit
            # error object on a 200, like a transport failure. A
            # request-level error (a revert, bad params) comes on a 200 and
            # is forwarded below — every provider would answer it the same.
            err = data.get("error") if isinstance(data, dict) else None
            limited = is_provider_limit_error(err)
            if status >= 400 or limited:
                # Cooldown for limiter / overload / server-side states (the
                # DRPC 408 lands after a multi-second server stall — without
                # a cooldown every request would re-eat that stall before
                # failing over). A plain 4xx gets no cooldown: it's often
                # per-method brokenness (DRPC's free Gnosis endpoint 400s
                # eth_call while serving everything else), not host sickness.
                if limited or status in (408, 429) or status >= 500:
                    self._host_last_fail[url] = now
                msg = ((err.get("message") if isinstance(err, dict) else None)
                       or f"upstream HTTP {status}")
                log.warning("proxy %s: provider-side failure from %s "
                            "(HTTP %s): %s", method, url, status, msg[:200])
                code = err.get("code", -32603) if isinstance(err, dict) else -32603
                last_err = RpcError(code, msg)
                continue
            # Success — clear this host's cooldown so it's preferred next time.
            self._host_last_fail.pop(url, None)
            if err:
                raise RpcError(err.get("code", -32603), err.get("message", "upstream error"))
            return data.get("result")
        # Every provider failed or is on cooldown. Surface the last error — a
        # connection error gets demoted to a one-line WARNING by _handle_one.
        if last_err is not None:
            raise last_err
        raise RpcError(-32603, "upstream temporarily unreachable")
