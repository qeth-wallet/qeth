# qeth — air-gapped QR signer (design note, step 3)

**Status:** **proposed — not started.** Step 3 of `docs/signers.md`: the first
worker-side backend, an **air-gapped QR wallet** (Keystone / **Keycard Shell** —
same protocol). Builds directly on steps 1–2 (the `SignerPlugin` registry and
the `SignerInteraction` host with its worker→main marshaling). This note is the
plan; the code is the source of truth once written.

## What we're building

1. **Signing** — instead of the Ledger-style spinner, a single window that
   *simultaneously* shows our QR (the unsigned request, animated) **and** runs
   the camera to read the device's response QR. The user holds the device up:
   its camera reads our screen, it signs, it shows the signature QR, our camera
   reads it back. This is `SignerInteraction.exchange_qr()` made real.
2. **Account import** — scan the device's account export (xpub + master
   fingerprint), supporting **multiple derivation methods**, and handle the ugly
   asymmetry: some methods need **one** exchange for many addresses, others need
   **one exchange per address** (see below).

## The standards (reason from the spec, don't guess)

- **BC-UR** — the transport: a payload is CBOR-encoded, chunked, and
  fountain-encoded into a sequence of `ur:…` parts rendered as an **animated**
  QR. The reader collects parts (order-independent, loss-tolerant) until it can
  reassemble. We *encode* our request and *decode* their response.
