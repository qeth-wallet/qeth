# Storage-slot cache for local tx simulation

Experiments exploring whether a **storage-slot cache** makes pre-broadcast
transaction simulation practical on RPCs that **don't** support
`eth_simulateV1` / trace-sim — i.e. zkSync Era, TAC, light nodes, and most
L2s. There, qeth has to fall back to a local EVM (pyrevm) fork that fetches
each storage slot on demand (`eth_getStorageAt`), one at a time. That cold
"slot walk" is the bottleneck: tens of serial round-trips per tx.

## The idea

We already run the sim locally; every sim *touches* a set of storage
slots. So **keep them in a per-contract cache**. Before the next sim:

1. **Preheat** — refresh the cached slots' values in **one batched read**
   (all slots, one request), invalidating stale ones.
2. **Simulate** — run the local EVM. We don't know this tx's slots in
   advance, so only slots already in the cache (from *earlier* txs) are
   served for free; genuinely-new ("unseen") slots are fetched on demand.

The whole bet is **temporal locality**: do our consecutive txs to a
contract touch overlapping slots? If yes, the cache turns a serial
N-slot walk into one batched refresh plus a handful of misses.

```
WALK  (today) = one eth_getStorageAt per slot, serial.
CACHE         = one batched read of the cached slots (parallel/1 request)
                + one eth_getStorageAt per unseen slot (the misses).
```

Production uses **only** `eth_getStorageAt` / `eth_getProof` / batch — all
universal, no debug/trace/simulate/createAccessList. (For the benchmark we
*measure* each tx's true slot set with the `prestateTracer` — a read of
already-mined history, instant on a local archive node — but nothing in
the modeled production path needs it.)

## Scripts

All read the wallet + etherscan key from `~/.qeth/config.json`.

- **`cache_sim_bench.py`** — overlap & round-trip sweep. For the wallet's
  most-used contracts, warms a cache from earlier txs and reports per-tx
  WALK vs CACHE round-trips and the temporal hit-rate.
  `--rpc <trace-capable archive, e.g. your Erigon> --wallet default`
- **`cache_time.py`** — wall-clock: cold walk vs hot refresh. Slot sets
  from a `--trace` archive node; timing measured against the TARGET rpc
  (arg 1, e.g. DRPC) with only `getStorageAt`/`getProof`.
  `python cache_time.py <target-rpc> <trace-rpc>`
- **`cache_time_batch.py`** — hot-refresh variants (parallel/serial
  `getProof`, batched `getProof`, batched `getStorageAt`) for one contract,
  to find the fastest refresh and dodge concurrency throttling.

## Findings

### 1. Temporal overlap is high — the bet pays off

On a real wallet's four most-used mainnet contracts (hundreds of txs each),
warming from 12 earlier txs and testing on the next 8:

| contract                | hit-rate | WALK rt | CACHE rt | ×    |
|-------------------------|---------:|--------:|---------:|-----:|
| heavy swap (7 accounts) |    81 %  |   37.1  |   11.0   | 3.4  |
| USDC                    |    94 %  |    4.5  |    1.2   | 3.6  |
| USDT                    |    86 %  |    7.0  |    2.0   | 3.5  |
| router (6 accounts)     |    91 %  |   28.0  |    8.6   | 3.2  |

**81–94 % of a tx's slots were already in the cache** from earlier txs.
Most individual txs are 100 % hits (0 misses); the misses cluster on novel
interactions (a new counterparty's balance slot, a path not hit before) —
the predicted bimodal split (your stable slots cached, a fresh
counterparty's slot a miss). The 3.4× is a *round-trip count*; wall-clock
is far better because WALK is serial and the refresh is one request.

### 2. Access-list harvesting is a dead end (negative result)

We first tried to learn slots trace-free from the EIP-2930 access lists
embedded in recent txs. Coverage on a real wallet's contracts was **0–8 %**
— regular contracts/users don't emit access lists (only MEV/arb bots do).
So the cache can't be bootstrapped from others' declared slots; it must
**self-populate from our own local sims**. (Script dropped.)

### 3. Wall-clock: the hot request is ~flat and small

`cache_time` / `cache_time_batch` against your own Erigon (67 ms fetch):

```
heavy 49-slot swap:  WALK 4.06 s (serial)  ->  HOT 0.24 s   (~17×)
```

And the optimal refresh is a **single batched `eth_getStorageAt`** of all
cached slots — *flat in slot count*:

```
43 slots, one request:  batch getStorageAt 0.069 s   (vs 0.98 s serial,
                        batch getProof     0.259 s    vs 8.3 s walk)
```

Two flavors: batched `getStorageAt` (values only — fastest, trust the
endpoint) vs batched `getProof` (verified — for a light-node/trustless
setup). Public free tiers may throttle bursts or cap big batches (DRPC
500s on a 43-call batch) → chunk into ~20-call batches.

### 4. On the real targets — TAC and zkSync Era

Method support (both **lack `eth_simulateV1`** — the reason we're here):

| method               | TAC (`rpc.ankr.com/tac`) | zkSync (`mainnet.era.zksync.io`) |
|----------------------|:------------------------:|:--------------------------------:|
| `eth_getStorageAt`   | ✅ | ✅ |
| `eth_getProof`       | ✅ | ❌ (non-MPT state; use `zks_getProof`) |
| JSON-RPC batch       | ✅ (43-call ok) | ✅ (43-call ok) |
| `eth_simulateV1`     | ❌ | ❌ |
| `debug_traceTransaction` | gated (ankr key) | exists |

Wall-clock, 43 slots:

| chain      | single | WALK (43 serial) | HOT (1 batched) | ×    |
|------------|-------:|-----------------:|----------------:|-----:|
| TAC        | 220 ms |     **9.6 s**    |   **0.13 s**    | ~75× |
| zkSync Era | 233 ms |    **10.8 s**    |   **0.33 s**    | ~32× |

The cold walk is ~10 s on these higher-latency endpoints — the real pain,
and there's no `simulate` to dodge it. The batched hot refresh is
0.13–0.33 s and **flat in slot count**. TAC also supports the verified
`getProof` refresh (0.73 s); zkSync is values-only.

## Takeaway

For an RPC without `eth_simulateV1`/trace-sim, a self-populating
per-contract slot cache + one **batched** refresh turns local simulation
from a ~10 s serial slot-walk into a ~0.1–0.3 s single request, using only
methods those chains actually expose. The misses (unseen slots) are
discovered serially by the local sim but are few (hit-rate 81–94 %) and
bounded.

## Reproduce

```bash
# overlap / round-trip sweep (needs a trace-capable archive node)
uv run python experiments/cache_sim_bench.py --rpc http://<erigon>:8545 --wallet default

# wall-clock against a target RPC (slots from your archive node)
uv run python experiments/cache_time.py https://eth.drpc.org http://<erigon>:8545

# refresh-variant comparison for one contract
uv run python experiments/cache_time_batch.py https://eth.drpc.org http://<erigon>:8545 <contract>
```

Measurements above: mainnet (Erigon 3.4.3 local archive + DRPC), TAC and
zkSync Era public RPCs, June 2026.
