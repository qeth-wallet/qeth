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


class RpcStateReader(StateReader):
    """StateReader over the chain's JSON-RPC via ``EthClient`` — so
    fork-state reads inherit the UA header and read-failover for free.
    A per-instance memo keeps repeat lookups (State init probes, the
    AccountDB's uncached ``from_journal=False`` paths) off the network."""

    def __init__(self, chain: Any, block_id: str) -> None:
        from .chain import EthClient
        self._client = EthClient(chain)
        self._block_id = block_id
        self._memo: dict[tuple, Any] = {}

    def _rpc(self, method: str, params: list) -> Any:
        key = (method, tuple(params))
        if key not in self._memo:
            self._memo[key] = self._client.rpc(method, params)
        return self._memo[key]

    def get_account(self, address: str) -> "tuple[int, int, bytes]":
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
