# qeth — Tron support (design note)

**Status:** on the `tron` branch, usable end-to-end. Built so far:

- the chain-family refactor;
- Tron as a default network;
- TRX and TRC-20 balances, discovery, prices and logos;
- transaction history and activity;
- the send dialog, with signing, broadcast and pending tracking;
- hot-wallet and Trezor signing;
- Trezor Tron discovery;
- `T…` watch-only accounts;
- Tron dapps in the browser (qeth's extension and the Falkon connector): a
  TronLink-compatible provider with transaction, message (TIP-191) and typed
  data (TIP-712) signing, reviewed in qeth.

Mainnet accepts qeth's locally encoded, signed transactions: a transfer
signed by an unfunded key passes the TaPoS and signature checks and fails
only at contract validation ("account does not exist"). A tampered
signature fails at signature validation.

Not built yet (see "Later phases"):

- Ledger's Tron app;
- Keystone;
- Tron message signing on a Trezor (the firmware has none);
- multi-signature transactions;
- TRC-20 approvals;
- staking and voting;
- WalletConnect for Tron;
- testnets.

Hardware signing is untested on a real device. The research was done on
2026-09-24 against live mainnet endpoints and primary sources (java-tron,
TronWeb, tronpy, trezor-firmware, app-tron).

## Why Tron needs a refactor

Every qeth code path assumed an EVM chain:

- **addresses:** `0x` hex, keyed by `.lower()`;
- **transactions:** RLP-encoded with nonce and gas, sent via
  `eth_sendRawTransaction`;
- **native asset:** 18 decimals;
- **indexers:** Blockscout and Etherscan for history.

Tron differs in each of these:

| | EVM | Tron |
|---|---|---|
| Address | `0x` + 20 bytes, EIP-55 | base58check of `0x41` + the **same 20 bytes**, e.g. `T…` (34 characters) |
| Tx encoding | RLP, typed | protobuf `Transaction.raw`, one contract per tx |
| Replay protection | nonce + chain id | TaPoS: `ref_block_bytes` / `ref_block_hash` + `expiration` (≤ 24 h). No nonce, no chain id in the tx |
| Tx id | keccak(signed tx) | **sha256(raw_data)**, known before signing |
| Signature | per tx type | secp256k1 over the txid, 65-byte `r‖s‖v`; v = 0/1 or 27/28 (java-tron accepts both) |
| Fees | gas × price | bandwidth (bytes, 600 free per day, all-or-nothing) + energy (contracts, 100 sun each) + account activation (1–1.1 TRX) + memo (1 TRX). `fee_limit` caps energy |
| Native | 18 decimals | TRX, 6 decimals (sun) |
| Reads | JSON-RPC | TronGrid HTTP (`/wallet/*`, `/v1/*`). There is also a read-only Ethereum JSON-RPC at `/jsonrpc`: `eth_call`, `eth_getBalance` (in sun), `eth_getLogs` over ≤ 5000 blocks. It has no send, nonce or subscribe. |
| Dapps | EIP-1193 (`window.ethereum`) | TronLink's `window.tron.tronWeb`, which injects a whole TronWeb instance, not a request/response provider |

The 20-byte body is `keccak(pubkey)[-20:]` on both families. So **the same
secp256k1 key has the same body on EVM and Tron**, and ABI calldata / event
topics use the 20-byte body on both.

## The core decision: hex inside, base58 at the edges

qeth keeps **one internal address form, `0x` + 20-byte body, for every
family**. Every store record, cache key, calldata word and Qt item role holds
it. `qeth/address.py` converts only at the edges:

- `codec_for(chain).parse(text)`: user input or API output → internal hex.
- `.display(addr)`: internal hex → what the user sees and copies (EIP-55 or
  `T…`).

base58 is case-sensitive, and the codebase `.lower()`s addresses everywhere.
Carrying `T…` strings internally would have meant touching about 240 sites;
the codec touches only the display and input sites. A `T…` string must never
be used as a key.

## Phase 1: the chain-family refactor (done)

- **`Chain.family`** (`"evm"` | `"tron"`), **`Chain.native_decimals`** (18 or
  6) and **`Chain.api_url`** (the TronGrid base) are in `qeth/chains.py`.
  `native_amount(raw, chain)` (`qeth/chain.py`) replaces `wei_to_ether`
  wherever the chain may not be EVM.
- **Account families.** `store.account_families(record)`:
  - A hot wallet serves **both** families, because one key is one body.
  - Every other record serves one family. It's stored as `family` on non-EVM
    records; absent means EVM. A device account's family comes from its coin
    type (60 vs 195), a watch-only account's from the address form it was
    added with.
- **The wallet tree lists only the selected chain's family**, shown in that
  family's form. The filter matches `T…` too.
- **Dapps stay on EVM.** The RPC server serves `Store.dapp_chain()`: the
  current chain when it's EVM, else the last EVM chain (persisted as
  `dapp_chain_id`). Tron isn't listed in `wallet_getEthereumChains`, can't be
  switched to, and doesn't push `chainChanged`. A Tron-only account never
  becomes `default_account` (that value is `eth_accounts`). Sign Message is
  off on a Tron view. Tron dapps get their own provider (Phase 4).
