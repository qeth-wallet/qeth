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
from typing import (TYPE_CHECKING, Any, Callable, List, NamedTuple, Optional)

from PySide6.QtCore import QThread, Signal

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
    raw_signed: Optional[str]


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
    balance_dirty = Signal(object, str, str)  # (chain, account, token_address)

    _POLL_S = 1.0          # supervisor reconcile / stop-poll cadence
    _MAX_BACKOFF_S = 30.0  # reconnect backoff ceiling
    # Re-broadcast a still-open pending tx at most this many times (an RPC
    # can ack a tx it never propagated), then give up but keep watching.
    REBROADCAST_MAX_ATTEMPTS = 30

    def __init__(
        self,
        chains_provider: "Callable[[], list[Chain]]",
        pending_provider: "Optional[Callable[[int], List[PendingTx]]]" = None,
        account_provider:
            "Optional[Callable[[], Optional[tuple[Chain, str]]]]" = None,
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
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

    async def _serve(self) -> None:
        """Supervisor: keep one ``_watch_chain`` task alive per desired
        chain, restarting it when its on-screen account changes (so the
        Transfer-log subscription re-targets), cancelling departed ones,
        until ``stop()``."""
        tasks: dict[int, "tuple[asyncio.Task[None], Optional[str]]"] = {}
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

    def _desired_targets(self) -> "dict[int, tuple[Chain, Optional[str]]]":
        """Chains to watch and, for the on-screen chain, the account whose
        Transfer logs we also subscribe to. Pending-tx chains get newHeads
        only (account ``None``); the current chain gets logs too. The current
        chain may also have a pending tx — it gets both."""
        targets: "dict[int, tuple[Chain, Optional[str]]]" = {}
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
        self, chain: "Chain", account: Optional[str] = None,
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
        self, chain: "Chain", account: Optional[str] = None,
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
        last_err: Optional[Exception] = None
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
                    async for msg in w3.socket.process_subscriptions():
                        sub = msg["subscription"]
                        if sub == heads_sub:
                            self.head.emit(
                                chain, _to_int(msg["result"]["number"]))
                            await self._probe_pending(chain, w3)
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
    def _transfer_filters(account: str) -> "list[list[Optional[str]]]":
        """The two ``logs`` filter topic-lists for ERC-20 Transfers touching
        the account: incoming (``to == account``) and outgoing
        (``from == account``), any token contract. You can't OR across topic
        positions in one filter, hence two subscriptions."""
        padded = _topic_address(account)
        return [
            [TRANSFER_TOPIC0, None, padded],   # to = account (incoming)
            [TRANSFER_TOPIC0, padded, None],   # from = account (outgoing)
        ]

    def _handle_log(self, chain: "Chain", account: str, log: Any) -> None:
        """A Transfer touching the account → the token's balance changed.
        Emit ``balance_dirty`` so the consumer re-reads ``balanceOf`` — we
        never trust the log's value, so a removed (reorg) log is treated the
        same: re-read gives the truth."""
        token = log.get("address") if hasattr(log, "get") else log["address"]
        if token:
            self.balance_dirty.emit(chain, account, str(token))

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
        await self._maybe_rebroadcast(tx, w3)

    async def _maybe_rebroadcast(self, tx: PendingTx, w3: Any) -> None:
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
            await self._rpc(w3, "eth_sendRawTransaction", [tx.raw_signed])
        except Exception as e:
            # "already known" / "nonce too low" are expected + harmless.
            log.debug("re-broadcast of %s: %s", tx.hash, e)

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
