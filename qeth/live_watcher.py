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

    _POLL_S = 1.0          # supervisor reconcile / stop-poll cadence
    _MAX_BACKOFF_S = 30.0  # reconnect backoff ceiling
    # Re-broadcast a still-open pending tx at most this many times (an RPC
    # can ack a tx it never propagated), then give up but keep watching.
    REBROADCAST_MAX_ATTEMPTS = 30

    def __init__(
        self,
        chains_provider: "Callable[[], list[Chain]]",
        pending_provider: "Optional[Callable[[int], List[PendingTx]]]" = None,
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._chains_provider = chains_provider
        self._pending_provider = pending_provider
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
        chain, starting new ones and cancelling departed ones, until
        ``stop()``."""
        tasks: dict[int, "asyncio.Task[None]"] = {}
        try:
            while not self._stopping.is_set():
                desired = {c.chain_id: c for c in self._chains_provider()}
                for cid, chain in desired.items():
                    existing = tasks.get(cid)
                    if existing is None or existing.done():
                        tasks[cid] = asyncio.create_task(
                            self._watch_chain(chain))
                for cid in [c for c in tasks if c not in desired]:
                    tasks.pop(cid).cancel()
                await asyncio.sleep(self._POLL_S)
        finally:
            for t in tasks.values():
                t.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    async def _watch_chain(self, chain: "Chain") -> None:
        """Hold a connection for one chain, reconnecting with exponential
        backoff. ``link_state(False)`` whenever down so the legacy timer
        takes over; a chain with no working ws settles into the backoff
        idle rather than churning supervisor restarts."""
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                await self._serve_connection(chain)
                backoff = 1.0  # connected + streamed then dropped; reset
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("[%s] connection ended: %s", chain.name, e)
            self.link_state.emit(chain, False)
            await asyncio.sleep(min(backoff, self._MAX_BACKOFF_S))
            backoff *= 2

    async def _serve_connection(self, chain: "Chain") -> None:
        """Connect to the chain's ws endpoints in order; on the first that
        subscribes, emit each ``newHeads`` block and probe the pending txs
        over the same connection until it drops (then return — the caller
        reconnects). Raise if no endpoint connects (the caller backs off).

        The single live-I/O seam — orchestration tests override it with a
        synthetic emitter; the probe logic itself is tested via
        ``_probe_one``."""
        last_err: Optional[Exception] = None
        for url in ws_urls_for(chain):
            try:
                async with make_async_web3(url) as w3:
                    await w3.eth.subscribe("newHeads")
                    self.link_state.emit(chain, True)
                    async for msg in w3.socket.process_subscriptions():
                        self.head.emit(chain, _to_int(msg["result"]["number"]))
                        await self._probe_pending(chain, w3)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_err = e
                log.debug("[%s] ws %s failed: %s", chain.name, url, e)
        raise last_err or RuntimeError(f"no ws endpoint for {chain.name}")

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
