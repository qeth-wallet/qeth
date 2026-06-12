"""Local transaction simulation on a py-evm fork of live chain state.

This is the engine behind the event preview's fork fallback (see
``qeth/simulate.py``): execute the prospective tx locally against the
chain's *current* state and collect the logs it would emit. py-evm is
pure Python — it ships on every platform/Python combination (unlike
pyrevm, whose wheel matrix stops at Linux cp312), and, more importantly,
the fork state database here is OUR code: the seam where proof-verified
reads (Helios light-client roots + eth_getProof) plug in later without
touching the execution layer. See docs/eth-browsing.md.

Layout:

- ``StateReader`` — "give me account / slot X at the fork block": the
  *only* place that talks to the network, and therefore the test seam
  (tests inject a fake and run the REAL py-evm engine offline) and the
  future verified-fetch seam.
- ``_ForkAccountDB`` — py-evm's ``AccountDB`` with reads falling through
  to a StateReader on local misses. The subtle part is journaling: a
  lazily-seeded slot must NOT be re-fetched after the call frame that
  seeded it reverts, so the "already fetched" markers live in a parallel
  ``JournalDB`` whose checkpoints move in lockstep with the main one.
  That pattern (and the storage-presence probe) is adapted from
  titanoboa's ``AccountDBFork`` (vyperlang/titanoboa, ``boa/vm/fork.py``,
  Apache-2.0).
- ``run_fork_call`` — build a State at a realistic block context (real
  timestamp/baseFee/coinbase — an env-less fork falsely reverts
  time-dependent contracts) and ``apply_message`` an eth_call-shaped
  message: no nonce/signature/balance-for-gas validation, gas price 0.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from eth_utils import to_canonical_address, to_checksum_address

log = logging.getLogger("qeth.pyevm_fork")

_ZERO_ADDR = "0x" + "00" * 20
_HAS_KEY = b"\x01"
_EMPTY = b""
# eth_call-style execution: generous fixed cap, no fee accounting.
_CALL_GAS = 30_000_000


def fork_available() -> bool:
    """Is the local-fork engine importable? (py-evm is an optional
    dependency: ``qeth[simulate]``.)"""
    try:
        import eth  # noqa: F401
        return True
    except Exception:
        return False


class SimulationRevert(Exception):
    """The simulated call reverted. ``output`` carries the raw revert
    payload (ABI-encoded Error(string) / Panic / custom error bytes)."""

    def __init__(self, output: bytes, error: object) -> None:
        super().__init__(f"execution reverted: 0x{output.hex()}")
        self.output = output
        self.error = error


class StateReader:
    """Chain state at a pinned block. Addresses are 0x-hex strings
    (any case), slots/values are ints, code is bytes. Implementations
    must be safe to call repeatedly for the same key (the AccountDB
    caches, but State init may probe system-contract addresses)."""

    def get_account(self, address: str) -> "tuple[int, int, bytes]":
        """-> (balance_wei, nonce, code_bytes)"""
        raise NotImplementedError

    def get_storage(self, address: str, slot: int) -> int:
        raise NotImplementedError

    def prefetch(self, *, from_addr: str, to_addr: str, data: str,
                 value: int) -> None:
        """Optional warm-up hint before executing this call — purely an
        optimization, NEVER a trust or correctness input (anything the
        hint misses is fetched lazily; anything it returns is read
        through the same verified/unverified channel as a lazy fetch).
        Default: no-op."""


# Don't prefetch absurd hint payloads (a runaway accessList) — the lazy
# path covers anything beyond the cap.
_PREFETCH_MAX_KEYS = 512

_static_accounts_cache: "Optional[frozenset[str]]" = None


def _static_accounts() -> "frozenset[str]":
    """Lowercase addresses whose ACCOUNT fields can never influence a
    preview, so fetching them is pure waste (profiled at ~0.2 s each,
    ~9 per simulation — they dominated warm-run latency):

    - precompiles (0x01…0x11): executed natively by the EVM; their code
      and balance are never read by the computation;
    - the Cancun/Prague system contracts: py-evm's State init OVERWRITES
      their code with its own canonical copies the moment the State is
      built — only their STORAGE matters, and that still fetches lazily.

    Addresses come from py-evm's own constants, not hardcoded."""
    global _static_accounts_cache
    if _static_accounts_cache is None:
        from eth.vm.forks.cancun.constants import BEACON_ROOTS_ADDRESS
        from eth.vm.forks.prague.constants import (
            CONSOLIDATION_REQUEST_PREDEPLOY_ADDRESS,
            HISTORY_STORAGE_ADDRESS,
            WITHDRAWAL_REQUEST_PREDEPLOY_ADDRESS,
        )
        addrs = {"0x" + "00" * 19 + f"{i:02x}" for i in range(1, 0x12)}
        addrs |= {"0x" + bytes(a).hex() for a in (
            BEACON_ROOTS_ADDRESS, HISTORY_STORAGE_ADDRESS,
            WITHDRAWAL_REQUEST_PREDEPLOY_ADDRESS,
            CONSOLIDATION_REQUEST_PREDEPLOY_ADDRESS)}
        _static_accounts_cache = frozenset(addrs)
    return _static_accounts_cache


