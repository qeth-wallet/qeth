# qeth — pluggable signers (design note)

**Status:** **proposed — not started.** This is the plan for turning the
current hardcoded Ledger/hot-wallet dispatch into a signer *plugin* system so a
new backend (Keystone or Keycard Shell — air-gapped QR signers, …) is added by
dropping one module into `qeth/signers/` rather than editing branches across
`ui.py` and `wallets.py`. The code is the source of truth; this note is the
rationale and the target shape. Pairs with `ARCHITECTURE.md` §2 (the signing
core) and `CLAUDE.md`'s Ledger/HID thread-safety convention.

## Why now

qeth signs with two backends today (Ledger, hot wallet) plus a non-signing
`watch_only` source. The **cryptography** is already behind a clean interface;
the **dispatch, account-creation, and per-backend UI are not**. Adding a QR or
smartcard signer today means editing 4–5 sites and inventing a new interaction
primitive inline. This note fixes that so the next backend is additive.

## Current architecture (what exists)

The good seam — `qeth/signing.py`:

- **`Signer` (ABC, `signing.py:308`)** — `can_sign(address)`, `sign(req, chain)
  -> bytes` (RLP tx), `sign_message(req) -> bytes` (personal_sign),
  `sign_typed_data(req) -> bytes` (EIP-712). Requests are typed dataclasses
  (`SigningRequest`, `MessageSigningRequest`, `TypedDataSigningRequest`).
- **Implementations:** `LedgerSigner` (`ledger.py:321`), `HotWalletSigner`
  (`hot_wallet.py:131`). All Ledger/HID work is funnelled through the
  single-thread service `ledger_hid.py` (load-bearing on macOS — keep this).
- **Workers:** `SignAndBroadcastWorker` (`signing.py:472`) and
  `SignMessageWorker` (`signing.py:537`) run `signer.sign*()` on a QThread and
  emit the result; the UI stays responsive while a Ledger blocks on the device.
- **Cross-thread (dapp-initiated):** `SignerBridge` (`signing.py:340`) marshals
  an RPC-thread request onto the Qt main loop via a `Signal` + a
  `concurrent.futures.Future`, and the UI slot resolves it.

So the payload-producing layer is genuinely pluggable: a new `Signer` subclass
flows through the existing workers unchanged.

## What's not abstracted (the friction)

1. **No registry — dispatch is hardcoded `if source == …`**, duplicated:
   inline in the tx flow (`ui.py:985–1006`) and again in `_pick_signer_for`
   (`ui.py:1040–1078`, used by the message + typed-data flows at 1112/1178).
   Sources are bare string literals (`"ledger"`, `"hot"`, `"watch_only"`)
   spread across `wallets.py` (add flows) and `ui.py` (dispatch).
2. **UI is entangled with dispatch.** Each branch inlines its UX: hot prompts
   for a passphrase *up front* on the main thread (`ui.py:990`); Ledger just
   sets a `progress_text` for an indeterminate `QProgressDialog`. Account
   creation is three separate hardcoded flows — `_add_ledger` (`wallets.py:966`,
   `LedgerWorker` discovery + a picker), `_add_hot_wallet` (`:997`,
   `AddHotWalletDialog`), `_add_watch_only` (`:1181`, `AddWatchOnlyDialog`).
3. **The interaction model can't express QR.** Today's assumption is:
   *collect any input up front (password) → run a non-interactive worker to
   completion → broadcast.* Ledger fits (interaction is device-side; the UI is a
   passive spinner). Hot fits (one password). **QR/air-gapped does not**: its
   signing *is* a two-way UI step — animate the unsigned tx as a QR, then open
   the camera to scan the signed reply. `signer.sign() -> bytes` on a worker
   thread has no channel to drive that.

## Target: `qeth/signers/` + a registry + an interaction host

```
qeth/signers/
  __init__.py      # REGISTRY: {source_id: SignerPlugin}; register() + lookups
  base.py          # SignerPlugin ABC + SignerInteraction Protocol
  ledger.py        # wraps existing LedgerSigner + its add-flow + spinner UX
  hot.py           # wraps HotWalletSigner + password UX
  watch_only.py    # non-signing source (capability = cannot sign)
  keystone_qr.py   # first NEW backend — UR/animated-QR show+scan
```

### 1. `SignerPlugin` — everything one source needs

```python
class SignerPlugin(ABC):
    source_id: str                 # persisted in the account's "source" field
    display_name: str              # "Ledger", "Keystone", …
    def icon(self) -> QIcon: ...
    def can_sign(self) -> bool:    # watch_only → False
        return True

    # Account creation: run the add flow, return the accounts to persist
    # (address + source + any per-backend fields, e.g. derivation path / xpub).
    def add_accounts(self, host: AddContext) -> list[dict]: ...

    # Produce a Signer bound to this account, driving `ui` for any interaction
    # (password, spinner, QR exchange). Returns None if the user cancelled.
    def signer(self, account: dict, ui: SignerInteraction) -> Signer | None: ...
```

