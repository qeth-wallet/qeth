# Browsing `.eth` natively — design (not yet implemented)

Status: **ideation, 2026-06-11.** Goal agreed, architecture settled, nothing
built. This documents the design so it can be picked up cold.

## Goal

Typing `vitalik.eth` in the browser (Falkon first; ideally any browser on the
machine) renders the same site as `vitalik.eth.limo` — with the URL bar
showing **just `vitalik.eth`** — while removing every trusted intermediary
that limo represents: limo's resolver, limo's IPFS fetch, and the public
gateways. End state: ENS resolution and content retrieval both verified
locally; every remote party reduced to an untrusted courier that can withhold
but cannot lie.

## Architecture

```
URL bar "vitalik.eth"
  │  resolver config (PAC / host-resolver-rules / system DNS) — no rewrite,
  │  the name actually resolves, so the URL bar keeps the bare name
  ▼
local gateway (in qeth — it already runs an aiohttp server)
  │  Host: vitalik.eth
  ├─ ENS contenthash (EIP-1577) via qeth's chain stack ──► CID
  ▼
verified IPFS fetch (trustless-gateway client; local Kubo optional)
  │  every block sha256-checked against the CID locally
  ▼
HTTP response to the browser
```

### 1. Making `.eth` resolve (pick one; all keep the bare name in the URL bar)

| Mechanism | Scope | Port-80 needed? | Notes |
|---|---|---|---|
| PAC: `dnsDomainIs(host,".eth") → PROXY 127.0.0.1:<port>` via `QTWEBENGINE_CHROMIUM_FLAGS=--proxy-pac-url=…` | browser | **no** (proxy port arbitrary) | easiest spike; browser never does DNS for proxied hosts; gateway must accept absolute-URI proxy requests |
| `--host-resolver-rules=MAP *.eth 127.0.0.1` (same env var) | browser | yes | Chromium-internal resolver; maps host→IP only |
| System DNS: dnsmasq `address=/eth/127.0.0.1` or a systemd-resolved `~eth` routing domain | machine | yes | every browser/tool gets `.eth` (`curl http://vitalik.eth/` works); precedent: ENS's own EthDNS/eth.link |