class RpcStateReader(StateReader):
    """StateReader over the chain's JSON-RPC via ``EthClient`` — so
    fork-state reads inherit the UA header and read-failover for free.
    A per-instance memo keeps repeat lookups (State init probes, the
    AccountDB's uncached ``from_journal=False`` paths) off the network.

    ``prefetch`` collapses the lazy path's serial round-trips: one
    ``eth_createAccessList`` (the "which accounts/slots will this call
    touch" hint — Helios serves it natively and proof-fetches the whole
    set concurrently as a side effect) + one BATCHED JSON-RPC POST that
    seeds the memo for every hinted key. Per the probed lesson
    (reference: chainlist.probe_access_list) the hint call carries no
    fee fields. Best-effort: any failure leaves the lazy path intact."""

    def __init__(self, chain: Any, block_id: str) -> None:
        from .chain import EthClient
        self._chain = chain
        self._client = EthClient(chain)
        self._block_id = block_id
        self._memo: dict[tuple, Any] = {}

    def _rpc(self, method: str, params: list) -> Any:
        key = (method, tuple(params))
        if key not in self._memo:
            self._memo[key] = self._client.rpc(method, params)
        return self._memo[key]

    def get_account(self, address: str) -> "tuple[int, int, bytes]":
        if address.lower() in _static_accounts():
            return 0, 0, b""
        addr = to_checksum_address(address)
        balance = int(self._rpc("eth_getBalance", [addr, self._block_id]), 16)
        nonce = int(
            self._rpc("eth_getTransactionCount", [addr, self._block_id]), 16)
        code_hex = self._rpc("eth_getCode", [addr, self._block_id]) or "0x"
        return balance, nonce, bytes.fromhex(code_hex[2:])

    def get_storage(self, address: str, slot: int) -> int:
        addr = to_checksum_address(address)
        raw = self._rpc("eth_getStorageAt", [addr, hex(slot), self._block_id])
        return int(raw, 16) if raw and raw != "0x" else 0

    # --- prefetch ------------------------------------------------------

    def prefetch(self, *, from_addr: str, to_addr: str, data: str,
                 value: int) -> None:
        import time as _time
        t0 = _time.monotonic()
        try:
            entries = self._access_list_hint(from_addr, to_addr, data, value)
            if not entries:
                return
            n = self._batch_seed(entries, from_addr, to_addr)
        except Exception as e:
            log.debug("prefetch skipped: %s", e)
            return
        log.debug("prefetched %d keys in %.2fs", n, _time.monotonic() - t0)

    def _access_list_hint(self, from_addr: str, to_addr: str, data: str,
                          value: int) -> "list[dict]":
        call: dict[str, str] = {
            "from": to_checksum_address(from_addr),
            "to": to_checksum_address(to_addr),
        }
        if data and data not in ("0x", "0X"):
            call["data"] = data
        if value:
            call["value"] = hex(int(value))
        # Straight through the client (the memo can't hash the call dict,
        # and the hint runs once per fork anyway). web3 wraps result
        # objects in AttributeDict — a Mapping but NOT a dict subclass —
        # so duck-type on .get(), never isinstance(x, dict).
        res = self._client.rpc("eth_createAccessList", [call, self._block_id])
        entries = res.get("accessList") if hasattr(res, "get") else None
        return list(entries) if isinstance(entries, (list, tuple)) else []

    def _batch_seed(self, entries: "list[dict]", from_addr: str,
                    to_addr: str) -> int:
        """One batched JSON-RPC request seeding the memo with the account
        triple for every hinted address (+ the sender and target, which
        geth-style nodes omit from access lists as always-warm) and every
        hinted storage slot. Responses map back by request id."""
        import json as _json
        import urllib.request as _u
        from . import USER_AGENT

        payload: list[dict] = []
        keys: list[tuple] = []
        queued: set[tuple] = set()

        def add(method: str, params: list) -> None:
            key = (method, tuple(params))
            if (key in self._memo or key in queued
                    or len(keys) >= _PREFETCH_MAX_KEYS):
                return
            queued.add(key)
            keys.append(key)
            payload.append({"jsonrpc": "2.0", "id": len(keys) - 1,
                            "method": method, "params": params})

        addresses = [to_checksum_address(from_addr),
                     to_checksum_address(to_addr)]
        slot_lists: dict[str, list] = {a: [] for a in addresses}
        statics = _static_accounts()
        for e in entries:
            # AttributeDict-tolerant (see _access_list_hint).
            if not hasattr(e, "get") or not e.get("address"):
                continue
            if e["address"].lower() in statics:
                continue   # get_account short-circuits these anyway
            addr = to_checksum_address(e["address"])
            addresses.append(addr)
            slot_lists[addr] = list(e.get("storageKeys") or [])
        for addr in addresses:
            add("eth_getBalance", [addr, self._block_id])
            add("eth_getTransactionCount", [addr, self._block_id])
            add("eth_getCode", [addr, self._block_id])
            for slot_hex in slot_lists.get(addr, []):
                # Normalise to the exact form get_storage uses, so the
                # memo key matches: hex(int) has no leading zeros.
                add("eth_getStorageAt",
                    [addr, hex(int(slot_hex, 16)), self._block_id])
        if not payload:
            return 0
        req = _u.Request(
            self._chain.rpc_url, data=_json.dumps(payload).encode(),
            method="POST",
            headers={"User-Agent": USER_AGENT,
                     "Content-Type": "application/json"})
        with _u.urlopen(req, timeout=30) as r:
            responses = _json.loads(r.read())
        seeded = 0
        for resp in responses if isinstance(responses, list) else []:
            if not isinstance(resp, dict) or "result" not in resp:
                continue
            idx = resp.get("id")
            if isinstance(idx, int) and 0 <= idx < len(keys):
                self._memo[keys[idx]] = resp["result"]
                seeded += 1
        return seeded


