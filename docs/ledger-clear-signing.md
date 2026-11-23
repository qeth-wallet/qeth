# Ledger: own app client, then clear signing without lookups

Status: **M1–M2 done** on branch `ledger-app` (2026-11-24); M3 next.

Two goals, in this order:

1. Replace `ledgereth` with a small Ledger Ethereum-app client of our own
   (`qeth/ledger_app.py`), straight on `hidapi`.
2. On top of it, make the Ledger show what is being signed (token transfers,
   approvals, EIP-712 messages, later ERC-7730 contract calls) **without telling
   any server what the user is about to sign**.

Devices that must work throughout: **Nano S** (original) and **Nano X**.

## Why leave ledgereth

- Unmaintained: last release 0.10.0 (2024-11), no commits since.
- Pins `ledgerblue==0.1.48` exactly, blocking upstream fixes.
- Has none of what clear signing needs: no token-info command, no full EIP-712
  (hash-only), no challenge / certificate loading, no store-only / start-flow
  signing modes.
- 28 locked packages exist only because of it, incl. Bluetooth/NFC stacks
  (`bleak`, `nfcpy`, `dbus-fast`, `winrt-*`, `pyobjc-*`) and the three
  sdist-only packages (`ledgerblue`, `pydes`, `python-u2flib-host`) behind the
  pinned-setuptools builds.

Not chosen:

- Ledger's `ledger_app_clients.ethereum` as a dependency (it requires
  `ragger[speculos]`, a test framework). It is Apache-2.0 and is the reference
  we port encoders from, with attribution.
- `ledgercomm` as the transport: its HID reader loops forever if the device
  stops mid-reply, validates packets with `assert` (stripped under `-O`),
  skips continuation sequence checks, doesn't pad the last report (Windows
  needs full reports) and doesn't expose the product id we need to tell a
  Nano S from a Nano X. Ledger's framing is ~60 lines; we own it. Specs: `LedgerHQ/app-ethereum`
`doc/apdu.md`, `doc/eip712.md`, `doc/gcs.md`, `doc/tlv_structs.md`.

## Privacy rules (apply to every milestone)

1. **Nothing is fetched because of a signature.** Metadata is mirrored in bulk
   on a schedule (daily, jittered) and looked up locally.
2. **No online fallback.** Missing locally → the device behaves as today (blind
   sign); qeth's own dialog still decodes and simulates.
3. **Never send calldata, a selector, or a recipient** to a metadata service.
   That rules out Ledger's proxy lookup (POSTs calldata), "transaction checks",
   signing reports, and challenge-bound trusted names (ENS on device).
4. The mirror runs only when the store has a Ledger account.
5. Signed blobs need no trusted source — the device verifies them. qeth also
   verifies the legacy token signature on the host before use, so a corrupt
   mirror is dropped, not sent.

## Architecture

```
qeth/ledger_app.py       Qt-free. Transport protocol + EthApp commands + status words.
qeth/ledger_metadata.py  Qt-free. Bulk mirror + local lookup of signed metadata.
qeth/ledger.py           LedgerSigner / LedgerWorker (unchanged surface), now on EthApp.
qeth/ledger_hid.py       The one device thread (unchanged rule); cache-clear hook removed.
```

- **Transport**: a two-method protocol (`exchange(apdu) -> (sw, data)`,
  `close()`). Implementations: `HidTransport` (hidapi, production),
  `TcpTransport` (the Speculos emulator; `QETH_LEDGER_SPECULOS=host:port` also
  points qeth itself at one), and in-memory fakes for unit tests. Opened and
  closed inside each `run_ledger_hid_job`, so no handle outlives a job — which
  is what the ledgereth cache-clear hack emulated.
- **Signatures are verified on the host**: the y-parity is recovered against
  the account being signed for, never read from the device's `v` byte (legacy
  txs get only the low byte of the EIP-155 `v`). A signature that doesn't
  recover to the account is refused.
- **Several Ledgers** (Nano S + Nano X plugged in together): signing uses the
  one that derives the account, skipping a locked one; discovery refuses to
  guess.
- **EthApp**: one method per command; chunking and path encoding in one place.
  Capabilities are derived once per job from `GET APP CONFIGURATION` (version +
  flags: blind signing enabled, token info wanted) and the USB product id
  (model). Every optional step is gated on a capability, never on a model name
  alone.
- **Errors**: our own status-word table → the same user sentences
  `_explain_ledger_error` produces now (rejected, locked, app not open, asleep
  `0x55xx`, blind signing disabled, unknown SW shown in hex).
- **Transaction bytes**: unsigned RLP built by the helpers Trezor and QR already
  use (`signing.signed_eip1559_tx` / `signed_legacy_tx` assemble the result).
- **Metadata failure never aborts a signature**: any refused or missing
  metadata step falls back to the plain sign.

## Testing

- **Unit (gate)**: golden APDU vectors captured from ledgereth
  (`tests/data/ledger_golden_apdus.json`) and `SoftLedger`, an in-memory
  Ethereum app signing with real keys (`tests/test_ledger_app.py`).
- **Speculos (not a gate, like `scripts/wine.sh`)**: Ledger's emulator running
  the real Ethereum app — built with Ledger's app-builder container at the last
  Nano S release (1.16) and the current Nano X one — driven over TCP with its
  REST API pressing the buttons. Both run in containers (the Speculos image
  bundles `qemu-arm`), so no host qemu is needed.