Port 80 (when needed): nftables loopback redirect `:80 → :<port>`, or systemd
socket activation with `CAP_NET_BIND_SERVICE`. Verify the
`QTWEBENGINE_CHROMIUM_FLAGS` pass-through against Falkon's QtWebEngine before
building on it (spike item #1).

Don't use `*.eth.localhost` origin tricks — solving this at the resolver layer
keeps the real name, and real hostnames give per-site origin isolation
(cookies/localStorage) for free.

### 2. ENS resolution (the ~50-line gap in `qeth/ens.py`)

`ens.py` has forward/reverse address resolution; a site needs the
**contenthash** record: `resolver.contenthash(namehash(name))` + EIP-1577
multicodec decode. Live example (resolved 2026-06-11 via the default RPC):

```
vitalik.eth  resolver 0x231b0Ee14048e9dCcD1d247744d114a4EB5E8E63
contenthash  e301 0170 1220 2156fb…   (e301=ipfs-ns, 0170=dag-pb CIDv1, 1220=sha2-256)
           = ipfs://bafybeibbk35rvvgr7y7qynel2lc5s7fihuxvk23ubzzdyv3dpwgma3lahm
```

Long tail, in priority order: `ipns://` (rare for ENS sites; records are
ed25519-verifiable client-side via `?format=ipns-record`), ENSIP-10 wildcard +
CCIP-read names (web3.py covers much of it), swarm/arweave/onion contenthashes
(error page).

### 3. Content fetch — verified client first, Kubo optional

**Primary: trustless-gateway verified client (~500–800 lines, the only real
new code).** Ask any public gateway for `?format=car&dag-scope=entity` (or
per-block `?format=raw`), verify every block's multihash locally, walk the DAG
yourself. Components: CAR parsing (varint-framed blocks — easy), dag-pb (a
~40-line protobuf schema), UnixFS file-chunk reassembly (easy), HAMT-sharded
directories (the one fiddly piece — murmur3 fanout). The `multiformats` PyPI
package covers CID/multihash plumbing.

Because verification is local, gateways are untrusted **by construction** —
race a pool and take whoever answers. The pool must be probed, not hardcoded
(Cloudflare sunset its public gateway; lists rot). Verification cost is noise:
sha256 at ~GB/s, a whole site checks in milliseconds. The expensive half of
IPFS is *retrieval*, and that's exactly what gets outsourced.

**Optional upgrade: local Kubo.** Probe `127.0.0.1:5001`; if a daemon is
running, reverse-proxy `GET /ipfs/<cid>/<path>` to its gateway and get true
P2P retrieval + UnixFS/HAMT/`index.html`/`_redirects`/IPNS handling for free.
Not required: Kubo idles at 200–500 MB RAM with hundreds of live connections
(it serves the network) — too heavy to *require* for a desktop wallet.

**Future "B⁺": delegated routing** (`/routing/v1`, e.g. cid.contact) — find
providers over plain HTTPS and fetch blocks directly from HTTP-speaking
providers. Gateway-free retrieval, still daemonless.

### 4. HTTPS / secure context

There is **no CA-issued cert for `vitalik.eth` anywhere** — public CAs only
issue for ICANN DNS names, and limo's padlock is a cert for `*.eth.limo`, not
the `.eth` name. Authenticity in this stack never came from TLS: it comes from
the ENS record (chain state) + the CID (content hash). limo's HTTPS only
authenticates limo, which you then trust to resolve and fetch honestly — the
local design is strictly stronger.

Consequence: `http://vitalik.eth` is not a Chromium secure context (no service
workers / `crypto.subtle`). Static IPFS sites are unaffected. v2 shim: a local
**name-constrained CA** (`nameConstraints: permitted DNS=.eth`, mkcert-style),
gateway mints per-name certs on the fly → real `https://vitalik.eth`; the name
constraint caps the blast radius of a stolen CA key to `.eth`. Typed-URL
HTTPS-first upgrades are a non-issue meanwhile (connection-refused on loopback
falls back to http instantly).

## Trust model

| Layer | Courier (untrusted) | Local verification | Residual trust |
|---|---|---|---|
| Consensus | beacon API endpoint | sync-committee sigs (Helios) | one honest checkpoint / ~2 weeks |
| ENS state | any `eth_getProof` RPC | merkle proof vs verified root (Helios) | the ENS contracts |
| Content | any IPFS gateway | CID hash check | the name's owner |

**The RPC is the last unverified link** (and the most important one): IPFS
verification only pins content to a CID, but the CID itself comes from an
`eth_call` taken on faith — a compromised RPC returns a malicious contenthash
and the attacker's site then verifies *perfectly*. (Same exposure, higher
stakes, already exists for ENS-name **sends** in the wallet.)

- **Interim (~20 lines):** require a quorum of N independent RPC providers to
  agree on resolution reads — qeth's multi-RPC infrastructure already exists.
  Not cryptographic, but turns "hack one RPC" into "hack several at once".
- **Endgame: Helios** (since **built** — the sidecar + verified mode now ship
  and back tx-preview simulation and verified ENS resolution; see
  `docs/verified-reads.md`. What remains for `.eth` browsing specifically is
  wiring contenthash resolution through it.) — a localhost sidecar that follows
  the sync-committee
  light-client protocol from a weak-subjectivity checkpoint, then serves
  `eth_call` by fetching state from an untrusted RPC **with EIP-1186 proofs**
  verified against the verified state root. Light (single Rust binary, tens of
  MB, syncs in seconds). qeth integration is near-zero code: `chain.py` is the
  RPC seam, so a "verified mode" is just pointing the chain at Helios's port —
  hardening *all* wallet reads, not only `.eth` browsing. The checkpoint
  source is the one trust input worth scrutinizing rather than defaulting.