- **EIP-4527** registry (Keystone's `eth-*` UR types):
  - `eth-sign-request` (qeth → device): `{request-id (uuid), sign-data (bytes),
    data-type (1 legacy-tx | 2 typed-tx | 3 personal-message | 4 typed-data),
    chain-id, derivation-path (crypto-keypath incl. **source-fingerprint**),
    address, origin}`.
  - `eth-signature` (device → qeth): `{request-id, signature (r‖s‖v)}`.
  - `crypto-hdkey` / `crypto-account` (device → qeth, at import): an xpub with
    its **origin path** and **master fingerprint (xfp)**, optionally several
    bundled keys.
- **Master fingerprint (xfp)** is load-bearing: the sign-request's derivation
  path must carry it so the offline device knows *which* seed/key to use. We
  capture it at import and store it per account.

## Derivation methods — the asymmetry (the "ugh")

The address a path yields depends on where the *hardened* boundary sits:

- **One exchange → many addresses** (BIP44 / "Legacy", `m/44'/60'/0'/0/i`): the
  device exports the **account-level xpub** at `m/44'/60'/0'/0`; qeth derives
  address index `i` **locally** (non-hardened child of the xpub). One scan, then
  offline derivation — same shape as Ledger's auto-detect, minus the device.
- **One exchange per address** (Ledger Live, `m/44'/60'/i'/0/0`): the account
  index `i'` is **hardened**, so address `i` lives under a *different* xpub that
  can't be derived from another account's xpub (hardened derivation needs the
  private key). Each address needs its **own** export. Keystone's
  `crypto-account` can bundle several hdkeys in one QR (best case → still one
  scan); if the device only exports one path at a time, it's genuinely one
  scan per address.

So the import flow is **method-driven**: pick a method → either (a) scan one
xpub and derive N addresses locally, or (b) collect one hdkey per account
(from a bundled `crypto-account`, or by re-scanning). This generalises the
existing Ledger `PATH_SCHEMES` (`qeth/ledger.py:137`) into a shared idea; the
QR store record mirrors the Ledger one — `address`, `source:"qr"`, `path`,
`scheme` — plus **`xfp`** and (for local-derivation methods) the account
**`xpub`**.

## Where it plugs into steps 1–2

- `SignerInteraction.exchange_qr(payload) -> bytes | None` already exists
  (raising `NotImplementedError` today) and already marshals a worker call onto
  the main thread. Step 3 fills in the real dialog.
- `SignerPlugin.make_signer(store, account, ui)` already passes the host to the
  signer. The QR signer holds `ui` and calls `ui.exchange_qr()` from `sign()`
  on the worker — exactly what step 2's plumbing was built for.
- **No pre-worker spinner for QR:** `ui.py` shows `interaction.progress(text)`
  before the worker. A QR plugin has **no** generic spinner — it drives its own
  window — so it sets `progress_text = ""` and `ui.py` skips `progress()` when
  the text is empty. One-line guard, no new concept.

## Dependencies (all behind a `qr` extra, mirrored into `dev`)

Grounding checks done on this machine:

- **`PySide6.QtMultimedia` is NOT present** in the system PySide6 → the camera
  capture stack is a **decision** (below), not a given.
- **No QR *decoder*** installed (`pyzbar`/`cv2`/`zxingcpp` all absent). `segno`
  (encode) and `PIL` are present.

New deps to add (exact choices are open decisions): a **BC-UR + EIP-4527 codec**,
a **QR decoder**, **CBOR** (`cbor2`), a **BIP32** derivation lib, and a **camera**
capture path. Per `CLAUDE.md`, declare them in a `qr` extra (don't lean on
system-site-packages) and mirror into `dev` so their tests run. `eth-account`
(already a dep) covers unsigned-tx build + signature assembly.

## Plan of work (ordered; each step lands independently)

The ordering front-loads the **provable, headless** parts and quarantines the
camera/UI risk to the end.

### 3a — Headless UR / EIP-4527 codec  *(no UI, pure, fully unit-testable)*
Add the deps; write `qeth/qr/ur.py`:
- **encode**: `eth-sign-request` from tx/message params → CBOR → UR fragments.
- **decode**: `eth-signature` → `(r, s, v)`; `crypto-account`/`crypto-hdkey` →
  `(xpub, xfp, origin-path)`.
Test against **known-vector fixtures** (a captured UR ↔ its bytes). This is the
foundation everything else stands on. First: a short **spike** to pick the
BC-UR library vs. implementing the (well-specified) registry CBOR ourselves.

### 3b — `QRSigner` + `QRSignerPlugin`  *(worker-side; tested with a fake `exchange_qr`)*
- `qeth/qr_signer.py`: `QRSigner(account, ui)` implementing `Signer`.
  `sign(req, chain)` builds the unsigned tx (`eth-account` `TypedTransaction` /
  legacy) at the account's `path`/`xfp`, encodes the sign-request, calls
  `ui.exchange_qr(...)`, decodes the signature, **assembles the signed
  serialized tx**, returns bytes. `sign_message` / `sign_typed_data` use
  data-type 3 / 4.
- `qeth/signers/qr.py`: the plugin (`source_id="qr"`, `progress_text=""`,
  `make_signer → QRSigner`), registered in `REGISTRY`.
- Tests inject a fake host whose `exchange_qr` returns a canned `eth-signature`,
  so the **entire protocol round-trip is proven headlessly** — no camera. This
  is the key de-risking step: the signing math is correct before any UI.

### 3c — The exchange dialog  *(the camera + animated-QR window)*
- `qeth/qr_exchange_dialog.py`: **side-by-side** (a desktop screen is wider than
  it is tall) — left = the animated `segno` QR (a fresh fountain part per timer
  tick via `multipart.frame_source`); right = the live camera preview with the
  decoder running per frame; a complete decode → `accept()` returning the bytes;
  cancel → `None`. The two panes are equal **squares** (`PANE` px, both the QR
  and the 1:1 camera view) laid out in a `QGridLayout` — captions in row 0, panes
  in row 1 — so they stay aligned however a caption wraps. Spacing is the house
  rhythm: caption↔pane is `item_spacing` (within a paragraph), the between-column
  gap is `group_spacing` (two distinct groups). The QR is rendered large by
  `ur_to_pixmap` then scaled to the pane with **nearest-neighbour** (hard module
  edges, best for the device's scan, vs. grey-fringed smooth scaling).
- Implement `DialogInteraction.exchange_qr` to open it (already marshaled from
  the worker by step 2). Add the `progress_text==""` skip in `ui.py`.
- Camera + decoder are injectable so the **frame-cycling and decode-callback
  logic** are unit-tested with a fake source; the **real camera** is verified
  manually in the Mint/Fedora VMs (and the flatpak camera **portal**).

### 3d — Account import with derivation methods
- New add flow "Add air-gapped (QR) wallet…" mirroring `AddLedgerDialog`: a
  method picker, then either scan-one-xpub-derive-N (local BIP32 + nonce lookup
  to surface used accounts, like Ledger auto-detect) or collect-per-account.
- Persist `address / source:"qr" / path / scheme / xfp / xpub?`.
- Register the plugin in the Add menu (this is also where step-1's deferred
  "Add-menu-from-registry" naturally lands).

### 3e — Wiring, packaging, polish
Registry/menu wiring, icons, error UX (scan timeout, request-id mismatch, a
response for the wrong tx, wrong-fingerprint), the flatpak **camera portal** +
per-format runtime deps for the `qr` extra, and docs.

## Recommended first slice
Do **3a + 3b end-to-end for a single method (BIP44/Legacy)** before any camera
code: it proves the UR codec and the signing assembly against fixtures + a fake
exchange, with zero UI risk. Then 3c (camera dialog, VM-tested), then 3d
(import), then generalise to Ledger-Live per-address.

## Decisions (locked 2026-07-04)

1. **Camera stack → system QtMultimedia.** System installs get `QtMultimedia`
   as a distro dependency; the PyPI PySide6 used by the flatpak / AppImage /
   deb-verify variants already bundles it. Consistent with qeth's system-Qt /
   native-theming approach. **Follow-ups this creates:** the gstreamer backend's
   runtime bits must be bundled for AppImage and allowed for flatpak (+ the
   camera **portal**); track under 3c/3e.
2. **First slice → BIP44/Legacy, headless-first.** Ship the single-exchange
   methods (one scan → derive N addresses locally) end-to-end first; add
   Ledger-Live per-address as a follow-up. Do **3a + 3b** (codec + signer,
   fixtures + fake exchange) before any camera code.

Still open, lower-stakes (resolve in-flight):

3. **QR decoder** — `zxing-cpp` (self-contained pip wheel, no system lib —
   recommended) vs `pyzbar` (needs `libzbar0`).
4. **BC-UR/EIP-4527** — adopt a Python lib if a solid one exists, else implement
   the registry CBOR ourselves (well-specified, bounded). Decide via the 3a spike.
