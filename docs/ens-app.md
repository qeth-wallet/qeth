# ENS app — design map (not started)

Status: **ideation, 2026-06-13.** A new plugin/tab: browse the ENS names an
account owns as a tree (names → subdomains → records), read records, and edit
them (write transactions). This maps the work; nothing is built.

Two themes run through it:
1. **Plugin isolation.** It's a self-contained plugin in its own tab,
   depending only on the `Host` protocol — addable/removable with one line, no
   other plugin depending on it. The one enabling refactor is a *generic
   transaction-submit host service* (§3), which every future write-plugin
   reuses.
2. **Discover-then-verify — the *same* pattern as token discovery.** Listing
   an address's names needs an indexer (no on-chain enumeration), exactly like
   token discovery leans on Blockscout/Etherscan for the candidate set. The
   indexer is an **untrusted hint**; everything that matters is re-verified
   on-chain — mirroring how qeth re-reads `balanceOf` rather than trusting the
   indexer's balances. For names the verification is even tighter (§2.1).
   So this isn't new trust architecture — it's `TokenSource` applied to names.

## 1. Where it plugs in

qeth's plugin system (`qeth/plugin.py`) already supports this with no
structural change:

- A `Plugin` subclass (`qeth/plugins/ens.py`) with `name = "Names"`,
  `widget()`, `action_widgets()`, and the lifecycle hooks
  (`on_account_changed`, `on_chain_changed`, `on_activated`).
- Mounted with **one call** in `MainWindow._build_central`:
  `self.right_slot.add_plugin(self.ens_plugin, self)` — it becomes a third tab
  next to Tokens / Transactions. (Or its own slot if we want it side-by-side;
  the Slot/tab machinery handles either.)
- It talks back only through the `Host` Protocol. Removing the feature = delete
  the module + that one line. This is the "no backward dependencies" property
  already designed into `Slot`/`Plugin`.

ENS lives on **mainnet** (chain 1) regardless of the wallet's current view —
the plugin pins reads/writes to chain 1 like `qeth/ens.py` already does. (L2
ENS — Base/Linea names via CCIP — is out of scope for v1.)

## 2. Data layer — what's a read vs a write

New module(s): extend `qeth/ens.py` (or add `qeth/ens_records.py`) for the
record read/write logic; keep the **plugin** (Qt/UI) thin over it. Reuse
web3.py's `ens` for namehash + resolver lookup (qeth already does in
`ens.py`).

### 2.1 Discover-then-verify (mirrors `qeth/tokens.py`)

The whole read model is the token-discovery model with names swapped in:

| | Tokens (today) | Names (this plugin) |
|---|---|---|
| **Discover** (untrusted) | `TokenSource` → Blockscout / Etherscan v2 give held-token contracts per address | `NameSource` → ENS subgraph (keyed) / chunked `eth_getLogs` (keyless) / Etherscan. **NB:** Blockscout's NFT-by-owner is *not* reliable for ENS — see §2.1b. |
| **Verify** (no trust) | multicall `balanceOf(contract, owner)` | `namehash(name) == tokenId` (**pure local hash** — proves the name string) **+** `ownerOf(tokenId)` / `registry.owner(node)` (`eth_call`, Helios) **+** resolver record reads (`eth_call`, Helios) |

So build a pluggable **`NameSource`** abstract base mirroring `TokenSource`
(`list_names(chain, address) -> [candidate]`, `supports(chain)`), with
per-source fault tolerance ("one bad source never takes the index down") and
disk caching — the same shape as the token sources. The candidates are hints;
the tree only ever shows names whose `ownerOf` re-confirmed on-chain. Names
verify **more** than tokens do — you check both the name↔tokenId mapping
(locally, free) and ownership (on-chain).

### 2.1a "Owns" is three roles — and an NFT indexer sees only one

A name has **three independent addresses** that can all differ (verified
on-chain with `vitalik.eth`: resolves-to / controller = `0xd8dA…6045`, but the
registration NFT is owned by `0x2208…3a9d` — a separate vault):