- **A connected account per family.** Double-click / Enter / Connect on a Tron
  view sets Tron's own connected account (`Store.default_for(TRON)`, persisted
  as `family_defaults`). It's marked `[…]` in the tree, leaves the EVM one
  untouched, and is the account Tron dapps are handed (Phase 4). It also
  routes signing when one address is held by two signers.
- **Plugin availability.** `PluginManifest.families` controls which plugins
  show on which chains:
  - ENS and Approvals are EVM-only; their tabs hide on Tron via
    `Slot.set_plugin_available`, with no restart.
  - A hidden plugin gets no broadcasts, and is told the current chain and
    account when it comes back.
  - ←/→ tab cycling skips hidden tabs.
- **Signer capability.** `SignerPlugin.families`:
  - hot: EVM + Tron
  - Trezor: EVM + Tron
  - Ledger and QR: EVM only
  - watch-only can't sign

## Phase 2: Tron core (`qeth/tron/`) (done)

- **`tx.py`**: a minimal proto3 encoder/decoder for `Transaction.raw`,
  `TransferContract` and `TriggerSmartContract`.
  - Fields are written in field-number order.
  - Zero or empty scalars are omitted (proto3).
  - `Permission_id` 0 is omitted.
  - A hand-rolled encoder like this reproduced the node's `raw_data_hex` and
    txid byte-for-byte for TRX and USDT transfers during research.
- **Building locally, never trusting the node's txid.** tronpy's online mode
  signs a txid the server returns, which a malicious node could swap. qeth
  encodes the tx itself, shows the user the fields decoded from those same
  bytes, signs `sha256(bytes)`, and broadcasts the exact bytes with
  `/wallet/broadcasthex`.
