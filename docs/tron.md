# qeth — Tron support (design note)

**Status:** in progress on the `tron` branch. Phase 1 (the chain-family
refactor) is done. Phase 2 (Tron core: address, protobuf, TronGrid client,
local tx building, hot-wallet signing) is being built. Later phases add
Trezor, send/history UI, TRC-20 and Ledger. The code is the source of truth;
this note records the research the design rests on and the target shape.
Research was done on 2026-09-24 against live mainnet endpoints and primary
sources (java-tron, TronWeb, tronpy, trezor-firmware, app-tron).

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
  becomes `default_account` (that value is `eth_accounts`). Connect and Sign
  Message are off on a Tron view.
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

## Phase 2: Tron core (`qeth/tron/`)

- **`proto.py`**: a minimal proto3 encoder/decoder for `Transaction.raw`,
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

## Later phases

- **Send.** A Tron send dialog for TRX and TRC-20:
  - recipient `T…` (`TronAddressCodec`);
  - fee preview: bandwidth, energy and activation, shown as "burns ≈X TRX",
    with `fee_limit` shown as the maximum;
  - sign → broadcast → pending row.
- **History.** TronGrid `/v1/accounts/{addr}/transactions` +
  `/transactions/trc20`, paged by fingerprint.
  - Transactions have no nonce, so ordering and completeness go by
    block/timestamp. `_is_full_history`'s nonce-contiguity test must not run.
  - A TRC-20 row carries `token_info`.
- **Tokens.**
  - TRX balance: `getaccount.balance`, or `/jsonrpc` `eth_getBalance`.
  - TRC-20 balances: `/v1/accounts/{addr}` `trc20` field, which includes
    spam and gives no decimals.
  - Metadata: `triggerconstantcontract`, or `eth_call` via `/jsonrpc`.
  - Token list: CoinGecko `tokens.coingecko.com/tron/all.json` (base58
    addresses, converted at ingest).
  - Prices: DefiLlama keys `tron:<base58>`; `coingecko:tron` for TRX.
  - Logos: TrustWallet `blockchains/tron/assets/<T…>/logo.png`.
- **Explorer:** `https://tronscan.org/#/transaction/<txid>`,
  `#/address/<T…>`, `#/token20/<T…>`.
- **An address that holds only TRC-20 isn't activated** and can't send until
  it receives TRX. Both `getaccount` and `getaccountresource` return `{}` for
  it; the UI should say so rather than fail.
- **Messages.** TIP-191 (`keccak("\x19TRON Signed Message:\n" + len + msg)`)
  and TIP-712 (EIP-712 with the 0x41 prefix dropped from addresses,
  `chainId` = `0x2b6653dc` on mainnet), for hot wallets.
- **Dapps.** A TronLink-compatible provider means injecting a TronWeb instance
  whose signing methods call qeth, which is a separate project from the
  EIP-1193 bridge. WalletConnect uses `tron:0x2b6653dc` with
  `tron_signTransaction` / `tron_signMessage`.

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