| Role | Read | Grants | NFT indexer sees it? |
|---|---|---|---|
| **Registrant** (NFT owner) | `BaseRegistrar.ownerOf` · NameWrapper | transfer, renew, fuses | **Yes** — it *is* the NFT |
| **Controller** | `registry.owner(node)` | set records/resolver, subdomains | **No** — needs subgraph / registry events |
| **Resolved-to** | `resolver.addr(node)` | nothing (just where it points) | **No** (unrelated to ownership) |

So an NFT indexer keyed on the wallet account gives the **registrant** set only.
A name managed from a hot wallet whose NFT sits in cold storage (vitalik's
setup) won't appear via an NFT indexer on the hot wallet — that's the
controller-only gap. v1 accepts it (rare; cover with manual pin); full coverage
wants the subgraph.

### 2.1b Indexer choices — and why Blockscout is the WRONG keyless pick here

Probed live (2026-06-14). **Blockscout's address→NFT-holdings index is
incomplete for ENS** and must not be the enumerator: querying the real
registrant of `vitalik.eth` (`0x2208…`, confirmed via `ownerOf`) returned
**zero** ENS across all its collections — even though Blockscout *does* know
the contract (`tokens/{BaseRegistrar}` → ERC-721 "Ethereum Name Service") and
has the correct *per-instance* owner (`instances/{tokenId}` → `0x2208…`). Its
generic NFT-by-owner aggregation just drops ENS. So unlike token discovery,
Blockscout can't be trusted to *list* an address's names.

There **is** a keyless, working `address → names` enumerator: **BENS**
(Blockscout's *dedicated* ENS microservice — not the generic NFT endpoint,
which under-reports ENS). Don't confuse the two.