- **Real devices**: one short final check per device of the USB layer only.

## Milestones

Each one is shippable on its own and ends with `uv run pytest` green.

### M0 — Device facts (read-only probe)

`scripts/ledger_probe.py`: model, app version, config flags, nothing signed.
Run on both devices to pin down what the plan currently assumes:

| Assumption to confirm | Nano S | Nano X |
|---|---|---|
| Newest Ethereum app | ≤ 1.16 (support removed in 1.17) | current (1.24) |
| Legacy token info (`P1=00`) | yes | yes |
| Full EIP-712 (V1) | believed **no** → hash-only stays | yes |
| Ledger PKI certificates | believed no | yes |
| ERC-7730 generic parser | no | yes (≥ 1.14) |

### M1 — `ledger_app.py`: transport, address, config, errors ✅

- Transport protocol + HID (hidapi) / TCP (Speculos) + fakes.
- `get_address(path)`, `get_configuration()`, status-word table.
- Tests: golden APDU bytes for each command, checked against what ledgereth
  emits while it is still installed; error-table tests.

### M2 — Signing parity, then switch ✅

- `sign_transaction` (legacy + EIP-1559, chunked), `sign_personal_message`,
  `sign_eip712_hashed` (V0).
- Unit tests: APDU streams byte-identical to ledgereth for a fixture set
  (short/long calldata, chunk-boundary cases incl. the known "RLP ends exactly
  on a chunk edge" trap, high chain ids).
- Device check on Nano S and Nano X: same address, and signatures that recover
  to it, for tx / message / typed data.
- Switch `ledger.py` (signer, discovery, availability probe) to `EthApp`.

### M3 — Remove ledgereth

- `pyproject.toml`: drop `ledgereth` (`hidapi` is already declared); `uv lock`,
  `uv audit`, `uvx deptry .`.
- The mypy `ledgereth.*` override (the code-side removals landed with M2).
- Packaging, each verified: AppImage / Flatpak (`dist/lock-export.sh` — the
  sdist-only list shrinks), rpm spec + launcher, both debs, Windows + macOS
  workflows (PyInstaller hidden imports), Gentoo overlay (separate repo).
- Docs: CLAUDE.md, `docs/ARCHITECTURE.md`, `docs/tron.md` (its Ledger-Tron note
  assumes `ledgerblue`; it becomes "another app class on the same transport").

### M4 — Token info: sending tokens, approve, revoke shown clearly

- `ledger_metadata.py`: mirror Ledger's token list per network
  (`cal/v1/tokens?network=…&output=contract_address,descriptor`, cursor from
  the `x-ledger-next` header; mainnet ≈ 15k tokens / 7 MB raw) into a compact
  `~/.qeth/ledger/tokens/<chain_id>.json` (address → descriptor + signature).
  All of qeth's EVM chains are mirrored, not just the ones in use.
- Before signing a tx whose `to` (or ERC-20 `approve`/`transfer` target) is in
  the mirror: `PROVIDE ERC 20 TOKEN INFORMATION` (legacy format — works on both
  devices, no certificate), then sign.
- Sign dialog line, known before the device is touched: "Ledger will show:
  USDC transfer" / "Ledger will ask to blind sign" (+ "enable blind signing in
  the Ethereum app" when the config flag says it is off).
- Tests: mirror paging/compaction with a fake HTTP layer (hermetic), host-side
  signature check, APDU order; Speculos run for the screen text where feasible.

### M5 — Full EIP-712

- Send the whole typed message so the device lists domain + every field
  (spender, amount, deadline). V1 protocol for older apps, V2 for app ≥ 1.24.
  Where unsupported (Nano S, per M0) the hash-only path stays, and the sign
  dialog says the device will show hashes only.
- Without Ledger's labels this is still behind the app's blind-signing /
  raw-messages setting; it is the values on the trusted screen that matter.

### M6 — ERC-7730 contract calls on the device (blocked on Ledger)

Needs Ledger's partner "origin token" (`cal/v1/dapps` is 403 without it).
Questions for Ledger before any code:

1. Can the full signed descriptor set be downloaded in bulk?
2. May qeth redistribute it from its own mirror (CI-built bundle, token kept
   in CI rather than in every build)?

If both are yes: mirror descriptors + certificates, then the store-only →
operation info → fields → start-flow sequence (`doc/gcs.md`). Nano X only.
Proxy-filed descriptors are skipped (rule 3).

## Out of scope here, tracked separately

- **Trezor**: serve definitions from `definitions.tar.xz` via trezorlib's
  `TarSource` instead of per-signature fetches (removes an existing leak).
- **qeth's own sign dialog**: render ERC-7730 from a local mirror of
  `ethereum/clear-signing-erc7730-registry`, EIP-712 first.
- Ledger Tron app; Bluetooth.

## Risks

- **Wrong signature for the wrong account** — unchanged guard: every signing
  job first checks the device derives the expected address; a serialization bug
  yields a signature that fails recovery, checked on the host before broadcast.
- **macOS HID threading** — same single service thread;
  `scripts/repro_hidapi_macos.py` now drives the new transport (run it on a
  Mac with a device).
- **Nano S limits** — small APDU buffers and an old app; M0 decides gating
  before any feature work.
- **Ledger changes or closes the token endpoint** — the mirror keeps the last
  good copy; a qeth-hosted bundle is the fallback.
- **Packaging breadth** — M3 touches every format; it is its own milestone so a
  packaging problem can't hold back M1–M2.