def _fork_account_db_class() -> type:
    """Build the AccountDB-with-remote-fallthrough class. Deferred into a
    function so importing this module without py-evm installed still
    works (``fork_available()`` gates all real use)."""
    import rlp
    from eth.db.account import AccountDB, keccak
    from eth.db.backends.memory import MemoryDB
    from eth.db.journal import JournalDB
    from eth.rlp.accounts import Account
    from eth.vm.interrupt import MissingBytecode
    from eth_typing import Address
    from eth_utils import int_to_big_endian

    class _ForkAccountDB(AccountDB):
        # Injected by _configured_state_class (py-evm instantiates the
        # class itself, with (db, state_root) only).
        _reader: StateReader

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            # "Slot already seeded locally — don't re-fetch" markers.
            # A JournalDB so the markers revert in lockstep with the
            # values they describe (see record/commit/discard below).
            self._dontfetch = JournalDB(MemoryDB())

        # --- accounts -------------------------------------------------
        def _get_account_local(self, address: "Address",
                               from_journal: bool = True) -> Any:
            if from_journal and address in self._account_cache:
                return self._account_cache[address]
            rlp_account = self._get_encoded_account(address, from_journal)
            if rlp_account:
                return rlp.decode(rlp_account, sedes=Account)
            return None

        def _get_account(self, address: "Address",
                         from_journal: bool = True) -> Any:
            account = self._get_account_local(address, from_journal)
            if account is None:
                balance, nonce, code = self._reader.get_account(
                    to_checksum_address(address))
                account = Account(
                    nonce=nonce, balance=balance, code_hash=keccak(code))
            if from_journal:
                self._account_cache[address] = account
            return account

        def get_code(self, address: "Address") -> bytes:
            try:
                return super().get_code(address)
            except MissingBytecode:
                # code_hash is known (from _get_account) but the bytes
                # aren't local yet.
                pass
            _, _, code = self._reader.get_account(
                to_checksum_address(address))
            self.set_code(address, code)
            return code

        def account_exists(self, address: "Address") -> bool:
            if super().account_exists(address):
                return True
            return self.get_balance(address) > 0 or self.get_nonce(address) > 0

        # --- storage --------------------------------------------------
        def _have_storage_locally(self, address: "Address", slot: int,
                                  from_journal: bool = True) -> bool:
            if not from_journal:
                # AccountStorageDB internals: committed-but-unflushed
                # writes land in _locked_changes (cf. titanoboa).
                store: Any = super()._get_address_store(address)
                key = int_to_big_endian(slot)
                return store._locked_changes.get(key, _EMPTY) != _EMPTY
            key = self._get_storage_tracker_key(address, slot)
            return self._dontfetch.get(key) == _HAS_KEY

        def get_storage(self, address: "Address", slot: int,
                        from_journal: bool = True) -> int:
            # super() first for its warm/cold + validation side effects.
            val = super().get_storage(address, slot, from_journal)
            if self._have_storage_locally(address, slot, from_journal):
                return val
            fetched = self._reader.get_storage(
                to_checksum_address(address), slot)
            if from_journal:
                # Seed it (set_storage also marks dontfetch); skipped for
                # journal-bypassing reads so they can't shadow writes.
                self.set_storage(address, slot, fetched)
            return fetched

        def set_storage(self, address: "Address", slot: int,
                        value: int) -> None:
            super().set_storage(address, slot, value)
            key = self._get_storage_tracker_key(address, slot)
            self._dontfetch[key] = _HAS_KEY

        # --- keep the dontfetch journal in lockstep --------------------
        def record(self) -> Any:
            checkpoint = super().record()
            self._dontfetch.record(checkpoint)
            return checkpoint

        def commit(self, checkpoint: Any) -> None:
            super().commit(checkpoint)
            self._dontfetch.commit(checkpoint)

        def discard(self, checkpoint: Any) -> None:
            super().discard(checkpoint)
            self._dontfetch.discard(checkpoint)

    return _ForkAccountDB


