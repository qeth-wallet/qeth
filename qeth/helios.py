"""Helios light-client sidecar — verified chain state for simulations.

When a ``helios`` binary is installed and the chain is one Helios can
verify, simulations stop trusting the remote RPC: the py-evm fork's
state reads are routed through a local Helios instance, which
proof-verifies every account/slot/header against sync-committee-verified
roots (EIP-1186 proofs vs the light-client state root). The remote node
is demoted to an untrusted courier — it can withhold, but a lie fails
proof verification inside Helios instead of reaching the preview.

Design (settled in docs/eth-browsing.md "Helios integration shape"):

- **Sidecar process over loopback TCP.** Helios has no Python binding
  and its RPC server is TCP-only (jsonrpsee over a SocketAddr) — and a
  process boundary means a Helios crash degrades to the untrusted path
  instead of taking the wallet down.
- **Readiness gate**: ``eth_syncing == False``. Checkpoint sync from
  cold measured at ~6 s (mainnet, 2026-06-12); the gate caps the wait
  and reports not-ready rather than serving unverified data early.
- **Lazy per-chain singletons**, spawned on first use from the worker
  thread that wants one, terminated via atexit. ``QETH_HELIOS=0``
  disables the whole feature.

The sidecar's execution-rpc is the chain's configured RPC: the same
endpoint qeth already uses, just stripped of its power to lie.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from typing import Any

from .chains import Chain

log = logging.getLogger("qeth.helios")

# chain_id -> (helios subcommand, --network value or None for the
# subcommand's default). Only networks Helios actually verifies: the
# consensus light client exists for Ethereum; OP-stack chains verify via
# the unsafe-signer path; Linea via its own module. Everything else
# (Polygon, Arbitrum, Gnosis, BNB, TAC, …) has no Helios support — those
# chains keep the direct untrusted-RPC path.
HELIOS_NETWORKS: dict[int, tuple[str, str | None]] = {
    1:     ("ethereum", None),          # mainnet is the subcommand default
    10:    ("opstack", "op-mainnet"),
    8453:  ("opstack", "base"),
    59144: ("linea", None),
}

# Default install location of `heliosup` — GUI sessions often don't have
# ~/.helios/bin on PATH.
_HELIOSUP_BIN = os.path.expanduser("~/.helios/bin/helios")

_READY_TIMEOUT_S = 30.0
_POLL_INTERVAL_S = 0.5


def helios_binary() -> str | None:
    """Path to a usable helios binary, or None (not installed, or the
    feature is disabled via ``QETH_HELIOS=0``).

    Resolution order:
      1. ``QETH_HELIOS_BIN`` — an explicit path. The "verify" package
         variants bundle helios and point their launcher at it (the
         only way verified mode reaches a sandboxed Flatpak), and it's
         a clean override for anyone with a helios in a custom spot.
      2. ``helios`` on ``PATH``.
      3. the heliosup default install (``~/.helios/bin/helios``)."""
    if os.environ.get("QETH_HELIOS", "1").strip().lower() in (
            "0", "false", "no", "off", ""):
        return None
    explicit = os.environ.get("QETH_HELIOS_BIN", "").strip()
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    found = shutil.which("helios")
    if found:
        return found
    if os.access(_HELIOSUP_BIN, os.X_OK):
        return _HELIOSUP_BIN
    return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _rpc(url: str, method: str, params: list | None = None,
         timeout: float = 5.0) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params or []}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("result")


class HeliosSidecar:
    """One helios process serving one chain on a loopback port."""

    def __init__(self, chain: Chain, binary: str,
                 popen: Any = None) -> None:
        # Resolved at call time (not as a bound default) so tests can
        # monkeypatch subprocess.Popen on this module.
        popen = popen or subprocess.Popen
        module, network = HELIOS_NETWORKS[chain.chain_id]
        self.chain_id = chain.chain_id
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._ready = False
        argv = [binary, module]
        if network:
            argv += ["--network", network]
        argv += [
            "--execution-rpc", chain.rpc_url,
            "--rpc-bind-ip", "127.0.0.1",
            "--rpc-port", str(self.port),
        ]
        log.info("spawning helios for chain %d: %s",
                 chain.chain_id, " ".join(argv[1:]))
        self._proc = popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def alive(self) -> bool:
        return self._proc.poll() is None

    def wait_ready(self, timeout: float = _READY_TIMEOUT_S,
                   sleep: Any = time.sleep,
                   clock: Any = time.monotonic) -> bool:
        """Block until helios reports synced (``eth_syncing`` → False).
        Sticky once true. False when the process died or ``timeout``
        passed — callers then fall back to the untrusted path."""
        if self._ready:
            return True
        deadline = clock() + timeout
        while clock() < deadline:
            if not self.alive():
                log.warning("helios for chain %d exited (rc=%s)",
                            self.chain_id, self._proc.returncode)
                return False
            try:
                if _rpc(self.url, "eth_syncing") is False:
                    self._ready = True
                    log.info("helios ready for chain %d on %s",
                             self.chain_id, self.url)
                    return True
            except Exception:
                pass   # server not up yet
            sleep(_POLL_INTERVAL_S)
        log.warning("helios for chain %d not ready after %.0fs",
                    self.chain_id, timeout)
        return False

    def stop(self) -> None:
        if self.alive():
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()


_sidecars: dict[int, HeliosSidecar] = {}
_lock = threading.Lock()


def _stop_all() -> None:
    for sc in _sidecars.values():
        sc.stop()


atexit.register(_stop_all)


def _ensure_sidecar(chain: Chain) -> HeliosSidecar | None:
    """The running (not necessarily synced) sidecar for ``chain`` —
    spawning one if needed. None when helios is absent/disabled or the
    chain isn't one Helios supports. Non-blocking (Popen returns
    immediately; the sync happens inside the helios process)."""
    if chain.chain_id not in HELIOS_NETWORKS:
        return None
    binary = helios_binary()
    if binary is None:
        return None
    with _lock:
        sc = _sidecars.get(chain.chain_id)
        if sc is None or not sc.alive():
            try:
                sc = HeliosSidecar(chain, binary)
            except Exception:
                log.exception("failed to spawn helios for chain %d",
                              chain.chain_id)
                return None
            _sidecars[chain.chain_id] = sc
        return sc


def prewarm(chain: Chain) -> None:
    """Spawn the sidecar WITHOUT waiting for it to sync — call at app
    start / chain switch so checkpoint sync (~3–6 s) overlaps with the
    user looking at their wallet instead of delaying the first preview.
    Instant (one Popen) and silent; safe on the main thread."""
    _ensure_sidecar(chain)


def verified_chain(chain: Chain, wait_s: float = _READY_TIMEOUT_S,
                   ) -> Chain | None:
    """A Chain whose RPC is a ready Helios sidecar verifying ``chain`` —
    or None when helios is absent/disabled, the chain isn't one Helios
    supports, or the sidecar didn't become ready in time (callers fall
    back to the direct untrusted path).

    Blocking unless prewarmed (first call spawns + syncs, ~6–10 s) —
    call from a worker thread. Reuses the running sidecar.
    """
    sc = _ensure_sidecar(chain)
    if sc is None:
        return None
    if not sc.wait_ready(timeout=wait_s):
        return None
    # The shadow chain must read from Helios ONLY. fallback_rpcs is set
    # to the same URL on purpose: an EMPTY tuple makes chain._rpc_urls
    # inherit the matching DEFAULT_CHAINS fallbacks for a known chain id
    # — which would silently fail verified reads over to unverified
    # public endpoints. The self-reference dedupes to a single-URL list.
    return Chain(
        name=f"{chain.name} (helios)",
        chain_id=chain.chain_id,
        rpc_url=sc.url,
        symbol=chain.symbol,
        coingecko_id=chain.coingecko_id,
        eip1559=chain.eip1559,
        fallback_rpcs=(sc.url,),
    )