- **TaPoS:**
  - Reference the **solid** block (java-tron's default: "head may cause
    TaPoS error"), read from `/wallet/getnodeinfo`; `getnowblock` is 0.5 MB.
  - `ref_block_bytes` = block number bytes [6:8].
  - `ref_block_hash` = blockID bytes [8:16].
  - Expiration is the head block time + 60 s by default.
- **`client.py`: `TronClient`** over the full-node HTTP API.
  - Keyless TronGrid allows about **3 req/s**, and a burst gets suspended for
    4–5 s with a 429.
  - PublicNode (`tron-rpc.publicnode.com`) serves `/wallet/*` +
    `/walletsolidity/*` keyless without that limit. It's the fallback for
    reads and broadcast.
  - Only TronGrid has the indexed `/v1/accounts/…` API (history, TRC-20
    lists).
  - DRPC's free tier can't broadcast Tron.
- **Fees** come from `/wallet/getchainparameters` (`getTransactionFee`
  1000 sun/byte, `getEnergyFee` 100 sun, `getCreateNewAccountFeeInSystemContract`
  1 TRX) and `/wallet/getaccountresource` (free and staked bandwidth/energy).
  - Energy is estimated with `/wallet/triggerconstantcontract` `energy_used`,
    which includes the dynamic-energy penalty. `estimateenergy` is disabled
    on public nodes.
  - USDT costs about 64k energy to an existing holder, about 130k to a new
    one.
  - `fee_limit` = the estimate plus headroom. tronpy's 10 TRX default is too
    low for USDT.
  - The bandwidth charged is the signed tx size + 64 bytes.
- **Status:** `/wallet/gettransactioninfobyid` returns `{}` until the tx is
  included, then `fee`, `receipt.{net_fee, energy_fee, result}`. A pending
  tx is **dropped once its expiration passes** with no info. There's no
  nonce, so there's no replace or cancel.
- **Signing:** `Signer.sign_tron(req)` returns 65 bytes over the txid.
  - Hot wallet: `eth_keys` signs the 32-byte hash directly; no new
    dependency.
  - Trezor: `trezorlib.tron.sign_tx` (below).

## Hardware wallets

**Trezor.** Model T and Safe 3/5/7 with core firmware ≥ 2.11.0 (March 2026)
support Tron: TRX, TRC-20, arbitrary TriggerSmartContract, staking and
voting. The Trezor One does not, and the firmware has **no Tron message
signing**. trezorlib 0.20.2 (already our `trezor` extra) has `trezorlib.tron`:
`get_address` (one call per index, no xpub export), `from_raw_data`, and
`sign_tx`.

- The firmware **re-serializes** raw_data from the fields it's sent, then
  signs its own bytes. So qeth sends `from_raw_data(our_bytes)` and first
  checks that trezorlib's re-encoding equals our bytes. A field the device
  would drop, such as `call_value` before fw 2.12.5, is then refused up
  front instead of producing a bad signature.
- It returns v = 27/28.
- Only 18 built-in TRC-20 tokens get a readable display. Others show the
  contract and raw calldata.
- Path `m/44'/195'/0'/0/i` (Trezor Suite, TronLink). Ledger Live uses
  `44'/195'/i'/0/0`.

**Ledger.** `ledgereth` only speaks the Ethereum app, and there is **no Python
library** for the Ledger Tron app. Tron on Ledger would need its own APDU
client, about 200 lines, over `ledgerblue` inside `run_ledger_hid_job`:

- **Transport:** not `ledgereth.init_dongle`, whose version check rejects the
  Tron app.
- **Transaction signing:** GET_PUBLIC_KEY E0 02 (with chain code, so
  discovery can derive on the host) and SIGN E0 04. Raw_data is streamed in
  chunks of ≤ 250 bytes, split at top-level protobuf field boundaries. The
  device signs the exact bytes, v = 0/1.
- **App settings:**
  - unknown TRC-20s need "Custom contracts";
  - a memo needs "Data allowed";
  - a contract field over about 250 bytes needs blind hash signing (E0 05,
    "Sign by hash").
- **Message signing:** TIP-191 personal messages are shown as a hash only.
- **Status:** deferred to a later phase.

**Keystone 3** signs Tron via the `tron-sign-request` / `tron-signature` UR
types (CBOR tags 5101/5102), including TIP-191 messages. That's a later
phase on top of the existing UR code.

**Keycard Shell:** no Tron support.

## Phase 3: Tron in the UI (done)

The plugins stay family-agnostic wherever the data allows. Tron's read-only
Ethereum JSON-RPC (`Chain.rpc_url`) serves `eth_getBalance` (in sun),
`eth_call` and Multicall3, which has its own Tron address
(`Chain.multicall_address`, `TEazPvZw…`). So the Tokens tab reads TRX,
TRC-20 balances and metadata through the unchanged `EthClient`.

What's Tron-specific:

- **Discovery.** `token_discovery.tron.TronGridSource` reads the `/v1`
  account `trc20` list. It includes spam, which the known-token gate
  filters.
- **Token lists.** CoinGecko's Tron list and the top-tokens head take base58
  addresses, converted at ingest.
- **Prices.** DefiLlama keys Tron tokens as `tron:<T…>`.
- **Explorer links.** `qeth.explorer.explorer_url` builds Tronscan's `#/`
  routes.
- **History.** `tron.history.TronGridTransactionSource` serves Tron history.
  - Rows carry `nonce = -1` and `Transaction.fee`.
  - Lists order by `Transaction.order_key`, which is the timestamp when
    there's no nonce.
  - The paging block cursor maps to `max_timestamp`.
  - Activities come from the TRC-20 transfer index (`tx_activity`).
- **Send.** The SAME composer as on EVM. `TronSendTokenDialog` =
  `tron_send._TronFeesMixin` + `SendTokenDialog`, so it shares:
  - recipient, amount, Max and USD value;
  - the recipient identity row (Tronscan, via `TronIdentitySource`);
  - the decoded call and the Events preview (the node's own simulation,
    `simulate._simulate_tron` over `triggerconstantcontract`);
  - the revert banner and the signing lock.

  Only the fee half differs:
  - a "Network resources" section (bandwidth, energy, activation,
    fee_limit, and what gets burned) in place of gas / fee / nonce;
  - `finalised_tron()` in place of an EVM request.

  `MainWindow._begin_sign` routes a Tron chain to `_begin_tron_sign` →
  `TronSignAndBroadcastWorker`.
- **Pending transactions.** `add_tron_pending`. `PendingProbeWorker`'s Tron
  branch confirms via `gettransactioninfobyid`, re-pushes the signed bytes,
  and drops a transaction once it's past its expiration.
- **Decoding.** Some wallets write addresses in calldata as 21-byte `41…`
  words; they're normalised before decoding
  (`tron.tx.strip_address_prefixes`).
- **Identity rows.** The Contract / Spender rows of the details and sign
  dialogs come from Tronscan on Tron (`TronIdentitySource`, through the same
  `ContractIdentitySource`), with addresses in `T…` form.
- **ABIs.** Neither explorer serves Tron, but java-tron keeps the ABI a
  contract was deployed with. `abi.TronAbiSource` reads it with
  `wallet/getcontract` from the chain's own node: keyless, and it covers
  contracts Tronscan hasn't verified. It normalises Tron's capitalised
  entry kinds, and resolves a proxy through the EIP-1967 & co. slots read
  over `/jsonrpc`. The transactions plugin routes Tron chain ids to it, so
  dapp calls and history decode with parameter names instead of the 4-byte
  database.

## Phase 4: Tron dapps in the browser (done)

qeth's extension (Chrome / Firefox) and the Falkon connector serve Tron dapps
as TronLink does. They inject a real TronWeb whose node traffic and signing go
to qeth. The page side is the shared `provider.js`, so both browsers run the
same code.

- **Always injected, and small.** Every page gets:
  - `window.tron`: TIP-1193, flagged `isTronLink` like TronLink, as
    `isMetaMask` is on EVM;
  - a TIP-6963 announcement;
  - `window.tronLink` / `window.tronWeb`, TronLink's legacy names, only
    where nothing else defined them.
- **TronWeb on demand.** The unmodified npm dist of TronWeb 6.5.1 is shipped
  in `extensions/*/tronweb/`, pinned by sha256 in `build.py`. It's loaded into
  a frame the first time its page uses Tron: an account request, or touching
  `tronWeb`.
  - The extension loads it with `chrome.scripting.executeScript` in the MAIN
    world, targeting the requesting document (Chrome) or frame (Firefox).
    This is why the extension now needs the `scripting` permission.
  - The Falkon bridge finds the requesting frame by a token its relay holds
    in the SafeJsWorld, then uses native `runJavaScript`.
  - Neither route is bound by the page's CSP, and an EVM-only page never
    loads TronWeb.
  - TronWeb leaves two globals behind, `TronWebProto` and `proto`. Its
    generated protobuf code needs them at run time, and TronLink leaves the
    same two.
- **The node goes through qeth.** The instance's HTTP providers call
  `tron_node [path, payload, method]`:
  - `wallet/*` and `walletsolidity/*` go to the Tron chain's full node, with
    failover like `TronClient`;
  - `v1/*` goes to TronGrid's event API;
  - no other path is allowed.
  The page's CSP and CORS don't apply, and it uses the same nodes as the
  wallet. `fullHost` reads as TronLink's (`api.trongrid.io` on mainnet), so
  dapps that compare it keep working.
- **Accounts.** `tron_accounts` / `tron_requestAccounts` return
  `Store.default_for(TRON)` in `T…` form, with no per-site gate (as
  `eth_accounts`). A connect on the Tron view is pushed as the
  `tronAccountsChanged` subscription (the extension) or picked up by the
  poll (Falkon). Sub-frames stay inert until an explicit connect, as on EVM.
- **Transactions.** `trx.sign(tx)` becomes `tron_signTransaction`.
  - qeth decodes `raw_data_hex`, and requires two things: that it re-encodes
    to the same bytes (so nothing goes unshown), and that it matches `txID`.
  - Only Transfer / TriggerSmartContract, single-signature, from the
    connected account, are accepted.
  - The review is `TronSignTransactionDialog`: the dapp sign dialog plus the
    Tron fee mixin, READ-ONLY. It shows the dapp's own fee limit (flagged
    when the estimate exceeds it), any memo, and an expiration countdown;
    TronWeb gives a transaction a minute.
  - `TronSignWorker` signs, checks the recovery, and returns the signature.
    The dapp broadcasts. When it does so through `tron_node`, qeth
    recognises the txid and records the pending row (the bridge's
    `tron_broadcast_seen`). It isn't recorded at signing, because the
    watcher would re-broadcast a transaction the dapp never sent.
