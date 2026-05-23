import asyncio
import json
import logging
import threading
from typing import Any, Optional

from aiohttp import ClientSession, WSMsgType, web

from .chains import Chain

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

    def __init__(self, store, host: str = "127.0.0.1", port: int = 1248):
        self.store = store
        self.host = host
        self.port = port
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._runner: Optional[web.AppRunner] = None
        self._client: Optional[ClientSession] = None
        self._ready = threading.Event()
        self._error: Optional[str] = None

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
        if isinstance(body, list):
            return web.json_response([await self._handle_one(r) for r in body])
        return web.json_response(await self._handle_one(body))

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
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
                    resp = [await self._handle_one(r) for r in req]
                else:
                    resp = await self._handle_one(req)
                await ws.send_str(json.dumps(resp))
            elif msg.type == WSMsgType.ERROR:
                break
        return ws

    async def _handle_one(self, req: dict) -> dict:
        method = req.get("method")
        params = req.get("params") or []
        rid = req.get("id")
        try:
            result = await self._dispatch(method, params)
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except RpcError as e:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": e.code, "message": e.message}}
        except Exception as e:
            log.exception("rpc dispatch failed: %s", method)
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32603, "message": str(e)}}

    async def _dispatch(self, method: str, params: list) -> Any:
        if method in ("eth_accounts", "eth_requestAccounts"):
            return [self.store.default_account] if self.store.default_account else []

        if method == "eth_chainId":
            return hex(self.store.current_chain().chain_id)

        if method == "net_version":
            return str(self.store.current_chain().chain_id)

        if method == "wallet_switchEthereumChain":
            cid = int(params[0]["chainId"], 16)
            if not any(c.chain_id == cid for c in self.store.chains):
                raise RpcError(4902, "Unrecognized chain")
            self.store.set_current_chain(cid)
            return None

        if method == "wallet_addEthereumChain":
            p = params[0]
            self.store.add_chain(Chain(
                name=p.get("chainName", "Custom"),
                chain_id=int(p["chainId"], 16),
                rpc_url=p["rpcUrls"][0],
                symbol=(p.get("nativeCurrency") or {}).get("symbol", "ETH"),
                explorer=(p.get("blockExplorerUrls") or [""])[0],
            ))
            return None

        if method in (
            "eth_sendTransaction", "eth_signTransaction",
            "personal_sign", "eth_sign",
            "eth_signTypedData", "eth_signTypedData_v3", "eth_signTypedData_v4",
        ):
            raise RpcError(-32601, "Signing not implemented in MVP")

        return await self._proxy(method, params)

    async def _proxy(self, method: str, params: list) -> Any:
        chain = self.store.current_chain()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with self._client.post(chain.rpc_url, json=payload, timeout=15) as r:
            data = await r.json()
        if "error" in data and data["error"]:
            err = data["error"]
            raise RpcError(err.get("code", -32603), err.get("message", "upstream error"))
        return data.get("result")