### Helios integration shape (grounded in the a16z/helios repo, 2026-04)

Settled by reading the source, not guessing:

- **Sidecar process, not a Python library.** There is **no Python binding**
  (no pyo3/maturin anywhere in the tree). The only non-Rust binding is
  `helios-ts` — a `cdylib` compiled to **WASM** for JS hosts (assumes
  fetch/timers), not usable from Python. The maintained consumption modes are
  exactly two: the **standalone CLI binary** that serves JSON-RPC, and the
  **Rust `Client` / `HeliosApi` library** for Rust hosts. Rolling our own
  PyO3 wrapper would mean a Rust toolchain in CI + per-arch compiled wheels —
  fighting qeth's "system PySide6, vendor pure-Python" packaging (the `.deb`
  already compiles PySide from source; that's enough). Bonus: process
  isolation means a Helios hang/crash mid-sync leaves the wallet up and
  `chain.py` falls back to direct RPC — an in-process library crash couldn't.
- **Talk over loopback TCP, not a pipe — Helios decides this for us.** Its RPC
  server is `jsonrpc::start(client, addr: SocketAddr)` → jsonrpsee
  `ServerBuilder::default().build(addr)`, i.e. HTTP+WS on one TCP socket.
  There is **no Unix-domain-socket / IPC / stdio server** in the codebase; the
  CLI exposes only `--rpc-bind-ip` (default `127.0.0.1`) and `--rpc-port`. A
  pipe would require forking Helios or a translating shim — pointless. And TCP
  loopback is the right answer anyway: `chain.py`'s `HTTPProvider` speaks it
  natively, so verified mode is literally `http://127.0.0.1:<port>` with zero
  new transport code. Security surface is the same as qeth's own Frame server
  on `:1248`, and Helios serves read-only verified data (no keys). (A UDS's
  one advantage — isolation from *other local users* — is moot; Helios doesn't
  offer one.)
- **Spawn/supervise with `QProcess`** (the process analog of the long-lived
  `QThread`s qeth already manages):
  `helios ethereum --execution-rpc <untrusted> --rpc-bind-ip 127.0.0.1 --rpc-port <chosen>`.
- **Readiness gate:** Helios must sync from the checkpoint before it can
  answer, so verified mode needs a "warming up → ready" state. Helios
  implements `eth_syncing` — that's the clean readiness probe.
- **Binary distribution:** probe for an installed `helios` first (same
  "Kubo-optional" pattern as the content layer above), bundle per-arch as a
  fallback.
- **The access-list groundwork already fits:** Helios implements
  `eth_createAccessList` as a first-class method — the untrusted-RPC
  slot-prefetch hint `qeth/chainlist.py::probe_access_list` now probes for.
- **`verifiable-api` (a newer Helios crate)** is a REST API that wraps the
  execution layer with verifiable responses, splitting heavy proof-fetching
  (server, next to the untrusted RPC) from a thin verifying client. Doesn't
  change the sidecar+loopback conclusion for a desktop wallet (you still run
  the verifying half locally), but it's the building block if a shared
  verified endpoint is ever wanted instead of per-machine sync.

## Phasing

1. **Spike:** verified-CAR fetcher in isolation (fetch vitalik's CID, verify,
   walk, dump `index.html`) — proves the only hard new code. Then contenthash
   in `ens.py`, the Host-routing gateway in qeth, PAC glue, and see
   `http://vitalik.eth/` render in Falkon.
2. **v1:** gateway pool probing, error pages (bad contenthash / all gateways
   down), IPNS, directory `index.html`, RPC quorum for resolution.
3. **v2:** name-constrained local CA (secure context), `_redirects`, system
   DNS option, delegated-routing retrieval.
4. **Endgame:** Helios verified mode.