`ui.py`/`wallets.py` iterate `REGISTRY` instead of branching: the Add menu is
built from `display_name`/`icon()`, dispatch is
`REGISTRY[account["source"]].signer(account, ui)`, and `can_sign()` replaces the
special-casing that today lets `watch_only` fall through to a "no signer" warn.

### 2. `SignerInteraction` — one host, every UX shape

The crux. Pass an interaction host into signing; the signer drives only the
parts it needs. This unifies all four shapes and is trivially fakeable in tests.

```python
class SignerInteraction(Protocol):
    def progress(self, text: str) -> None: ...            # spinner label
    def request_secret(self, prompt: str) -> str | None:  # password/PIN
        ...
    def exchange_qr(self, payload: bytes) -> bytes | None:  # show animated QR,
        ...                                                 # scan the reply
```

| Backend | Uses | Shape |
|---|---|---|
| Hot wallet | `request_secret` (once) | pre-input, then non-interactive |
| Ledger | `progress` | device-blocking, passive UI |
| Keystone / Keycard Shell (QR) | `progress` + `exchange_qr` | interactive, bidirectional |

**Threading.** The worker still runs `signer.sign*()` off the main thread. A
signer that needs UI calls the interaction host, whose implementation marshals
to the Qt main loop and blocks the worker on a `Future` until the user answers —
the *same* pattern as `SignerBridge`, just worker→main-thread instead of
RPC-thread→main-thread. The two can share one small "call the UI, await a
future" helper. The main-thread `SignerInteraction` impl owns the actual
widgets: reuse the existing `QProgressDialog` for `progress`, `prompt_text(…,
password=True)` for `request_secret`, and a new animated-QR + camera-scan dialog
for `exchange_qr`.

## Standards / backend notes

- **Keystone & Keycard Shell (air-gapped QR)** — both do the *same* QR exchange,
  so they share one code path. The interop standard is **BC-UR** (Uniform
  Resources, animated QR) with **EIP-4527** `crypto-request` / `crypto-response`
  (and `crypto-hdkey` / `crypto-account` for import). `exchange_qr(payload)`
  maps directly: encode the unsigned tx as a UR, animate it, scan the response
  UR. Needs a UR codec + a camera/QR-scan widget (new deps — declare them behind
  an extra, per `CLAUDE.md`'s system-site-packages caveat). Any UR quirks
  specific to one device stay isolated in that device's plugin module; the
  `exchange_qr` primitive is shared. (The bare Keycard *smartcard* — NFC/APDU,
  no Shell — would be a separate transport, out of scope here.)
- **Watch-only** stays a registered plugin with `can_sign() == False` — it owns
  its add flow but produces no signer; the registry makes “this source can’t
  sign” a first-class property instead of an `else` branch.
- **Ledger/HID rule is unchanged** — the Ledger plugin’s `Signer` must still
  route every call through `ledger_hid.run_ledger_hid_job` (see `CLAUDE.md`).
  The refactor moves *dispatch*, not the HID discipline.

## Migration plan (incremental, each step test-backed)

1. **Extract the registry (pure refactor, no behaviour change).** Introduce
   `SignerPlugin` + `REGISTRY`, wrap the existing Ledger/hot/watch flows, and
   replace the `if source ==` branches in `ui.py` (both the tx path and
   `_pick_signer_for`) and the Add-menu construction in `wallets.py` with
   registry lookups. Provable green by the existing signing/UI tests; the only
   new tests assert “registry has these three sources” and “dispatch routes by
   `source`”.
2. **Introduce `SignerInteraction`** and route Ledger’s spinner + hot’s password
   through it (still no new backend). Now `signer()` takes the host; the
   main-thread impl wraps the current `QProgressDialog` / `prompt_text`. Add the
   worker→main-thread future helper (shared with `SignerBridge`). Tests use a
   fake host to assert the calls without Qt.
3. **Add the first QR signer (`keystone_qr.py`)** — the proof the abstraction
   holds. New: a UR codec dep (behind an extra) and the animated-QR + scan
   dialog implementing `exchange_qr`. Its add flow scans a `crypto-account` /
   `crypto-hdkey` UR to import addresses.

Steps 1–2 are behaviour-preserving, so the QR work in step 3 is purely
additive. Once `exchange_qr` exists, a second air-gapped device (Keystone and
Keycard Shell share it) is mostly a new plugin module + any device-specific UR
handling — not new plumbing.

## Open questions

- **`exchange_qr` granularity** — is one show-then-scan call enough, or do some
  backends need multi-round exchanges (request → response → ack)? Start with the
  single round EIP-4527 needs; widen only if a backend forces it.
- **Per-account backend fields** — derivation path (Ledger), keystore ref
  (hot), xpub/fingerprint (QR) already live loosely in the account dict. The
  registry is the natural place to document/validate each source’s expected
  fields; consider a light per-plugin schema check on load.
- **Dapp path parity** — `SignerBridge.submit_async` must resolve through the
  same registry + interaction host so a QR signer works for dapp-initiated
  `personal_sign` / `eth_signTypedData_v4`, not just the Send dialog.
```
