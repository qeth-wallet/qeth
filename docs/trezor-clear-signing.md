# Trezor: clear signing without lookups

Status: **done** on branch `ledger-app` (2026-11-24), verified on Trezor's
emulator (Safe 3 and Safe 5, firmware 2.13.1). The final check is a signature
on a real Safe 3.

The Ledger counterpart is `docs/ledger-clear-signing.md`; the same privacy
rules apply: nothing is fetched because of a signature, and there is no online
fallback.

## What Trezor shows, and what it needs

While it signs, the firmware asks the host for what it needs to show the
transaction (`EthereumDefinitionRequest`: the chain id, the called contract,
and its function selector), and the host answers with a signed definition:

| Definition | Shows | Since |
|---|---|---|
| `eth/chain-id/<id>/network.dat` | the network's name and coin | core 2.6 / legacy 1.12 |
| `eth/chain-id/<id>/token-<address>.dat` | ERC-20 transfer / approve as "5 CRV" | core 2.6 / legacy 1.12 |
| `eth/chain-id/<id>/display-format/<address>-<selector>.dat` | an ERC-7730 description: "Aave DAO: PoolInstance · Supply · 1,000 USDC" | core 2.12.2 (Safe models) |

Some are built into the firmware (Ethereum, Base, USDC, …) and never asked
for. Typed messages (EIP-712) need none: Safe models list every field, a
Trezor One signs the hashes. The Trezor One has no ERC-7730 support.

Every definition is signed by Trezor: a Merkle proof up to a root co-signed
by 2 of 3 production keys. The firmware checks it — and aborts the whole
signature on one that doesn't verify, or that is older than the minimum date
the firmware keeps (before anything is shown). Emulator builds accept Trezor's
development key too, but try the production keys first, so they take the real
definitions.

## What leaked

qeth answered each request by downloading that one file from
`data.trezor.io` (memoized per run). So SatoshiLabs learned, per transaction,
the chain, the contract and the function called — with the IP and the time
(not the amounts or arguments). Trezor Suite does the same.

## The local copy (`qeth/trezor_definitions.py`)

Trezor publishes every definition in one archive:
`https://data.trezor.io/firmware/definitions/definitions.tar.xz` — ~14 MB,
~64,000 files (2026-11-12: 2,860 networks, ~50,000 tokens, 3,843 ERC-7730
descriptions of which 1,232 on mainnet, plus Solana tokens), served with an
ETag.

- **Refresh** (`DefinitionMirror.refresh`): a conditional GET (`If-None-Match`)
  — an unchanged archive is one request answered 304. A new one is streamed
  to disk (size-capped) and split. Driven by
  `plugins/wallets/trezor_mirror.py`'s scheduler, which runs only while the
  store has a Trezor account: hourly checks, a refresh when the copy is a day
  old, and right after Trezor accounts are added. Never because of a signature.
- **Storage**: the Ethereum definitions only, split by chain id mod 32 into
  xz-compressed tar files (`~/.qeth/trezor/definitions/eth-NN.tar.xz`, ~16 MB
  in all). xz shares the Merkle proofs between definitions; one file per
  definition (or a zip) took ~46 MB. A bucket loads in under half a second
  (mainnet's holds ~15,000 definitions) and the two last used stay in memory.
  Each bucket is replaced atomically, so a failed refresh keeps the old copy.
- **Lookup** (`DefinitionMirror.lookup`): local only. Every definition is
  verified on the host against Trezor's production keys before it is sent
  (`verify_definition`, ~5 ms), so a damaged copy can't abort a signature.
  Missing → the device gets none and shows the raw value.
- **Fallback** (`trezor._sign_with_definitions`): if the firmware still refuses
  a definition qeth sent (a `DataError` about a definition — e.g. the copy is
  older than the firmware's minimum date), the transaction is signed again
  without any: the device shows the raw call (with its own "Unknown contract"
  warning) rather than refusing to sign. Only when qeth actually supplied one.

## Testing

- Hermetic: `tests/test_trezor_definitions.py` (refresh / 304 / split /
  tampering / caps / scheduler) and `tests/test_trezor.py` (the fake device
  asks for definitions mid-signature, refuses them, the retry). The sample
  `tests/data/trezor_definitions_sample.tar.xz` holds real production-signed
  definitions (networks 1 and 8453, CRV, Lido `submit`, Aave `supply`).
- Emulator: `scripts/trezor-emu.sh test` runs `tests/test_trezor_emulator.py`
  (marker `trezor_emulator`) on Trezor's own Linux builds of firmware 2.13.1 for
  the Safe 3 (buttons) and Safe 5 (touch), each headless in a small Debian
  container with SDL3 (the binaries are Nix builds; their loader is patched, as
  trezor-user-env does), loaded with the test seed "all" × 12. The debug link
  (UDP, next port) reads the screens and presses; qeth reaches the emulator
  through trezorlib's `TREZOR_PATH`. Every flow: ETH / legacy transfers,
  personal_sign, EIP-712, a rejection, a built-in token, CRV from the copy,
  Lido and Aave clear-signed from the copy, the same without a copy (raw), and
  the refused-definition fallback. The network guard fails any test that
  fetches a definition during a signature.
  `scripts/trezor-emu.sh qeth` launches qeth against an emulator.

## Not covered

- Safe 7 (THP pairing) and the Trezor One on the emulator.
- EIP-712 messages with nested arrays: the firmware refuses them ("Nested
  arrays are not supported"); qeth reports that, it doesn't fall back to the
  hashes.
