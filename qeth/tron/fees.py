"""Tron fee estimation and transaction assembly.

Tron has no gas price. A transaction pays with *resources*, and burns TRX for
whatever its resources don't cover (java-tron ``BandwidthProcessor`` /
``EnergyProcessor``; docs/tron.md has the research):

- **bandwidth** — the signed transaction's size + 64 bytes. Paid from staked
  bandwidth, else the free daily allowance (600), else burned at
  ``getTransactionFee`` sun per byte. Each source is all-or-nothing.
- **account activation** — TRX sent to an address that doesn't exist yet
  costs ``getCreateNewAccountFeeInSystemContract`` (1 TRX), and its bandwidth
  can come only from STAKED bandwidth, else ``getCreateAccountFee`` (0.1 TRX)
  is burned — the free allowance doesn't apply.
- **energy** — contract calls. Estimated by simulating the call
  (``triggerconstantcontract`` ``energy_used``, dynamic penalty included);
  paid from staked energy, else burned at ``getEnergyFee`` sun each.
  ``fee_limit`` caps the energy spend and must be set on the transaction.
- **memo** — a non-empty ``data`` field costs ``getMemoFee`` (1 TRX).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .client import TronClient, TronError
from .tx import (
    Contract, TransferContract, TriggerSmartContract, TronTx, ref_block,
    signed_transaction,
)

# Energy headroom on fee_limit over the simulated need: the dynamic-energy
# factor can move at the 6-hourly maintenance, and running out of energy
# still burns what was spent.
FEE_LIMIT_HEADROOM = 1.5
# A floor for fee_limit (sun): a contract call must be allowed SOME energy
# even when the simulation reports none.
MIN_FEE_LIMIT = 1_000_000
# How long a built transaction stays valid, from the head block's time. Tron
# allows up to 24 h; ten minutes covers a hardware-wallet confirmation while
# still letting a transaction that never lands be called dropped soon after.
EXPIRATION_MS = 10 * 60 * 1000
# Fee schedule cache — it changes by governance vote, not per block.
_PARAMS_TTL_S = 600.0

# Fallbacks if a parameter is missing from a node's answer (the values in
# force on mainnet on 2026-09-24).
_DEFAULT_PARAMS = {
    "getTransactionFee": 1000,
    "getEnergyFee": 100,
    "getCreateAccountFee": 100_000,
    "getCreateNewAccountFeeInSystemContract": 1_000_000,
    "getMemoFee": 1_000_000,
    "getMaxFeeLimit": 15_000_000_000,
}

_params_lock = threading.Lock()
_params_cache: dict[int, tuple[float, dict[str, int]]] = {}


def chain_parameters(client: TronClient) -> dict[str, int]:
    """The chain's fee schedule, cached for ``_PARAMS_TTL_S``."""
    cid = client.chain.chain_id
    with _params_lock:
        hit = _params_cache.get(cid)
        if hit is not None and time.monotonic() - hit[0] < _PARAMS_TTL_S:
            return hit[1]
    params = {**_DEFAULT_PARAMS, **client.chain_parameters()}
    with _params_lock:
        _params_cache[cid] = (time.monotonic(), params)
    return params


class TronAccountNotActivated(TronError):
    """The sender has never received TRX, so its account doesn't exist on
    chain and it can't pay for anything — not even a TRC-20 transfer."""


@dataclass(frozen=True)
class TronFee:
    """What a transaction will cost, in sun unless noted."""
    bandwidth: int            # bytes it will consume
    bandwidth_burn: int       # burned for bandwidth (0 if staked/free covers it)
    energy: int = 0           # estimated energy units (contract calls)
    energy_burn: int = 0      # burned for energy staked energy doesn't cover
    activation: int = 0       # creating the recipient account
    memo_fee: int = 0
    fee_limit: int = 0        # the cap to put on the tx (contract calls)

    @property
    def total_burn(self) -> int:
        return (self.bandwidth_burn + self.energy_burn + self.activation
                + self.memo_fee)


