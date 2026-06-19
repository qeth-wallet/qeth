"""Live block watcher — the async subsystem behind WebSocket live updates.

A single ``QThread`` runs one asyncio event loop that supervises one
``newHeads`` subscription per *active* chain (the chains the UI cares about
right now: the current view plus any with a pending tx). Each pushed header
becomes a queued Qt ``head`` signal; on each block the connection also
probes the chain's pending txs and emits ``confirmed`` / ``dropped`` — the
async equivalent of the sync ``PendingProbeWorker``, so confirmation lands
on the block the tx mined in instead of up to a poll interval later.
``link_state`` reports whether a chain currently has a live ws connection so
the legacy HTTP timers stay the floor whenever ws is unavailable.

The receipt/nonce probe rides the *same* ws connection that pushed the head
(raw ``make_request`` — the node just proved it's alive, and the raw receipt
matches what ``_on_receipt_confirmed`` already consumes). Resilience comes
from reconnect plus the legacy timer; a flaky receipt just retries next
block.

Threading model
---------------
``run()`` calls ``asyncio.run(self._serve())`` — the loop, every connection,
and every coroutine live in this QThread. Signals are emitted from here and
delivered to the main thread via Qt's queued connections. Shutdown is driven
by a plain ``threading.Event`` the supervisor polls (no cross-thread loop
handle to race on): ``stop()`` sets it, the supervisor unwinds and cancels
the per-chain tasks, ``asyncio.run`` returns, ``run()`` exits, and ``wait()``
joins — avoiding the "QThread destroyed while running" crash.

The ``pending_provider`` callback is invoked from the asyncio thread; it must
return an immutable snapshot (the plugin swaps a fresh list in atomically),
so no lock is needed — see the wiring in the transactions plugin.
"""

import asyncio
import logging
import threading
import time
from typing import (TYPE_CHECKING, Any, NamedTuple)
from collections.abc import Callable

import aiohttp
from PySide6.QtCore import QObject, QThread, Signal

from . import USER_AGENT
from .async_chain import make_async_web3, ws_urls_for

if TYPE_CHECKING:
    from .chains import Chain

log = logging.getLogger("qeth.live_watcher")


def _to_int(value: Any) -> int:
    """Normalise a quantity from a newHeads result. web3's subscription
    result formatting is provider-inconsistent: a block ``number`` arrives
    web3-formatted to ``int`` from some endpoints (publicnode) but as a raw
    ``0x`` hex string from others (DRPC). ``int(hex_str)`` raises, which —
    uncaught in the head loop — used to drop the connection and reconnect on
    every block (flapping). Handle both."""
    if isinstance(value, str):
        return int(value, 16)
    return int(value)


# keccak256("Transfer(address,address,uint256)") — the ERC-20 Transfer event
# topic0. Stable; defined here to keep the watcher import-light (the same
# value lives in tx_activity.TRANSFER_TOPIC0).
TRANSFER_TOPIC0 = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")


def _topic_address(account: str) -> str:
    """A 20-byte address as a 32-byte (left-zero-padded) log topic."""
    return "0x" + "00" * 12 + account[2:].lower()


class PendingTx(NamedTuple):
    """The minimum a probe needs about a pending tx, independent of the
    plugin's Transaction model so the watcher stays Qt-core-light."""

    hash: str
    from_addr: str
    nonce: int
    raw_signed: str | None