| Source (address → names) | Reliable for ENS? | Key required? | Reality (probed 2026-06-14) |
|---|---|---|---|
| **BENS** `addresses:lookup?address=&owned_by=&resolved_to=&only_active=` (`bens.services.blockscout.com`) | **Yes** — purpose-built ENS index | **No** — keyless | **Works.** `owned_by(0xd8dA)` → 47 names incl `vitalik.eth`; `resolved_to` → names pointing here. Both flags **required**. `owned_by` = registry **controller** (not NFT registrant — `owned_by(0x2208 vault)` missed `vitalik.eth`). Lists include poison subdomains → filter for scam + verify on-chain. |
| **ENS subgraph** (`domains(where:{owner})`) | **Yes** — native, complete, fast | **Yes** — Graph key (hosted endpoint sunset 2024) | the "fuller/faster" upgrade, key-gated |
| **ENS metadata service** (`/.../{tokenId}`) | tokenId → name/avatar/**expiry** (NOT enum) | **No** — keyless (verified) | names a tokenId; not an enumerator |
| **`eth_getLogs`** (BaseRegistrar/NameWrapper `Transfer`, tokenId in indexed topic) | on-chain truth | **No** — but needs a getLogs-friendly RPC | **DRPC 400'd every query**; impractical on DRPC, fine on a getLogs-capable RPC + chunking ([[reference_drpc_limit_shape]]) — the permanent fallback |
| Alchemy `getNFTsForOwner` / Reservoir / SimpleHash | yes (also getLogs-friendly RPCs) | **Yes** — API key (Alchemy free tier) | keyed; same rot risk as any hosted service |
| **Blockscout** generic NFT-by-owner (`/addresses/{a}/nft`) | **No — verified gap** | **No** | under-reports ENS — use BENS, not this |

**Recommended v1 (keyless):** **BENS** as the default `NameSource`
(`owned_by` + `resolved_to`), then per name: **scam-filter** the poison
subdomains (reuse the notification scam heuristic) and **verify on-chain**
(`namehash`, `registry.owner`/`ownerOf`, resolver records, expiry). Seed also
from the **primary name** + manual pin (both keyless, zero-trust). The **ENS
subgraph** is the opt-in keyed upgrade (fuller + the registrant/cold-storage
names BENS's controller-view misses); **`getLogs`** is the permanent fallback
for anyone on a getLogs-capable RPC. BENS is still a hosted service (rot
principle, §2.1c) — but keyless, Blockscout-operated, self-hostable, and
already a qeth dependency, so it's a sound *swappable* default, never a hard
dependency (on-chain verification keeps it honest).

### 2.1c Design principle: indexers are swappable, the chain is the index

**Never hard-depend on a hosted backend for enumeration — they rot.** This is
the load-bearing principle, and it's not hypothetical:

- **The Graph's hosted ENS subgraph** — sunset 2024.
- **Reservoir** — wound down.
- **Frame's Pylon** (the hosted NFT/data backend that powers Frame's NFT
  inventory) — Frame itself is effectively defunct: repo `floating/frame` last
  released **v0.6.11 (Feb 2025)**, last commit **Mar 2025**, ~16 months dark.
  A hosted backend whose parent has gone quiet is on borrowed time.

Other wallets "just show your NFTs/names" by *being* the hosted, keyed party
(MetaMask's NFT API, Rainbow API, Rabby→DeBank, Frame→Pylon). That UX is bought
with a dependency that **will** eventually break. qeth is local-first with no
backend, so it can't (and shouldn't) take that bet.

The hedge — and qeth's real advantage — is **discover-then-verify**: the
candidate-set indexer is a swappable, best-effort `NameSource`; *trust* lives in
on-chain reads (`ownerOf`, `namehash`, resolver, `getLogs`) which **never
sunset**. So a worse/dead indexer only costs *completeness*, never
*correctness* — unlike Pylon, whose data Frame must trust outright. Concretely:
make every source pluggable + fault-tolerant (like `TokenSource` already is),
keep **getLogs as the permanent fallback** (works with any getLogs-capable RPC,
forever), and never let a single indexer's death break the feature.

> **Project aside (not ENS-specific):** Frame being defunct also means qeth's
> "Frame-compatible JSON-RPC server" (`:1248`) targets a dormant client. The
> *protocol* (localhost EIP-1193 + open CORS) is still useful to other tools, so
> the feature keeps value — but it's worth **reframing it as a generic
> local-wallet RPC bridge** rather than "Frame compatibility" in the project's
> framing/README. Tracked here since it surfaced during this analysis.

### Reads

| Read | How | Verifiable? |
|---|---|---|
| Names owned by an address (the tree roots) | An indexer. ENS names ARE NFTs but **neither contract is enumerable** (verified on-chain), so no on-chain `tokenOfOwnerByIndex`. **Keyless: BENS** (`addresses:lookup`, §2.1b) — the dedicated Blockscout ENS service, returns owned + resolved-to names. Keyed upgrade: the ENS subgraph. (NOT Blockscout's generic NFT endpoint — it under-reports ENS.) | **No** (indexer) |
| Subdomains of a name | Subgraph (`domain.subdomains`) or `NewOwner` events. NB **unwrapped subdomains are NOT tokens** (registry `owner(node)` only) — an NFT indexer misses them; only the subgraph / registry events surface them. | **No** |
| tokenId → name | tokenId is labelhash/namehash (**irreversible**) — recover the string via the ENS metadata service (`tokenURI`) or the subgraph. | n/a |
| Ownership of a *known* name | `BaseRegistrar.ownerOf(labelhash)` · `NameWrapper.ownerOf(namehash)` · `registry.owner(node)` — `eth_call` | **Yes** (Helios) |
| Owner / resolver of a node | `registry.owner(node)`, `registry.resolver(node)` — `eth_call` | **Yes** (Helios) |
| **Expiry** (see §2.2 — a headline use case) | `.eth` 2LD: `BaseRegistrar.nameExpires(labelhash)`; wrapped: `NameWrapper.getData(node) → (owner, fuses, expiry)`; grace = `BaseRegistrar.GRACE_PERIOD` (90 days). Compare to the latest block timestamp. | **Yes** (Helios) |
| Records: `addr`, `addr(coinType)`, `text(key)`, `contenthash`, `name` | resolver `eth_call`s | **Yes** (Helios, strict — reuse the `ens.py` pattern + the Content-Type fix) |

- **Enumeration** is the honest weak point — same as the ENS app itself
  (`docs/verified-reads.md`). v1 uses the subgraph; be upfront it isn't proof-
  verified. Fallback / complement: let the user **pin names manually** (type
  `foo.eth`) so the tree works with zero indexer trust, and seed roots from the
  account's **primary name** (reverse record, already in `ens.py`).
- **Text records have no on-chain enumeration** — read a curated set of
  standard ENSIP-5 keys (avatar, description, url, email, com.twitter,
  com.github, org.telegram, …) plus any the subgraph reports.
- Per-record reads route through Helios when available → show the same green
  **✓ verified** badge as elsewhere.

### 2.2 Expiry status — a headline use case

"When do my names expire / do I need to renew" is probably *the* reason to open
this tab, and it's a perfect fit: expiry is a **per-name, on-chain, Helios-
verifiable** read — no indexer, so it works in v1 even for a single manually-
pinned name. (Subdomains don't have their own expiry; they ride the parent
2LD's — so a subdomain's effective expiry is its parent's.)

Read `nameExpires` / `getData().expiry`, compare to the latest block timestamp,
and derive a status with the registrar's 90-day grace period:

| Status | Condition | Treatment |
|---|---|---|
| **Active** | `now < expiry − warn_window` | neutral; show the date + "in N months" |
| **Expiring soon** | `expiry − warn_window ≤ now < expiry` | amber; "expires in N days" + **Renew** |
| **In grace period** | `expiry ≤ now < expiry + 90d` | red; "expired — N days left to renew before release" (resolution already stopped) + **Renew** |
| **Released** | `now ≥ expiry + 90d` | grey/struck; "released — anyone can register" |

UX: surface it prominently on each name row (not buried in records), colour-
coded, and make the tree **sortable by soonest expiry** so at-risk names float
up. The **Renew** action is the natural write to pair with it — it ties to
`ETHRegistrarController.renew(label, duration)` (a payable tx; quote the price
from `rentPrice`). Given how central this is, renew is worth pulling earlier
than general ownership writes (see Phasing). Optional later: a passive
notification when a name crosses into "expiring soon" (reusing the desktop-
notification path), since the wallet polls anyway.

### Writes (transactions)

Which contract depends on the operation and whether the name is **wrapped**
(NameWrapper ERC-1155) or **unwrapped** (legacy registry; .eth 2LD in the
ERC-721 BaseRegistrar). Detect wrap state via `registry.owner(node) ==
NameWrapper`.

| Edit | Contract · method |
|---|---|
| Set address / multichain addr | **resolver** · `setAddr(node, a)` / `setAddr(node, coinType, a)` |
| Set text record (avatar, url, …) | **resolver** · `setText(node, key, value)` |
| Set contenthash (for `.eth` sites) | **resolver** · `setContenthash(node, hash)` |
| Set resolver | registry or NameWrapper · `setResolver(node, resolver)` |
| Create subdomain | registry `setSubnodeRecord/Owner` **or** NameWrapper `setSubnodeRecord` |
| Transfer ownership | registry `setOwner` · BaseRegistrar `safeTransferFrom` (unwrapped 2LD) · NameWrapper `safeTransferFrom` (wrapped) |
| Set primary name (reverse) | **ReverseRegistrar** · `setName` / `setNameForAddr` |
| Renew `.eth` | ETHRegistrarController · `renew` |
| Wrap / unwrap | NameWrapper `wrap*` / `unwrap*` |

- **Record edits (resolver) are wrap-agnostic** — the resolver is set on the
  node either way, so `setText`/`setAddr` are identical for wrapped and
  unwrapped names. That's why v1 starts there.
- Build calldata from the resolver/registry ABI (web3 `contract.encode_abi`
  or qeth's `abi.py`). **Do not** sign via web3's account — route through
  qeth's signer (Ledger/hot) via §3.
- **Contract addresses** (Registry `0x0000…2e1e`, NameWrapper, BaseRegistrar,
  PublicResolver, ReverseRegistrar, ETHRegistrarController) — pull from a
  maintained constant or resolve live; don't scatter literals.

## 3. The one enabling refactor — generic tx submit

Today the sign+broadcast flow is reached through `SendTokenDialog` and the RPC
signing bridge (`MainWindow._on_sign_request` → confirm dialog with the event
preview → `_launch_sign_flow`). A `SigningRequest`
(`qeth/signing.py`: `chain_id, from_addr, to_addr, value_wei, data, gas,
nonce`) is already the generic "send this transaction" unit, and the RPC path
already shows a **generic confirm-and-sign dialog with the verified event
preview** for arbitrary dapp txs.

So the ENS plugin should **not** reimplement signing. Add one `Host` method:

```
host.submit_transaction(req: SigningRequest) -> Future[str]   # tx hash
```

— which runs the *same* confirm dialog (event preview, verified badge, gas) +
signer + broadcast the RPC bridge already uses, just initiated in-app instead
of from a dapp. The ENS plugin builds the `SigningRequest` (to = resolver,
data = `setText(...)`) and calls it; the user sees the standard preview ("this
sets text record `url` = …") and signs with Ledger/hot as usual.

This is the load-bearing isolation work: a reusable transaction-submit service
that **any** future write-plugin uses, so no plugin couples to `SendTokenDialog`
or ui internals. (Bonus: the verified-preview decoding already labels ENS
calls if the resolver ABI is known — good free UX.)

## 4. UX

- **Tab "Names".** Tree on top, record panel below (or expandable rows).
- **Tree:** roots = names owned by the selected account (subgraph + manually
  pinned + primary name); expand → subdomains (lazy, fetched on expand);
  expand a name → its records grouped (Addresses · Text · Content · Ownership
  with owner/resolver/expiry).
- **Editing:** inline-edit a record → builds a `SigningRequest` → standard
  confirm/sign/broadcast (§3) → optimistic update + re-read once mined.
  Verified ✓ on reads.
- **Account/chain hooks:** `on_account_changed` reloads the tree for the new
  owner; ENS stays pinned to mainnet so `on_chain_changed` mostly no-ops.
- Empty state: "No names found — paste a name to manage it" (the manual-pin
  path), so it's useful without trusting an indexer.

## 5. Phasing

1. **Read-only v1, expiry-first.** Plugin + tab; tree roots from **BENS**
   (keyless `addresses:lookup`) + primary name + manual pin, scam-filtered and
   on-chain-verified; per-name **expiry status** (§2.2) and records
   (addr, text set, contenthash, owner/resolver) via registrar/resolver
   `eth_call`, Helios-verified. Expiry is the headline value and needs no
   indexer, so it lands in v1. Zero write risk.
2. **`NameSource` discovery.** The real "list my names": a pluggable source
   abstraction mirroring `TokenSource` (subgraph / NFT-indexer / Blockscout),
   fault-tolerant, disk-cached. Candidates are hints — every shown name is
   re-verified on-chain (`namehash==tokenId` + `ownerOf`), exactly as token
   discovery re-reads `balanceOf`. This is the core feature, not an add-on.
3. **Renew + record writes.** The §3 `submit_transaction` host service. Lead
   with **`ETHRegistrarController.renew`** (pairs with the expiry status — the
   most-wanted action), then `setText` / `setAddr` / `setContenthash`
   (wrap-agnostic). Start with the safest, highest-value edits.
4. **Ownership / structure.** Create subdomains, set resolver, set primary
   name, transfer; handle wrapped vs unwrapped (NameWrapper).
5. **Later.** Wrap/unwrap, fuses/permissions, registration of new names, L2/
   CCIP names.

## 6. Open questions / decisions

- **Enumeration trust + source:** all unverified — ENS subgraph, a generic
  NFT-ownership indexer (covers 2LDs + wrapped names, reusable for a future NFT
  tab, but **misses unwrapped subdomains**), or event-scan. vs manual-only
  (verifiable, manual). Lean: manual + primary name in v1; in v2 add an indexer
  as a pluggable source — NFT-indexer if we want NFTs generally, else the ENS
  subgraph for full subdomain coverage. Ownership of any candidate name is
  always re-checked verifiably via `ownerOf`/`registry.owner`.
- **Wrapped-name handling:** detect early; v1 record edits dodge it (resolver
  ops are wrap-agnostic), but ownership ops in phase 4 must branch.
- **Contract-address source:** maintained constants vs live resolution (the
  registry is fixed; resolver/controller versions change). Probably constants
  for the fixed ones, `registry.resolver(node)` live for the resolver.
- **Reuse vs new for the data layer:** lean on web3.py `ens` for namehash +
  resolver lookup (already a dependency); write only the record-set calldata
  builders + the subgraph client ourselves.
- Cross-references: `docs/verified-reads.md` (state-vs-logs, the verified ENS
  resolution this builds on), `docs/eth-browsing.md` (contenthash records feed
  the `.eth` browser — the two features share the resolver-read layer).