- **Messages.** Hot wallets only (`Signer.sign_tron_message`).
  - `signMessageV2` → `tron_signMessage [hex, 2]` (TIP-191).
  - `trx.sign(hexString)` → version 1, TronWeb's fixed `…\n32` header.
  - Its Ethereum-header variant is refused in the page. It would be an
    Ethereum `personal_sign` by the same key.
  - `_signTypedData` → `tron_signTypedData`. `tron.messages` ports TronWeb's
    TIP-712 encoder: `trcToken` keeps its name in the type hash, and
    addresses are accepted in any form.
  - The domain must carry the Tron chain id. Otherwise the signature would
    also be a valid EIP-712 signature for the key's EVM account.
  - Digests are always computed by qeth, from the content shown.
  - The TronWeb 6.5.1 vectors are in `tests/test_tron_dapp.py`.
- **Status views.** `qeth_status` (for qeth's own connectors only; a web
  page is refused) reports two things:
  - the network selected in qeth and that family's connected account;
  - what a given site is presented: the Tron account if it last explicitly
    used the Tron provider (a connect or a signature; the provider's
    automatic reads don't count), else its per-origin EVM chain and account.
  The extension popup (for the active tab) and Falkon's toolbar and status
  dialog (the active window's current tab) show both. They fall back to
  `eth_chainId` / `eth_accounts` on an older qeth.
- **Tests.** The page side runs end to end in real Chromium and Firefox
  (`tests/test_webext_tron_browser.py`, opt-in `-m browser`). It covers:
  - the real extension and TronWeb, the real `RpcServer`, a fake Tron node,
    and TronWeb's own verifiers checking qeth's signatures;
  - a strict-CSP page, and sub-frames;
  - account switching.
  The Falkon injection is tested in a real QtWebEngine
  (`test_falkon_tronweb_engine.py`).

## Later phases

- **Ledger Tron app.** Its own APDU client over `ledgerblue` (see Hardware
  wallets above). Also declare `ledgerblue` as a dependency; today it's only
  transitive.
- **Keystone 3.** The `tron-sign-request` / `tron-signature` UR types.
- **TRC-20 approvals.** The Approvals tab needs a Tron `Approval`-log source.
  TronGrid `/v1/contracts/{addr}/events`, or `eth_getLogs` over `/jsonrpc`
  (≤ 5000 blocks per call).
- **Router commands.** A Universal-Router-style `execute(commands, inputs,
  deadline)` (SunSwap's router) decodes by name, but `commands` / `inputs`
  stay packed bytes. Decoding them needs SunSwap's command set; the Events
  tab already shows what actually moves.
- **Multi-signature.** `trx.multiSign` and `Permission_id` contracts are
  refused today.
- **Staking / voting / resource delegation.** Trezor already supports these
  contract types.
- **Testnets.** Nile / Shasta chain entries. `TRONGRID_INSTANCES` already
  knows them.
- **WalletConnect.** It uses `tron:0x2b6653dc` with `tron_signTransaction` /
  `tron_signMessage`, which map onto the Phase 4 methods.

## Networks

| | chain id | HTTP | JSON-RPC | Explorer |
|---|---|---|---|---|
| Mainnet | 728126428 (`0x2b6653dc`) | `https://api.trongrid.io`, `https://tron-rpc.publicnode.com` | `…/jsonrpc` | tronscan.org |
| Nile | 3448148188 (`0xcd8690dc`) | `https://nile.trongrid.io` | `…/jsonrpc` | nile.tronscan.org |
| Shasta | 2494104990 (`0x94a9059e`) | `https://api.shasta.trongrid.io` | `…/jsonrpc` | shasta.tronscan.org |

A `T…` address is the same on all three; only TaPoS separates the networks.
Chain ids are the low 4 bytes of the genesis blockID (TIP-474), which is also
what `/jsonrpc` `eth_chainId` returns. Nile's id exceeds qint32, and the chain
id signals already use `QULONGLONG`.
