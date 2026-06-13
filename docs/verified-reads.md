# Verified reads — what the wallet can prove, and where it can't

Status: **2026-06-13.** Verified ENS resolution is **built and shipped**;
verified tx-preview simulation was already built (see
`docs/eth-browsing.md` and the simulate/helios modules). This doc captures the
trust-model reasoning behind those and sketches the next candidate
(verified approvals), so the design can be picked up cold.

The thread running through all of it: a light client (Helios) proves **state**
cheaply and proves **logs / indexer output** essentially not at all — so the
features worth building are the ones that reduce to state reads, and the place
to spend verification is the **signing boundary**, not the dapp's UI.

## 1. What's built: verified ENS resolution

ENS resolution is pure `eth_call`s (registry → resolver → `addr`) against
mainnet contracts — fully provable. When a mainnet Helios sidecar is
available, `qeth/ens.py` routes resolution through it, so the
`name ↔ address` mapping is proof-verified against sync-committee-verified
state instead of trusted from a remote RPC.

- `verified_resolve_address` / `verified_lookup_name`: Helios-first, **strict**
  — `w3.provider.global_ccip_read_enabled = False` so an offchain (CCIP /
  `OffchainLookup`) name can't follow an unverifiable HTTP-gateway hop. On
  success → verified; otherwise fall back to the public RPC marked
  **unverified** (the name still resolves, just without the badge).
- The workers take a `Chain` and emit a 3rd `verified: bool`. Forward resolves
  (user typing a recipient) wait ~8 s on a warming sidecar; mass reverse-label
  lookups pass `wait_s=0` (verify only if already synced — never block a
  label).
- **Indication**: green `✓ verified` pill (same as the Events-tab badge) on the
  Send recipient and Add-account forward resolution; ✓ + tooltip on
  Ledger-scan rows; tooltip on ENS-named account-tree rows. Green = verified;
  unverified resolutions get a neutral style so green never overclaims.

Gotcha that cost us: Helios's JSON-RPC server **415s a POST without
`Content-Type: application/json`**, and web3's `HTTPProvider` drops that header
when you pass a custom `headers` dict — so the verified path silently 415'd and
fell back to unverified (correct address, but `verified=False`). Fixed in
`_make_w3`. Keep in mind for **any** future web3-through-Helios code.

## 2. Where wallet verification reaches — and where it doesn't

### Dapps read with their *own* RPC, not the wallet's

ens.domains and revoke.cash (and essentially every consumer dapp) split two
transports: a **public client** (their Infura/Alchemy/own RPC) for all reads —
balances, allowances, ENS records — and the injected **wallet provider** only
for `eth_requestAccounts` / `eth_chainId` / `eth_sendTransaction`. revoke.cash
proves it: it works **fully read-only with no wallet connected** (paste any
address, see its approvals). On top of that, much "reading" is an
**indexer/subgraph** (The Graph), not live RPC at all.

Consequence: the wallet **cannot verify what those sites display**. Chasing
that is a dead end. Two levers actually exist:

1. **Verify at the signing boundary (already done).** Whatever a site renders,
   the consequential action arrives as `eth_sendTransaction`. qeth's verified
   tx preview simulates it against Helios-verified state and shows the decoded
   effect — independent of any claim the site made. A phishing clone of
   revoke.cash can show a fake "harmless" approval, but the approval you
   *sign* is previewed against verified state.
2. **Serve verified responses for the rare dapp that reads through the wallet
   provider.** qeth's RPC server (`:1248`) could answer `eth_call` from Helios.
   Free verified reads for those dapps, no cooperation needed beyond them
   choosing to read through the wallet. Not built; cheap once verified mode is
   wired into the RPC proxy.

ENS nuance: the ENS app resolves `name → address` **before** the tx reaches the
wallet, so qeth only ever sees the raw `0x…` recipient — wallet-side ENS
verification only protects qeth's **own** send-to-ENS flow (where it controls
the resolver read). For a dapp's flow the name→address step already happened in
the site's trust domain; the backstop is the verified recipient/effect at
signing.

### State verifies cleanly; logs / subgraphs essentially don't

A light client gives **verified state** — account/storage/`eth_call` via
EIP-1186 Merkle proofs against the verified state root. It does **not** cheaply
give verified **logs**: a `getLogs` scan would require proving each block's
`receiptsRoot` and reconstructing receipts across a range — no light client
does this for arbitrary ranges. And subgraph output has *no* on-chain proof.

This asymmetry decides feasibility:

- ENS resolution (resolver `eth_call`) ✓ provable
- "current allowance for token → spender" (storage/`eth_call`) ✓ provable
- "find *all* my approvals" (Approval **logs**) ✗ not cheaply provable
- ENS name lists / history (subgraph) ✗ not provable

It's also why qeth's live watcher treats Transfer logs as **hints only** and
re-reads `balanceOf` (verifiable state) for the authoritative number.

### In-browser light clients exist (just not in mainstream dapps)

Not vaporware — a few teams ship it: **Kevlar** (sync-committee LC as an
in-browser RPC proxy), **Helios** (Rust, WASM-friendly, runs in the browser),
**Lodestar light client** (TypeScript, in-browser, verifies via EIP-1186). They
all verify *state*; the log/subgraph-heavy features that ens.domains /
revoke.cash are built on are exactly what they can't verify, which is a big
reason neither bothers. qeth uses the same Helios, as a native sidecar rather
than WASM.

## 3. Next candidate: verified approvals (design sketch, not built)

A revoke.cash-style "what have I approved" view — but verifiable, because
`allowance(owner, spender)` is a state read.

**Speed is not the problem.** Unverified: a couple of Multicall3 batches over
~100 tokens, sub-second. Verified: the cost scales with **distinct
(token, spender) storage slots** (each needs a proof), not call count —
~hundreds of slots ≈ ~1 s cold (extrapolating the simulator's 24-account /
109-slot seed warming in ~0.2 s at 20-wide concurrency), near-instant warm.
Brute-forcing a big spender × token cross product is the only way to make it
slow.

**The hard part is sourcing `(token, spender)` candidates without logs.** You
can enumerate tokens (holdings + curated lists); you can't enumerate spenders
without an Approval-log scan (the un-provable part). Two sources that stay on
the verifiable-state path:

1. **The user's own tx history** (qeth already caches it) — every `approve` /
   `Permit2.approve` they ever sent gives the exact `(token, spender)` pairs,
   then verify the *current* allowance via state proof. Derives candidates from
   your own outgoing txs instead of a global log scan.
2. **A curated known-spender list** — Permit2, Uniswap routers/Universal
   Router, 0x, 1inch, CoW, Odos, Paraswap, major lending pools — swept across
   held tokens.

Residual gap: an approval made via *another* wallet to an *off-list* contract.
Be honest in the UI about coverage ("this wallet's history + known protocols")
rather than implying completeness.

**Permit2 is two layers**: a lot of modern approvals are `token → Permit2`
(one ERC-20 approve), after which Permit2 holds the per-spender allowance with
an expiry. A complete picture needs both `ERC20.allowance(owner, Permit2)` and
`Permit2.allowance(owner, token, spender)` — both state, both verifiable, just
two passes.

## See also

- `docs/eth-browsing.md` — the `.eth`/IPFS browsing design; same Helios
  sidecar, same "RPC is the last unverified link" trust model, applied to
  contenthash resolution + verified content fetch.
- `docs/ARCHITECTURE.md` — the chain seam (`chain.py`) verified mode plugs into.