def trc20_transfer_data(to: str, amount: int) -> bytes:
    """ABI calldata for ``transfer(address,uint256)`` — the 20-byte body,
    exactly as on an EVM chain (the TVM drops the 41 prefix)."""
    return (bytes.fromhex("a9059cbb") + bytes(12) + bytes.fromhex(to[2:])
            + amount.to_bytes(32, "big"))


def _bandwidth_bytes(contract: Contract, fee_limit: int, memo: bytes) -> int:
    """Bytes the signed transaction will be charged for: its serialized size
    with one signature, + 64 (java-tron's per-transaction overhead). Built
    with worst-case TaPoS / timestamps, which are the same width as real
    ones."""
    probe = TronTx(contract, bytes(2), bytes(8), expiration=2**42,
                   timestamp=2**42, fee_limit=fee_limit, memo=memo)
    return len(signed_transaction(probe.raw_data(), [bytes(65)])) + 64


def estimate_fee(client: TronClient, contract: Contract, *,
                 memo: bytes = b"") -> TronFee:
    """Estimate what ``contract`` will cost its owner right now. Raises
    ``TronAccountNotActivated`` for a sender with no on-chain account, and
    ``TronError`` when a contract call would revert (simulation fails)."""
    params = chain_parameters(client)
    owner = contract.owner
    if not client.get_account(owner):
        raise TronAccountNotActivated(
            "This address has never received TRX, so it isn't activated on "
            "Tron and can't pay fees yet. Send it some TRX first.")
    res = client.account_resources(owner)
    staked_bw = int(res.get("NetLimit", 0)) - int(res.get("NetUsed", 0))
    free_bw = int(res.get("freeNetLimit", 0)) - int(res.get("freeNetUsed", 0))

    energy = energy_burn = fee_limit = 0
    if isinstance(contract, TriggerSmartContract):
        sim = client.trigger_constant(owner, contract.contract, contract.data,
                                      contract.call_value)
        energy = int(sim.get("energy_used", 0))
        staked_energy = int(res.get("EnergyLimit", 0)) - int(res.get("EnergyUsed", 0))
        price = params["getEnergyFee"]
        energy_burn = max(0, energy - max(0, staked_energy)) * price
        fee_limit = min(max(int(energy * price * FEE_LIMIT_HEADROOM) + 1,
                            MIN_FEE_LIMIT),
                        params["getMaxFeeLimit"])

    size = _bandwidth_bytes(contract, fee_limit, memo)
    activation = 0
    if isinstance(contract, TransferContract) and not client.get_account(contract.to):
        activation = params["getCreateNewAccountFeeInSystemContract"]
        bandwidth_burn = 0 if staked_bw >= size else params["getCreateAccountFee"]
    elif staked_bw >= size or free_bw >= size:
        bandwidth_burn = 0
    else:
        bandwidth_burn = size * params["getTransactionFee"]
    return TronFee(
        bandwidth=size, bandwidth_burn=bandwidth_burn, energy=energy,
        energy_burn=energy_burn, activation=activation,
        memo_fee=params["getMemoFee"] if memo else 0, fee_limit=fee_limit)


def build_tx(client: TronClient, contract: Contract, *, fee_limit: int = 0,
             memo: bytes = b"") -> TronTx:
    """Assemble the transaction with a fresh TaPoS reference: the latest
    SOLID block (java-tron's own default — a head reference can land on a
    fork and fail TaPoS), expiring ``EXPIRATION_MS`` after the head block's
    time (the node checks expiration against head time, not our clock).
    Called right before signing, so a long review can't let it expire."""
    solid = client.solid_block()
    head = client.head_block()
    rb, rh = ref_block(solid.number, solid.block_id)
    return TronTx(contract, rb, rh, expiration=head.timestamp + EXPIRATION_MS,
                  timestamp=head.timestamp, fee_limit=fee_limit, memo=memo)
