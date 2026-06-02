#!/usr/bin/env python3
"""Storage-slot cache benchmark for local tx simulation, on chains/RPCs
without eth_simulateV1 / trace-sim (zkSync, TAC, light clients, ...).

Model (what production would do):
  - We simulate txs locally. Each touches a set of storage slots, which we
    keep in a per-contract CACHE.
  - Before simulating the NEXT tx we PREHEAT: one eth_getProof per cached
    account refreshes all its slots' values in a single round-trip (and
    invalidates stale ones).
  - Then we run the local sim; we do NOT know this tx's slots in advance,
    so only the slots already in the cache (from earlier txs) are served
    for free — genuinely-new slots are fetched on demand (one round-trip
    each). The win is pure temporal overlap across our txs.

  WALK  (today) = one eth_getStorageAt per slot the tx touches (serial).
  CACHE         = one eth_getProof per cached account this tx touches
                  (parallel) + one eth_getStorageAt per unseen slot.

The modeled production path uses only local sim + getProof + getStorageAt
(all universal). For the BENCHMARK we need each tx's true slot set; we read
it from the already-mined tx with the prestateTracer — a read of history,
instant on a local archive node (Erigon), used here only as measurement.

  uv run python experiments/cache_sim_bench.py --rpc http://<erigon>:8545 \
      --wallet default
"""

import argparse
import json
import os
import urllib.request
from collections import defaultdict

QETH_CFG = os.path.expanduser("~/.qeth/config.json")
UA = "qeth-experiment/0.1"


def rpc(url, method, params, timeout=60):
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"content-type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    if d.get("error"):
        raise RuntimeError(d["error"])
    return d.get("result")


def slots_of(url, txhash):
    """{addr: set(slot)} the tx touched, from the prestateTracer."""
    r = rpc(url, "debug_traceTransaction",
            [txhash, {"tracer": "prestateTracer"}])
    out = {}
    for a, info in (r or {}).items():
        st = info.get("storage") or {}
        if st:
            out[a.lower()] = set(k.lower() for k in st)
    return out


def etherscan_txlist(wallet, key, chainid, n=3000):
    url = (f"https://api.etherscan.io/v2/api?chainid={chainid}"
           f"&module=account&action=txlist&address={wallet}"
           f"&startblock=0&endblock=99999999&page=1&offset={n}&sort=desc&apikey={key}")
    with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": UA}), timeout=40) as r:
        return json.loads(r.read()).get("result", [])


def bench(url, contract, fill, test):
    print(f"\n=== {contract}  (warm from {len(fill)} txs, test on {len(test)}) ===",
          flush=True)
    cache = defaultdict(set)
    for t in fill:
        for a, s in slots_of(url, t["hash"]).items():
            cache[a] |= s
    print(f"  cache: {len(cache)} accounts, "
          f"{sum(len(s) for s in cache.values())} slots", flush=True)
    print(f"  {'tx':14}{'accts':>6}{'slots':>6}{'WALK':>6}{'CACHE':>7}{'hit%':>6}"
          f"   (preheat getProof + miss)", flush=True)
    rows = []
    for t in test:
        acc = slots_of(url, t["hash"])
        slots = sum(len(s) for s in acc.values())
        if not slots:
            continue
        hits = sum(len(s & cache.get(a, set())) for a, s in acc.items())
        misses = slots - hits
        preheat = sum(1 for a in acc if a in cache)   # getProof / cached touched acct
        walk_rt, cache_rt = slots, preheat + misses
        hr = 100 * hits / slots
        print(f"  {t['hash'][:12]}..{len(acc):6d}{slots:6d}{walk_rt:6d}{cache_rt:7d}"
              f"{hr:5.0f}%   ({preheat}+{misses})", flush=True)
        rows.append((walk_rt, cache_rt, hr))
        for a, s in acc.items():
            cache[a] |= s
    if rows:
        n = len(rows)
        w = sum(r[0] for r in rows) / n
        c = sum(r[1] for r in rows) / n
        h = sum(r[2] for r in rows) / n
        print(f"  -> avg WALK {w:.1f}  CACHE {c:.1f}  ({w/max(c,1):.1f}x fewer "
              f"round-trips, {h:.0f}% hit)", flush=True)
        return (w, c, h, n)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", required=True, help="trace-capable archive RPC (your Erigon)")
    ap.add_argument("--wallet", default="default")
    ap.add_argument("--chainid", type=int, default=1)
    ap.add_argument("--contracts", type=int, default=4)
    ap.add_argument("--fill", type=int, default=12)
    ap.add_argument("--test", type=int, default=8)
    args = ap.parse_args()

    cfg = json.load(open(QETH_CFG))
    wallet = (cfg["default_account"] if args.wallet == "default" else args.wallet).lower()
    key = cfg.get("etherscan_api_key")
    print(f"wallet={wallet}\nrpc={args.rpc}  client={rpc(args.rpc,'web3_clientVersion',[])}",
          flush=True)

    txs = etherscan_txlist(wallet, key, args.chainid)
    by_c = defaultdict(list)
    for t in txs:
        if (t.get("to") and t.get("from", "").lower() == wallet
                and (t.get("input") or "0x") not in ("0x", "")
                and t.get("isError") == "0"):
            by_c[t["to"].lower()].append(t)

    need = args.fill + args.test
    summary = []
    done = 0
    for contract, seq in sorted(by_c.items(), key=lambda kv: -len(kv[1])):
        if len(seq) < need:
            continue
        seq = list(reversed(seq))            # chronological
        r = bench(args.rpc, contract, seq[:args.fill], seq[args.fill:need])
        if r:
            summary.append((contract, len(seq), *r))
        done += 1
        if done >= args.contracts:
            break

    if summary:
        print("\n=== summary (wallet's most-used contracts) ===", flush=True)
        print(f"  {'contract':44}{'#tx':>5}{'WALK':>7}{'CACHE':>7}{'x':>6}{'hit%':>6}")
        tot_w = tot_c = wsum = 0
        for c, ntx, w, ca, h, n in summary:
            print(f"  {c:44}{ntx:5d}{w:7.1f}{ca:7.1f}{w/max(ca,1):6.1f}{h:5.0f}%")
            tot_w += w * ntx; tot_c += ca * ntx; wsum += ntx
        print(f"  {'weighted by usage':44}{'':5}{tot_w/wsum:7.1f}{tot_c/wsum:7.1f}"
              f"{tot_w/max(tot_c,1):6.1f}")


if __name__ == "__main__":
    main()