class LiveWatcher(QThread):
    """Owns the asyncio loop + the per-chain ``newHeads`` subscriptions and
    block-driven pending-tx probe.

    Construct with a thread-safe ``chains_provider`` returning the chains to
    watch right now, and an optional ``pending_provider(chain_id)`` returning
    that chain's pending txs to probe. Block numbers ride ``Signal(object)``
    (they can exceed the qint32 ceiling) — see CLAUDE.md.
    """

    head = Signal(object, object)            # (chain, block_number)
    link_state = Signal(object, bool)        # (chain, ws_connected)
    confirmed = Signal(object, str, object)  # (chain, hash, raw receipt dict)
    dropped = Signal(object, str)            # (chain, hash) — nonce consumed
    still_pending = Signal(object, str)      # (chain, hash) — probe saw it open
    balance_dirty = Signal(object, str, str)  # (chain, account, token_address)
    native_balance = Signal(object, str, object)  # (chain, account, wei)
    # (chain, account, token, counterparty, outgoing, raw_value) — a Transfer
    # touching the account, decoded for a sent/received desktop notification.
    # The value is the log's own (display only); balance_dirty drives the
    # authoritative balance re-read.
    transfer_seen = Signal(object, str, str, str, bool, object)

    _POLL_S = 1.0          # supervisor reconcile / stop-poll cadence
    _MAX_BACKOFF_S = 30.0  # reconnect backoff ceiling
    # How often to read the on-screen account's NATIVE balance over the live
    # ws (piggybacked on newHeads, throttled to this). A plain ETH receive
    # fires no Transfer log, so the log subscription misses it; this is the
    # native counterpart. It doubles as a keep-alive — a free RPC may drop a
    # client that sends nothing for minutes even while it streams us heads, so
    # one eth_getBalance a minute keeps the socket warm. NOT per block.
    NATIVE_POLL_S = 60.0
    # Re-broadcast a still-open pending tx at most this many times (an RPC
    # can ack a tx it never propagated), then give up but keep watching.
    REBROADCAST_MAX_ATTEMPTS = 30

    def __init__(
        self,
        chains_provider: "Callable[[], list[Chain]]",
        pending_provider: "Callable[[int], list[PendingTx]] | None" = None,
        account_provider:
            "Callable[[], tuple[Chain, str] | None] | None" = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        # web3's ws provider logs every connect / subscribe / disconnect at
        # INFO; on an account-switching session (each switch re-subscribes)
        # that's a wall of noise. Quiet to WARNING — errors still surface, and
        # qeth's own link_state (DEBUG) covers connection state.
        for _name in ("web3.providers.WebSocketProvider",
                      "web3.providers.persistent.subscription_manager"):
            logging.getLogger(_name).setLevel(logging.WARNING)
        self._chains_provider = chains_provider
        self._pending_provider = pending_provider
        # Returns the (chain, account) currently on screen, whose ERC-20
        # Transfer logs we subscribe to for live balances — or None.
        self._account_provider = account_provider
        self._stopping = threading.Event()
        self._rebroadcast_attempts: dict[str, int] = {}
        self._capped_warned: set[str] = set()

    # --- Qt-thread side ----------------------------------------------------

    def stop(self) -> None:
        """Ask the loop to unwind and join the thread. Safe from the Qt
        thread; idempotent. Bounded join so a wedged socket can't hang
        app close."""
        self._stopping.set()
        if self.isRunning():
            self.wait(5000)

    # --- asyncio-thread side (runs inside this QThread) --------------------

    def run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception:
            log.exception("live watcher loop crashed")

    def _quiet_ws_disconnects(self, loop, context) -> None:
        """Loop exception handler: a dropped WS connection (web3's websockets
        transport) surfaces here as an *unretrieved* exception in the
        library's background close task — typically a keepalive-ping timeout
        or a peer reset. ``_serve_connection`` already catches the drop and
        the supervisor reconnects with backoff, so the only fallout is the
        default handler dumping a multi-line ERROR + traceback. Downgrade a
        websockets-originated close to one clean warning; everything else
        still goes to the default handler so real bugs stay loud."""
        exc = context.get("exception")
        name = type(exc).__name__ if exc is not None else ""
        module = type(exc).__module__ if exc is not None else ""
        if module.startswith("websockets") or name.startswith("ConnectionClosed"):
            log.warning("live ws connection dropped (%s); reconnecting", name)
            return
        loop.default_exception_handler(context)

    async def _serve(self) -> None:
        """Supervisor: keep one ``_watch_chain`` task alive per desired
        chain, restarting it when its on-screen account changes (so the
        Transfer-log subscription re-targets), cancelling departed ones,
        until ``stop()``."""
        asyncio.get_running_loop().set_exception_handler(
            self._quiet_ws_disconnects)
        tasks: dict[int, tuple[asyncio.Task[None], str | None]] = {}
        try:
            while not self._stopping.is_set():
                desired = self._desired_targets()
                for cid, (chain, account) in desired.items():
                    cur = tasks.get(cid)
                    if cur is None or cur[0].done() or cur[1] != account:
                        if cur is not None and not cur[0].done():
                            await self._cancel(cur[0])   # account changed
                        tasks[cid] = (asyncio.create_task(
                            self._watch_chain(chain, account)), account)
                for cid in [c for c in tasks if c not in desired]:
                    await self._cancel(tasks.pop(cid)[0])
                await asyncio.sleep(self._POLL_S)
        finally:
            await asyncio.gather(
                *(self._cancel(t) for t, _ in tasks.values()),
                return_exceptions=True)

    def _desired_targets(self) -> "dict[int, tuple[Chain, str | None]]":
        """Chains to watch and, for the on-screen chain, the account whose
        Transfer logs we also subscribe to. Pending-tx chains get newHeads
        only (account ``None``); the current chain gets logs too. The current
        chain may also have a pending tx — it gets both."""
        targets: dict[int, tuple[Chain, str | None]] = {}
        for chain in self._chains_provider():
            targets[chain.chain_id] = (chain, None)
        if self._account_provider is not None:
            cur = self._account_provider()
            if cur is not None:
                chain, account = cur
                targets[chain.chain_id] = (chain, account)
        return targets

    @staticmethod
    async def _cancel(task: "asyncio.Task[None]") -> None:
        """Cancel a per-chain task and await its unwind, so a re-subscribe or
        shutdown never leaves an orphaned pending task (asyncio would warn)."""
        task.cancel()
        try:
            await task
        except BaseException:
            pass

    async def _watch_chain(
        self, chain: "Chain", account: str | None = None,
    ) -> None:
        """Hold a connection for one chain, reconnecting with exponential
        backoff. ``link_state(False)`` whenever down so the legacy timer
        takes over; a chain with no working ws settles into the backoff
        idle rather than churning supervisor restarts. ``account`` (the
        on-screen account) adds Transfer-log subscriptions for live
        balances; ``None`` means newHeads-only (a pending-tx-only chain)."""
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                await self._serve_connection(chain, account)
                backoff = 1.0  # connected + streamed then dropped; reset
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("[%s] connection ended: %s", chain.name, e)
            self.link_state.emit(chain, False)
            await asyncio.sleep(min(backoff, self._MAX_BACKOFF_S))
            backoff *= 2

    async def _serve_connection(
        self, chain: "Chain", account: str | None = None,
    ) -> None:
        """Connect to the chain's ws endpoints in order; on the first that
        subscribes, multiplex ``newHeads`` (→ block + pending-tx probe) and,
        when ``account`` is set, the account's ERC-20 ``Transfer`` logs (→
        ``balance_dirty``) over the one connection, routed by subscription
        id, until it drops (then return — the caller reconnects). Raise if no
        endpoint connects (the caller backs off).

        The single live-I/O seam — orchestration tests override it with a
        synthetic emitter; the probe / log logic is tested via ``_probe_one``
        and ``_handle_log``."""
        last_err: Exception | None = None
        for url in ws_urls_for(chain):
            try:
                async with make_async_web3(url) as w3:
                    heads_sub = await w3.eth.subscribe("newHeads")
                    log_subs: set = set()
                    if account:
                        for topics in self._transfer_filters(account):
                            log_subs.add(await w3.eth.subscribe(
                                "logs", {"topics": topics}))
                    self.link_state.emit(chain, True)
                    # last native read; 0.0 means "never" → the first head
                    # reads immediately (so a reconnect re-primes native).
                    last_native = 0.0
                    async for msg in w3.socket.process_subscriptions():
                        sub = msg["subscription"]
                        if sub == heads_sub:
                            self.head.emit(
                                chain, _to_int(msg["result"]["number"]))
                            await self._probe_pending(chain, w3)
                            if account is not None and (
                                    time.monotonic() - last_native
                                    >= self.NATIVE_POLL_S):
                                last_native = time.monotonic()
                                await self._emit_native(chain, account, w3)
                        elif account is not None and sub in log_subs:
                            self._handle_log(chain, account, msg["result"])
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_err = e
                log.debug("[%s] ws %s failed: %s", chain.name, url, e)
        raise last_err or RuntimeError(f"no ws endpoint for {chain.name}")

    @staticmethod
    def _transfer_filters(account: str) -> "list[list[str | None]]":
        """The two ``logs`` filter topic-lists for ERC-20 Transfers touching
        the account: incoming (``to == account``) and outgoing
        (``from == account``), any token contract. You can't OR across topic
        positions in one filter, hence two subscriptions."""
        padded = _topic_address(account)
        return [
            [TRANSFER_TOPIC0, None, padded],   # to = account (incoming)
            [TRANSFER_TOPIC0, padded, None],   # from = account (outgoing)
        ]

    @staticmethod
    def _log_hexstr(v: Any) -> str:
        """A log topic / data field as a 0x-prefixed lowercase hex string.
        web3 hands these back as ``HexBytes`` (``.hex()`` → bare hex) from
        some providers and plain ``0x``-strings from others; normalise both."""
        if hasattr(v, "hex"):
            h = v.hex()
            return (h if h.startswith("0x") else "0x" + h).lower()
        return str(v).lower()

    @classmethod
    def _addr_from_topic(cls, topic: Any) -> str:
        """The 20-byte address packed in the low bytes of a 32-byte topic."""
        return "0x" + cls._log_hexstr(topic)[-40:]

    @classmethod
    def _int_from_data(cls, data: Any) -> int:
        h = cls._log_hexstr(data)
        return int(h, 16) if h not in ("", "0x") else 0

    def _handle_log(self, chain: "Chain", account: str, log: Any) -> None:
        """A Transfer touching the account → the token's balance changed.
        Emit ``balance_dirty`` so the consumer re-reads ``balanceOf`` — we
        never trust the log's value, so a removed (reorg) log is treated the
        same: re-read gives the truth. Also emit ``transfer_seen`` with the
        decoded direction + value for a desktop notification (value display-
        only; a malformed log degrades to just the balance re-read)."""
        get = log.get if hasattr(log, "get") else log.__getitem__
        token = get("address")
        if not token:
            return
        self.balance_dirty.emit(chain, account, str(token))
        try:
            topics = get("topics") or []
            if len(topics) < 3:
                return
            frm = self._addr_from_topic(topics[1])
            to = self._addr_from_topic(topics[2])
            outgoing = frm == account.lower()
            counterparty = to if outgoing else frm
            value = self._int_from_data(get("data"))
            self.transfer_seen.emit(
                chain, account, str(token), counterparty, outgoing, value)
        except Exception:
            pass

    async def _emit_native(self, chain: "Chain", account: str, w3: Any) -> None:
        """Read ``account``'s native balance over the live ws and emit it.
        Called ~once a minute (NATIVE_POLL_S) off the head loop — the inbound
        side of the Transfer-log subscription, which a plain ETH send never
        triggers. Failures are swallowed (retried next interval); the value is
        the node's authoritative balance, so we emit it directly (unlike a log,
        whose value we never trust)."""
        try:
            raw = await self._rpc(w3, "eth_getBalance", [account, "latest"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("[%s] native read: %s", chain.name, e)
            return
        if raw is not None:
            self.native_balance.emit(chain, account, _to_int(raw))

    async def _probe_pending(self, chain: "Chain", w3: Any) -> None:
        """Probe every pending tx the provider reports for this chain over
        the live connection. Per-tx failures are swallowed (retried next
        block); cancellation propagates for clean shutdown."""
        if self._pending_provider is None:
            return
        for tx in self._pending_provider(chain.chain_id):
            if self._stopping.is_set():
                return
            try:
                await self._probe_one(chain, tx, w3)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("[%s] probe %s: %s", chain.name, tx.hash, e)

    async def _probe_one(self, chain: "Chain", tx: PendingTx, w3: Any) -> None:
        """One pending tx, mirroring the sync ``PendingProbeWorker``:

          receipt present        -> ``confirmed``
          else nonce already < latest -> ``dropped`` (replaced/re-sent)
          else still open        -> idempotent, capped re-broadcast

        Uses raw ``make_request`` so the receipt is the same raw dict the
        existing ``_on_receipt_confirmed`` consumes, and so a checksum isn't
        required for the nonce lookup (the node accepts lower-case hex)."""
        receipt = await self._rpc(w3, "eth_getTransactionReceipt", [tx.hash])
        if receipt is not None:
            self.confirmed.emit(chain, tx.hash, receipt)
            self._forget(tx.hash)
            return
        latest = await self._rpc(
            w3, "eth_getTransactionCount", [tx.from_addr, "latest"])
        if latest is not None and tx.nonce < int(latest, 16):
            self.dropped.emit(chain, tx.hash)
            self._forget(tx.hash)
            return
        # Nonce still open — a contradicting reading that cancels any
        # tentative drop count (DROP_CONFIRM_READINGS means CONSECUTIVE).
        self.still_pending.emit(chain, tx.hash)
        await self._maybe_rebroadcast(tx, chain)

    async def _maybe_rebroadcast(self, tx: PendingTx, chain: "Chain") -> None:
        if not tx.raw_signed:
            return
        attempts = self._rebroadcast_attempts.get(tx.hash, 0)
        if attempts >= self.REBROADCAST_MAX_ATTEMPTS:
            if tx.hash not in self._capped_warned:
                self._capped_warned.add(tx.hash)
                log.warning(
                    "tx %s still pending after %d ws re-broadcasts — giving "
                    "up on re-broadcast (likely stuck on gas); still watching.",
                    tx.hash, self.REBROADCAST_MAX_ATTEMPTS)
            return
        self._rebroadcast_attempts[tx.hash] = attempts + 1
        try:
            await self._broadcast_pinned(chain, tx.raw_signed)
        except Exception as e:
            # "already known" / "nonce too low" are expected + harmless.
            log.debug("re-broadcast of %s: %s", tx.hash, e)

    @staticmethod
    async def _broadcast_pinned(chain: "Chain", raw_signed: str) -> None:
        """Re-broadcast ONLY via the user's chosen RPC — never the live ws
        connection (``ws_urls_for`` may have connected to a *fallback*-derived
        endpoint) and never the async http failover stack. Same policy as
        ``EthClient.send_raw_transaction``: a signed tx relayed to a fallback
        would leak a private / MEV-protected transaction into a public mempool.
        One-shot session: rebroadcasts are rare (capped per tx), so there's no
        long-lived session to manage on the watcher's loop. A JSON-RPC error
        body ("already known", "nonce too low") is a valid, harmless answer —
        only transport errors raise (and the caller logs them at debug)."""
        payload = {"jsonrpc": "2.0", "id": 1,
                   "method": "eth_sendRawTransaction", "params": [raw_signed]}
        async with aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT}) as session:
            async with session.post(
                    chain.rpc_url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                await resp.read()

    def _forget(self, tx_hash: str) -> None:
        self._rebroadcast_attempts.pop(tx_hash, None)
        self._capped_warned.discard(tx_hash)

    @staticmethod
    async def _rpc(w3: Any, method: str, params: list) -> Any:
        """Raw JSON-RPC over the connection, returning the ``result`` (or
        ``None``). A JSON-RPC error surfaces as no ``result`` -> ``None`` (a
        receipt that isn't ready yet looks the same — both mean "not
        confirmed", which is correct); a transport error raises and the
        caller retries next block."""
        resp = await w3.provider.make_request(method, params)
        if isinstance(resp, dict):
            return resp.get("result")
        return getattr(resp, "result", None)
