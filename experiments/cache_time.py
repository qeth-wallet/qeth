"""Wall-clock: cold slot-walk vs hot-cache request (parallel getProof).

Slot sets come from a trace-capable archive node (--trace, your Erigon);
the timing is measured against the TARGET rpc (argv[1], e.g. DRPC) using
only eth_getStorageAt / eth_getProof — so it reflects what a wallet would
actually pay on that endpoint.
"""
import json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict

QETH_CFG = os.path.expanduser("~/.qeth/config.json")
UA = "qeth/0.1"
TARGET = sys.argv[1] if len(sys.argv) > 1 else "https://eth.drpc.org"
TRACE = sys.argv[2] if len(sys.argv) > 2 else "http://192.168.81.2:8545"
BLOCK = "latest"


def rpc(url, method, params, timeout=60):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(url, data=body,
        headers={"content-type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("result")


def slots_of(txhash):
    r = rpc(TRACE, "debug_traceTransaction", [txhash, {"tracer": "prestateTracer"}])
    out = {}
    for a, info in (r or {}).items():
        st = info.get("storage") or {}
        if st:
            out[a.lower()] = sorted(st)
    return out


def etherscan(wallet, key):
    url = (f"https://api.etherscan.io/v2/api?chainid=1&module=account&action=txlist"
           f"&address={wallet}&page=1&offset=3000&sort=desc&apikey={key}")
    with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": UA}), timeout=40) as r:
        return json.loads(r.read()).get("result", [])


def measure_latency():
    z = "0x" + "0" * 64
    usdt = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    t = time.perf_counter(); rpc(TARGET, "eth_getStorageAt", [usdt, z, BLOCK])
    g = time.perf_counter() - t
    t = time.perf_counter(); rpc(TARGET, "eth_getProof", [usdt, [z], BLOCK])
    p = time.perf_counter() - t
    return g, p


def time_walk(acc):
    t = time.perf_counter()
    for a, slots in acc.items():
        for s in slots:
            try: rpc(TARGET, "eth_getStorageAt", [a, s, BLOCK], 30)
            except Exception: pass
    return time.perf_counter() - t


def time_hot(acc):
    t = time.perf_counter()
    def one(a):
        try: rpc(TARGET, "eth_getProof", [a, acc[a], BLOCK], 30)
        except Exception: pass
    with ThreadPoolExecutor(max_workers=max(len(acc), 1)) as ex:
        list(ex.map(one, acc))
    return time.perf_counter() - t


def main():
    cfg = json.load(open(QETH_CFG))
    wallet = cfg["default_account"].lower()
    key = cfg["etherscan_api_key"]
    print(f"target={TARGET}\ntrace ={TRACE}", flush=True)
    g, p = measure_latency()
    print(f"target latency: getStorageAt={g*1000:.0f}ms  getProof={p*1000:.0f}ms\n", flush=True)

    by_c = defaultdict(list)
    for t in etherscan(wallet, key):
        if (t.get("to") and t.get("from", "").lower() == wallet
                and (t.get("input") or "0x") not in ("0x", "")
                and t.get("isError") == "0"):
            by_c[t["to"].lower()].append(t)

    print(f"  {'contract':16}{'accts':>6}{'slots':>6}{'WALK(s)':>9}{'HOT(s)':>9}{'x':>6}",
          flush=True)
    for contract, seq in sorted(by_c.items(), key=lambda kv: -len(kv[1]))[:5]:
        acc = slots_of(seq[0]["hash"])
        if not acc:
            continue
        nsl = sum(len(s) for s in acc.values())
        walk = time_walk(acc)
        hot = time_hot(acc)
        print(f"  {contract[:14]}..{len(acc):6d}{nsl:6d}{walk:9.3f}{hot:9.3f}"
              f"{walk/max(hot,1e-6):6.1f}", flush=True)


if __name__ == "__main__":
    main()
