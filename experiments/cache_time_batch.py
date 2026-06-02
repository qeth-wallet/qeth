"""Hot-cache refresh, several ways, against a (rate-limiting) target RPC —
to beat concurrency throttling by batching into one request."""
import json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict

UA = "qeth/0.1"
TARGET = sys.argv[1] if len(sys.argv) > 1 else "https://eth.drpc.org"
TRACE = sys.argv[2] if len(sys.argv) > 2 else "http://192.168.81.2:8545"
CONTRACT = (sys.argv[3] if len(sys.argv) > 3
            else "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640").lower()  # WETH/USDC v3 pool
BLOCK = "latest"


def rpc(url, method, params, timeout=60):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body,
        headers={"content-type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read()).get("result")


def batch(url, calls, timeout=90):
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
               for i, (m, p) in enumerate(calls)]
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body,
        headers={"content-type": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def slots_of(txhash):
    r = rpc(TRACE, "debug_traceTransaction", [txhash, {"tracer": "prestateTracer"}])
    out = {}
    for a, info in (r or {}).items():
        st = info.get("storage") or {}
        if st:
            out[a.lower()] = sorted(st)
    return out


def recent_tx_to(contract):
    cfg = json.load(open(os.path.expanduser("~/.qeth/config.json")))
    wallet = cfg["default_account"].lower(); key = cfg["etherscan_api_key"]
    url = (f"https://api.etherscan.io/v2/api?chainid=1&module=account&action=txlist"
           f"&address={wallet}&page=1&offset=3000&sort=desc&apikey={key}")
    with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": UA}), timeout=40) as r:
        for t in json.loads(r.read()).get("result", []):
            if (t.get("to") or "").lower() == contract and t.get("isError") == "0":
                return t["hash"]


def timeit(fn):
    t = time.perf_counter()
    try: fn()
    except Exception as e: return None, repr(e)[:50]
    return time.perf_counter() - t, None


def main():
    txh = recent_tx_to(CONTRACT)
    acc = slots_of(txh)
    nsl = sum(len(s) for s in acc.values())
    print(f"target={TARGET}\ncontract={CONTRACT}\ntx={txh}")
    print(f"{len(acc)} accounts, {nsl} slots\n", flush=True)

    def parallel_proof():
        with ThreadPoolExecutor(max_workers=len(acc)) as ex:
            list(ex.map(lambda a: rpc(TARGET, "eth_getProof", [a, acc[a], BLOCK], 30), acc))

    def serial_proof():
        for a in acc:
            rpc(TARGET, "eth_getProof", [a, acc[a], BLOCK], 30)

    def batch_proof():
        batch(TARGET, [("eth_getProof", [a, acc[a], BLOCK]) for a in acc])

    def batch_storage():
        batch(TARGET, [("eth_getStorageAt", [a, s, BLOCK])
                       for a, slots in acc.items() for s in slots])

    for name, fn in [("parallel getProof (per acct)", parallel_proof),
                     ("serial   getProof (per acct)", serial_proof),
                     ("BATCH    getProof (1 request)", batch_proof),
                     ("BATCH    getStorageAt (1 req)", batch_storage)]:
        dt, err = timeit(fn)
        print(f"  {name:32} {dt if dt is None else f'{dt:6.3f}s'}"
              f"{'  ERR ' + err if err else ''}", flush=True)


if __name__ == "__main__":
    main()
