"""Local transaction simulation via revm (pyrevm).

Run a not-yet-broadcast transaction against the chain's *forked* latest
state and return the event logs it would emit — so the Send / dapp-Sign
dialogs can preview "what will happen" the same way the confirmed-tx
details view shows past events.

Forking only uses standard state-read RPC methods
(``eth_getStorageAt`` / ``eth_getCode`` / ``eth_getBalance`` …), which
every endpoint supports — unlike ``debug_traceCall`` / ``eth_simulateV1``
which many public RPCs don't expose.

``pyrevm`` is an **optional** dependency: when it isn't installed (or the
simulation errors) the helpers return ``None`` and callers simply skip
the preview. The simulation is slow-ish — each cold storage slot is an
RPC round-trip — so callers should run it off the main thread.
"""

import logging

from eth_utils import to_checksum_address

log = logging.getLogger("qeth.simulate")


def pyrevm_available() -> bool:
    """True if the optional ``pyrevm`` dependency can be imported."""
    try:
        import pyrevm  # noqa: F401
        return True
    except Exception:
        return False


def simulate_logs(chain, from_addr: str, to_addr, data, value,
                  *, evm_cls=None):
    """Simulate the tx against ``chain``'s forked latest state and return
    its event logs as ``decode_event``-ready dicts::

        [{"address": "0x…", "topics": ["0x…", …], "data": "0x…"}, …]

    Returns ``None`` when pyrevm is unavailable, the tx is a contract
    creation (no ``to``), or the simulation raises — the caller then
    shows no preview rather than a wrong one. ``evm_cls`` is an injection
    seam for tests; production passes nothing and imports pyrevm.EVM."""
    if not to_addr:
        return None   # contract creation — not previewed
    EVM = evm_cls
    if EVM is None:
        try:
            from pyrevm import EVM
        except Exception:
            return None
    try:
        evm = EVM(fork_url=chain.rpc_url)
        calldata = b""
        if data and data not in ("0x", "0X"):
            calldata = bytes.fromhex(data[2:] if data.startswith("0x") else data)
        kwargs = {
            "caller": to_checksum_address(from_addr),
            "to": to_checksum_address(to_addr),
            "calldata": calldata,
        }
        if value:
            kwargs["value"] = int(value)
        evm.message_call(**kwargs)
        out = []
        for lg in evm.result.logs:
            # pyrevm Log: .address (str), .topics (list[str]),
            # .data is a (topics, data_bytes) tuple — the payload is [1].
            out.append({
                "address": lg.address,
                "topics": list(lg.topics),
                "data": "0x" + lg.data[1].hex(),
            })
        return out
    except Exception as e:
        log.warning("simulation failed: %s", e)
        return None
