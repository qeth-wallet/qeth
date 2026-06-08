"""Live block watcher — the async subsystem behind WebSocket live updates.

A single ``QThread`` runs one asyncio event loop that supervises one
``newHeads`` subscription per *active* chain (the chains the UI cares about
right now: the current view plus any with a pending tx). Each pushed header
becomes a queued Qt ``head`` signal; ``link_state`` reports whether a chain
currently has a live ws connection, so the legacy HTTP timers can stay the
floor whenever ws is unavailable.

This task wires only ``head`` / ``link_state``; receipt-confirmation and
balance/log subscriptions hang off the same loop in later phases.

Threading model
---------------
``run()`` calls ``asyncio.run(self._serve())`` — the loop, every connection,
and every coroutine live in this QThread. Signals are emitted from here and
delivered to the main thread via Qt's queued connections. Shutdown is driven
by a plain ``threading.Event`` the supervisor polls (no cross-thread loop
handle to race on): ``stop()`` sets it, the supervisor unwinds and cancels
the per-chain tasks, ``asyncio.run`` returns, ``run()`` exits, and ``wait()``
joins — avoiding the "QThread destroyed while running" crash.
"""

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, AsyncIterator, Callable, Optional

from PySide6.QtCore import QThread, Signal

from .async_chain import make_async_web3, ws_urls_for

if TYPE_CHECKING:
    from .chains import Chain

log = logging.getLogger("qeth.live_watcher")


class LiveWatcher(QThread):
    """Owns the asyncio loop + the per-chain ``newHeads`` subscriptions.

    Construct with a thread-safe ``chains_provider`` returning the chains to
    watch right now; the supervisor reconciles that set against the running
    subscriptions roughly every second. Block numbers ride ``Signal(object)``
    (they can exceed the qint32 ceiling) — see CLAUDE.md.
    """

    head = Signal(object, object)        # (chain, block_number)
    link_state = Signal(object, bool)    # (chain, ws_connected)

    _POLL_S = 1.0          # supervisor reconcile / stop-poll cadence
    _MAX_BACKOFF_S = 30.0  # reconnect backoff ceiling

    def __init__(
        self,
        chains_provider: "Callable[[], list[Chain]]",
        parent: Optional[object] = None,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._chains_provider = chains_provider
        self._stopping = threading.Event()

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
        """Stream ``newHeads`` for one chain, reconnecting with exponential
        backoff. Emits ``link_state(False)`` whenever the stream is down so
        the legacy timer takes over. A chain with no (working) ws endpoint
        simply settles into the backoff idle — ``link_state(False)`` every
        ``_MAX_BACKOFF_S`` — rather than churning supervisor restarts."""
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                async for num in self._stream_heads(chain):
                    backoff = 1.0
                    self.head.emit(chain, num)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("[%s] head stream ended: %s", chain.name, e)
            self.link_state.emit(chain, False)
            await asyncio.sleep(min(backoff, self._MAX_BACKOFF_S))
            backoff *= 2

    async def _stream_heads(self, chain: "Chain") -> "AsyncIterator[int]":
        """Connect to the chain's ws endpoints in order; on the first that
        subscribes, yield block numbers until the connection drops. Raises
        the last error if every endpoint fails (the caller backs off).

        Split out as the single I/O seam so tests can inject synthetic
        heads without a network or a real ws server."""
        last_err: Optional[Exception] = None
        for url in ws_urls_for(chain):
            try:
                async with make_async_web3(url) as w3:
                    await w3.eth.subscribe("newHeads")
                    self.link_state.emit(chain, True)
                    async for msg in w3.socket.process_subscriptions():
                        yield int(msg["result"]["number"])
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_err = e
                log.debug("[%s] ws %s failed: %s", chain.name, url, e)
                continue
        if last_err is not None:
            raise last_err