def _configured_state_class(reader: StateReader) -> type:
    """The latest-fork State class wired to a reader-backed AccountDB.
    (Always the newest fork py-evm ships: qeth's chains track mainnet
    rules closely, and for a call preview a too-new ruleset only skews
    gas costs, never logs.)"""
    from eth.vm.forks.prague import PragueVM

    db_class = type(
        "_ConfiguredForkAccountDB", (_fork_account_db_class(),),
        {"_reader": reader},
    )
    return type(
        "_ForkState", (PragueVM.get_state_class(),),
        {"account_db_class": db_class},
    )


def run_fork_call(reader: StateReader, block: dict, *, chain_id: int,
                  from_addr: str, to_addr: str, data: str,
                  value: int) -> "list[dict]":
    """Execute one eth_call-shaped message against forked state and
    return its logs as ``decode_event``-ready dicts. Raises
    ``SimulationRevert`` when the call reverts; other VM/state errors
    propagate as-is (the caller's retry loop classifies them).

    ``block`` is the env dict from ``simulate._latest_block``: number,
    timestamp, basefee, gas_limit, coinbase, mix_hash, excess_blob_gas.
    """
    from eth.constants import BLANK_ROOT_HASH
    from eth.db.atomic import AtomicDB
    from eth.vm.execution_context import ExecutionContext
    from eth.vm.message import Message
    from eth_typing import Hash32

    context = ExecutionContext(
        coinbase=to_canonical_address(block.get("coinbase") or _ZERO_ADDR),
        timestamp=block["timestamp"],
        block_number=block["number"],
        difficulty=0,
        mix_hash=Hash32(block.get("mix_hash") or b"\x00" * 32),
        gas_limit=block.get("gas_limit") or _CALL_GAS,
        prev_hashes=(),
        chain_id=chain_id,
        base_fee_per_gas=block.get("basefee") or 0,
        excess_blob_gas=block.get("excess_blob_gas") or 0,
    )
    # Warm the reader before execution: one accessList hint + one batched
    # fetch replaces dozens of serial cold-read round-trips. Optional and
    # best-effort (StateReader's default is a no-op; failures fall back
    # to the lazy path).
    reader.prefetch(from_addr=from_addr, to_addr=to_addr,
                    data=data, value=int(value or 0))

    state = _configured_state_class(reader)(
        AtomicDB(), context, BLANK_ROOT_HASH)

    sender = to_canonical_address(to_checksum_address(from_addr))
    to = to_canonical_address(to_checksum_address(to_addr))
    calldata = b""
    if data and data not in ("0x", "0X"):
        calldata = bytes.fromhex(data[2:] if data.startswith("0x") else data)

    message = Message(
        gas=min(context.gas_limit or _CALL_GAS, _CALL_GAS),
        to=to,
        sender=sender,
        value=int(value or 0),
        data=calldata,
        code=state.get_code(to),
    )
    tx_context = state.get_transaction_context_class()(
        gas_price=0, origin=sender)
    computation = state.computation_class.apply_message(
        state, message, tx_context)

    if computation.is_error:
        raise SimulationRevert(computation.output, computation.error)
    return [
        {
            "address": to_checksum_address(address),
            "topics": ["0x" + topic.to_bytes(32, "big").hex()
                       for topic in topics],
            "data": "0x" + payload.hex(),
        }
        for address, topics, payload in computation.get_log_entries()
    ]
