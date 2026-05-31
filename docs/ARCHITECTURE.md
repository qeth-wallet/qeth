# qeth — Architecture & Behavioral Reference

This document describes **how each part of qeth is meant to behave** — the
logic, invariants, and data flows — as a reference to read alongside the code.
It complements `CLAUDE.md` (which covers dev conventions and the *why* behind
recurring traps); this file covers the *what* and *how* of each subsystem.

File:line citations point at the canonical implementation. When code and this
doc disagree, the code is the source of truth — fix whichever is wrong.

---

## 1. The shape of the app

qeth is a desktop Qt (PySide6) Ethereum wallet with three pillars:

1. **A wallet UI** — a main window hosting pluggable panels (Wallets, Tokens,
   Transactions) over a shared chain selector.
2. **A signing core** — Ledger and hot-wallet signers behind a common `Signer`
   interface, driven by user-confirmation dialogs.
3. **A Frame-compatible JSON-RPC server** — `127.0.0.1:1248` (HTTP + WebSocket)
   that lets browser dapps talk to the wallet unchanged.

Everything on-chain flows through one **sync client** (`EthClient`, web3.py
under the hood) and a set of **pluggable HTTP data sources** (token discovery,
prices, risk, tx history, ABIs) that each tolerate their own failures.

Three threads matter:

- **Qt main thread** — all UI, all signal/slot delivery, all dialog handling.
- **Worker QThreads** — every blocking network/RPC call (discovery, balances,
  receipts, signing). Tracked in a `set` and self-evicting (§7).
- **`qeth-rpc` asyncio thread** — the aiohttp JSON-RPC server (§6).

The RPC thread and the UI thread never touch each other's state directly; they
hand off through the `SignerBridge` (a `QObject` + `concurrent.futures.Future`,
§6.6).

---

## 2. Persistence & caching layer

All state lives under `~/.qeth/`. Each cache is a separate concern with its own
file layout and freshness policy. The unifying rules:

- **Addresses are lowercased before any disk write**; original casing is kept
  only where a downstream RPC needs it (e.g. metadata lookups).
- **Timestamps are unix seconds.**
- **Stale beats missing** — a cache that's behind the chain renders instantly
  and is corrected by a background refresh, rather than showing an empty panel.
- **Robust parse** — a malformed row is skipped, never fatal; schema drift
  across versions degrades gracefully.

### 2.1 `store.py` — the config singleton (`~/.qeth/config.json`)

Single source of truth for user settings and app state. Thread-safe (`RLock`),
`save()` after every mutation.

Holds: `accounts`, `chains`, `current_chain_id`, `default_account`,
`hidden_tokens`/`shown_tokens` (sets of `(chain_id, address)`), window geometry +
splitter/header states (hex-encoded Qt serializations), and `etherscan_api_key`.

Key behaviors:

- **`_merge_chain` (store.py:13–43) — shipped defaults own canonical metadata.**
  For any chain in `DEFAULT_CHAINS`, the shipped record wins on every field
  *except* `rpc_url`; the persisted copy contributes only the user's RPC URL.
  This means stale/wrong metadata in an old config (e.g. a wrong
  `coingecko_id`) self-heals on load. Custom (non-default) chains use their
  persisted record verbatim.
- **Forward-fill (store.py:94–104)** — new chains added to `DEFAULT_CHAINS` in a
  release are appended to the user's chain list on first load after upgrade.
  There is no remove-chain UI, so a chain present in defaults but absent from a
  config means it predates that chain.
- **Token visibility:** `hidden` always beats `shown` on conflict. Toggles move
  a token between the two sets atomically.
- **Default account** is auto-promoted on first add and reassigned on removal of
  the current default.

### 2.2 `chains.py` — `Chain` dataclass + `DEFAULT_CHAINS`

Immutable `Chain(name, chain_id, rpc_url, symbol, explorer, coingecko_id,
eip1559)`. Seven shipped networks: Ethereum, Optimism, Polygon, Arbitrum, Base,
Gnosis, BNB Smart Chain — all on DRPC by default, all `eip1559=True`. Mutations
happen only via `Store`. Note Polygon's `coingecko_id` is
`polygon-ecosystem-token` (MATIC→POL rebrand), not `ethereum`.

### 2.3 `chainlist.py` — public RPC registry (for the RPC picker)

Fetches `chainid.network/chains.json`, caches to `~/.qeth/chainlist/chains.json`
with a **7-day TTL** (registry changes slowly). On network failure falls back to
stale cache; `[]` only if there's no cache at all. `probe_rpc()` sends
`eth_chainId` to a candidate URL and verifies the returned id matches —
returning `(ok, latency_ms, reason)`. Template URLs with `${VAR}` placeholders
are filtered out. **Probing is live, never a hardcoded allow/deny list.**

### 2.4 `wallet_cache.py` — per-wallet token/balance snapshot

`~/.qeth/wallets/<chain_id>/<address_lower>.json` — one file per
`(chain, address)` so concurrent wallets never contend. Stores `CachedToken`
rows (contract, symbol, name, decimals, logo_uri, `balance_raw`, `price_usd` as a
**stringified Decimal**, plus per-field update timestamps) and native balance +
price. This is what `show_cached` paints instantly on wallet selection. No TTL.

### 2.5 `transactions_cache.py` — per-wallet tx history

`~/.qeth/transactions/<chain_id>/<address_lower>.json` — a list of `Transaction`
dicts. `merge_txs()` dedups by hash (fresh fetch wins) and sorts by nonce
descending. `pending=True` exists only on locally-broadcast txs awaiting a
receipt — indexers never return mempool entries, so this flag is qeth's own.

### 2.6 `token_metadata.py` — immutable ERC-20 metadata

`~/.qeth/token_metadata/<chain_id>.json`, map of `address_lower → {symbol, name,
decimals}`. **No TTL ever** — metadata is immutable post-deployment. Lazy per-
chain load into memory, batched `put_many` writes. Prefilled at startup from the
curated token lists (§9.1) so first discovery of a chain skips a ~50-call
metadata multicall.

### 2.7 `abi_cache.py` — verified contract ABIs

`~/.qeth/abi/<chain_id>/<address_lower>.json`. Three-state load: an ABI list, a
`{"unverified": true}` **negative sentinel** (so unverified contracts aren't
re-queried forever), or `None` (not cached). Detects proxy-stub ABIs (only
`implementation`/`admin`/`upgradeTo` and no real methods) and rejects them so a
later-verified implementation gets picked up.

---

## 3. The chain client — `chain.py`

`EthClient` is a thin, synchronous layer over web3.py shaped like `w3.eth.*`. It
is **the only place that talks JSON-RPC for on-chain reads/writes** — new code
extends it rather than writing ad-hoc urllib.

- Methods: `get_balance`, `get_block_number`, `chain_id`,
  `get_transaction_count`, `gas_price`, `max_priority_fee`, `estimate_gas`,
  `call`, `send_raw_transaction`, plus a low-level `rpc(method, params)` escape
  hatch that raises `ChainError` on a JSON-RPC error.
- **`ExtraDataToPOAMiddleware` is injected unconditionally** (chain.py:140–142)
  so PoA chains (BSC, Polygon) don't throw on oversized `extraData`. Even a raw
  `rpc()` call routes through middleware in web3.py 7, so this must always be on.
- **Multicall3** (`0xcA11…CA11`) via a `Multicall` context manager that batches
  `aggregate3` calls with `allowFailure=True` — one reverting inner call never
  sinks the batch; failed calls come back with `success=False`. Metadata decode
  falls back from `string` to `bytes32` for legacy tokens like MKR.
- DRPC sits behind Cloudflare, which 403s the default urllib UA — every request
  sets `User-Agent: qeth/<version>` (centralized as `USER_AGENT` in
  `__init__.py`).
- Heavy imports (web3, eth_abi) are lazy at first instantiation to keep UI
  startup fast. No internal thread-safety — callers use worker threads.

> **Money math:** wei→ether and ERC-20 amounts always go through `Decimal`
> (`wei_to_ether`, `TokenBalance.balance`). Never `wei / 1e18`. See CLAUDE.md.

---

## 4. Pluggable data sources

Each external concern is an ABC with interchangeable implementations and a
`Routed*` wrapper that prefers a primary and falls back to a secondary **without
trapping exceptions** (it checks `supports()` first). Per-source failures are
always tolerated — one bad source never takes the feature down.

### 4.1 Token discovery — `tokens.py`

- `TokenSource.list_balances(chain, address) -> [TokenBalance]` + `supports()`.
- **`BlockscoutSource`** — per-chain Blockscout instances; Etherscan-compatible
  `tokenlist`; filters to ERC-20 (drops 721/1155). Lags chain head by minutes.
- **`EtherscanV2Source`** — unified multichain API
  (`api.etherscan.io/v2/api?chainid=<id>`); one key covers all chains in
  `ETHERSCAN_V2_CHAINS`. Key is fetched **dynamically per call** so a runtime
  paste works without re-instantiation. `supports()` is True only when the key
  is set *and* the chain is enumerated. **`offset=10000`** is the per-page cap —
  smaller values silently truncate long-tail holdings (this is what once hid a
  ~$9k position past index 100); a full page logs a truncation warning. A
  rate-limit reply (Etherscan puts that text in `result`, not `message`) raises
  **`RateLimited`**.
- **`RoutedTokenSource(EtherscanV2, Blockscout)`** is the default wiring:
  Etherscan when a key is present, else Blockscout — with **burst protection**
  for the rate-limited primary. Fast keyboard navigation fires a discovery per
  wallet selection (no debounce); without throttling a burst trips Etherscan's
  ~5 req/s free tier. So within a **2 s cooldown** after an Etherscan call the
  router diverts to Blockscout *when it can serve the chain* (chains it can't,
  e.g. BNB/Gnosis, keep using Etherscan), and a residual `RateLimited` from the
  primary falls back to Blockscout for that call. Safe because the source only
  supplies the **contract list** (balances + metadata come from multicall,
  §9.1), so Blockscout's list is a fine stand-in during a burst; the settled
  wallet's 60 s refresh (§9.2) lands outside the window and uses Etherscan
  again.

### 4.2 Curated token lists — `tokenlists.py`

Merges whitelists from **Uniswap, CoinGecko (per-chain), Curve, 1inch** into one
index keyed by `(chain_id, address)` — first source wins. Disk-cached at
`~/.qeth/tokenlists/` with stale-while-revalidate (fresh > live fetch > stale >
none, 24h TTL). Answers two questions:

- **"Is this token known?"** — used to skip risk checks and gate dust filtering.
- **"Is it likely a scam?"** (`is_likely_scam`) — short-circuits False if
  whitelisted; else flags GoPlus high-risk, URL-in-symbol, claim/airdrop
  keywords, or canonical-symbol impersonation (looks like USDC but isn't
  whitelisted).

Curve has no `logoURI`; icons are derived from the `curve-assets` repo by
address (404s silently off-mainnet).

### 4.3 Prices — `prices.py`

`DefiLlamaPrices` only (free, no key, multichain). Batches ≤100 keys per request
against `coins.llama.fi/prices/current/`. Native asset uses key `""` (matches
the panel's `NATIVE_CONTRACT`); ERC-20 keys are lowercase addresses. Chains not
in `DEFILLAMA_CHAIN_SLUGS` are silently skipped. One failed batch is skipped;
others proceed.

### 4.4 Risk — `risk.py`

`GoPlusRisk` → `api.gopluslabs.io` (~30 req/min, no key). `RiskReport` carries
honeypot / blacklist / hidden-owner / can't-buy-sell flags + buy/sell tax;
`is_high_risk()` trips on any danger flag or sell tax > 50%. Cached at
`~/.qeth/risk/<chain_id>.json` with a **1-week TTL**. Only **non-whitelisted**
contracts are ever checked.

### 4.5 ABIs & decoding — `abi.py`

`BlockscoutAbiSource` resolves verified ABIs via Blockscout v2 (falls back to
v1), **recursively merging proxy implementation ABIs** (max depth 4, proxy's own
entries win on selector collision). `decode_call` produces a tree (function →
args → nested tuples/arrays) that the Transactions plugin renders. Transient
HTTP errors raise rather than negative-caching a blip.

`decode_event(log, abi=None)` is the log-side counterpart used by the events
view (§10.5). The Transfer / Approval / ApprovalForAll family decodes from
their canonical signatures **without any ABI** (so the common case needs no
fetch); ERC-20 vs ERC-721 Transfer is told apart by topic count (3 vs 4). Any
other event is named only when its contract's ABI is supplied — fetched lazily
(§10.5) — otherwise it renders as a raw "unknown event".

### 4.6 Transaction history — `transactions.py`

`TransactionSource.list_transactions(chain, address, page, limit)`. Blockscout
and Etherscan-v2 implementations share one parser; `RoutedTransactionSource`
mirrors the token routing. `Transaction.direction(viewer)` →
SENT/RECEIVED/SELF/UNRELATED. Success is read from `txreceipt_status`, falling
back to `isError`. *(This is the history-fetch layer; the UI plugin that polls
pending receipts is §10.2.)*

### 4.7 ENS — `ens.py`

Reverse-resolves an address to a primary name **on mainnet only** (ENS lives on
chain 1 regardless of the browsed chain), with a forward-resolution round-trip
check to defeat spoofing. Any failure → `None` (informational only). Runs in
`EnsReverseWorker` (QThread); empty-string name means "no verified name".

### 4.8 Formatting — `formatting.py`

Pure, Qt-free display helpers: `format_balance` (6 sig figs, `e-9` →
`× 10⁻⁹`), `format_usd` (sub-cent shown as \<$0.01, empty for zero), `short_addr`
(`0x1234…abcd`, contract-creation sentinel), `format_datetime` (locale-aware).

---

## 5. The UI shell

### 5.1 Main window & slots — `ui.py`, `plugin.py`

- `MainWindow` is a `QMainWindow` with a horizontal `QSplitter`: **left slot** =
  Wallets (single plugin, no tab bar); **right slot** = Tokens + Transactions
  (tab bar appears only with ≥2 plugins). The chain combo and the ⋯ RPC-editor
  button are **shared widgets** mounted on the right slot's bottom row, so they
  persist across tab switches.
- **`Slot`** (plugin.py:120+) stacks plugin widgets, hides its tab bar when only
  one plugin is mounted, and fans `broadcast_account_changed` /
  `broadcast_chain_changed` to all mounted plugins.
- **Host interface** (`plugin.py`, a `Protocol` for test-stubbing) exposes to
  plugins: `selected_address`, `current_chain()`, `chain_by_id()`,
  `start_worker()`, `status_message()`, `token_info()`, `icon_cache()`,
  `native_price_usd()`, and references to sibling plugins (`tokens_plugin`,
  `transactions_plugin`).
- **Plugin lifecycle hooks** (all optional): `attach(host)`,
  `on_account_changed(addr)`, `on_chain_changed()`, `on_activated()`.
- **Navigation:** Tab / Shift+Tab cycle wallet-tree ↔ active right table;
  Left/Right switch Tokens↔Transactions. A focus-aware delegate paints the
  selected row solid when focused, outline-only when not (Norton-Commander
  style), with a `_FocusRepainter` forcing the swap to be immediate. The
  delegate **never paints the per-cell focus rectangle** (it strips
  `State_HasFocus` on every branch): the view's *current* index — set by a row
  insert, a rebuild, or the broadcast auto-switch moving focus to the
  Transactions tab — would otherwise draw a stray dotted box on an unselected
  cell (visibly, beside a freshly-prepended pending row's status icon).
  Selection, not the current cell, is what the UI surfaces.

### 5.2 Chain combo & live chain addition — `ui.py`

Populated from `Store.chains` as `"{name} ({chain_id})"` with an icon from
`ChainIconCache`. When a dapp calls `wallet_addEthereumChain`, the
`SignerBridge.chain_added(chain_id)` signal (queued to the main thread) runs
`_on_chain_added`, which appends the chain to the combo live (guarded against
double-add) — no restart needed.

### 5.3 Chain icons — `icons.py`

`ChainIconCache` is a three-tier resolver: **memory → bundled asset → disk
cache → background fetch**. Missing icons are **discovered**, not bundled-only:
`_ChainIconFetchWorker` walks an ordered URL list (Curve `curve-assets`, then
TrustWallet) and emits `icon_ready(chain_id)`, which patches the combo item.
Same disk+memory pattern backs token icons.

Every icon (token and chain) is passed through `to_circular` (icons.py:73) —
centre-cropped to a square (`KeepAspectRatioByExpanding`) then masked to an
anti-aliased circle — so sources that ship square logos match CoinGecko's
circular style and the list reads uniformly. The memory cache stores the
already-cropped pixmap.

### 5.4 Window-icon theming — `branding.py`, `__main__.py`

At startup, window-background luminance (ITU-R BT.601, threshold 0.5) picks the
mono vs reversed SVG glyph so the icon contrasts the user's theme. Detected
**once** at launch (a mid-session theme flip needs a restart) — deliberately
simple per the "don't escalate theme workarounds" rule.

### 5.5 System tray — `tray.py`

If `QSystemTrayIcon.isSystemTrayAvailable()`, minimizing **hides to tray** (an
event filter on `WindowStateChange` defers a hide via `singleShot(0)`, dropping
the window from the taskbar). Left-click toggles visibility; right-click gives
Show/Hide/Exit. The **close [X] still quits** — only minimize is redirected.

### 5.6 Chain-RPC editor dialog — `chain_rpc_dialog.py`

Opened from the ⋯ button. Shows the current RPC URL (editable), a **live-probed**
picker of chainlist.org HTTP endpoints (≤16 concurrent `eth_chainId` probes,
ranked by latency, failures unselectable), and the **global Etherscan key**
field (shown in plaintext — it isn't a secret). The chainlist loader is parented
to the `QApplication`, not the dialog, and its signals are disconnected in
`done()` so cancelling mid-probe can't abort a QThread and crash the app; the
loader finishes in the background and self-deletes.

### 5.7 Startup ordering — nothing blocks the window

The window must paint immediately; **no network I/O is on the critical path to
first render.** Two rules enforce this:

- **Token lists load asynchronously** (a background `TokenListsLoader`). The UI
  does not wait for them — panels render from the disk cache (§2.4/§2.5)
  on first paint and upgrade in place once lists land and the discovery pipeline
  runs. (Earlier, blocking on a slow token-list fetch delayed the window's
  contents by hundreds of ms after the frame already appeared.)
- **The initial account selection is replayed after plugins mount**, so the
  first wallet's cached tokens/transactions show without an "empty until the
  first loader finishes" gap.
- Heavy libraries (web3, eth_abi) are **lazy-imported** at first `EthClient`
  use, not at module import (§3), keeping import-time cost off startup.
- On Linux, `QT_X11_NO_MITSHM=1` is set (`setdefault`) **before** `QApplication`
  (`_harden_x11_backing_store`). Qt's shared-memory X11 backing store otherwise
  stops repainting the window after many hours of uptime — only a hide/show
  (minimise to tray and back) recreates the surface. xcb-only, so it's a no-op
  on Wayland/macOS/Windows; an explicit user override is respected.

### 5.8 Dialog conventions (GNOME HIG)

The dialogs follow the GNOME 2 HIG, applied through the system Qt style (never
overriding it — the theme stays in charge, §5.4):

- **Two-tier alerts & confirmations — `alerts.py`.** `warn`/`error`/`info`
  build a `QMessageBox` with the plain-language problem as the bold primary
  line (`setText`) and the technical detail as secondary text
  (`setInformativeText`), rather than cramming both onto one line or leaning on
  the title bar. `confirm(...)` is the same shape with a **verb button**
  (e.g. "Remove", not Yes/No), **Cancel as the default + Escape target** (so an
  accidental Enter/Esc is non-destructive), and a Warning icon for destructive
  actions. The only confirmation in the app guards account removal (an
  irreversible keystore delete for hot wallets); nothing reversible prompts.
- **Access-key mnemonics.** Buttons, menu items, and editable-field labels
  carry `&`-mnemonics (Alt+letter), unique within each window. Underlines reveal
  on Alt per the style; buttons built via `setDefaultAction` drop the `&` from
  `text()` but still trigger.
- **Header capitalization** on buttons, menu items, and window titles
  ("Confirm and Sign", "Open in Block Explorer"); articles/short
  prepositions/conjunctions stay lowercase.
- **Default button + Esc.** Esc dismisses every `QDialog` (built-in `reject()`);
  `QDialogButtonBox` promotes the affirmative to the default on show so Enter
  confirms. Explicit defaults are set only where the auto-default is wrong (the
  signature result dialog pins Close so Enter dismisses rather than copies).
- **Progressive disclosure — `_CollapsibleSection`** (transactions.py). A flat
  `QToolButton` + rotating arrow that shows/hides a content area; used to tuck
  the gas controls behind a collapsed "Gas settings" header (§10.3).
- **Working copy menus.** Address/hash hyperlinks (rich-text `QLabel`s) get a
  custom context menu via `_install_copy_menu` — "Copy Address/Hash/Link" (the
  noun auto-detected from the value) + "Open in Browser" — replacing Qt's
  default menu where "Copy" is for selected text (always disabled). Every
  context menu reuses its toolbar buttons' icons so menu and toolbar match.

---

## 6. The Frame-compatible JSON-RPC server — `rpc.py`

Runs an aiohttp app on `127.0.0.1:1248` (HTTP POST **and** WebSocket on the same
port) on the dedicated `qeth-rpc` asyncio thread. CORS is fully open so the
Frame browser extension connects from any origin.

### 6.1 Origin tracking

Frame's extension sends the real dapp URL in a JSON body field
`__frameOrigin` (the HTTP `Origin` header is the extension's own). Precedence:
`__frameOrigin` → `Origin` header → `None` (direct callers like curl/tests). For
WebSocket clients the origin is captured at handshake and cached per socket.

### 6.2 Per-origin chain tracking — the key isolation invariant

`_rpc_chain_id_by_origin: dict[str, int]` pins each dapp's chain independently
(`None`/`""` share one "origin-less" bucket).

- `_chain_for_origin(origin)` returns the origin's pinned chain, else the
  wallet UI's current chain.
- **`wallet_switchEthereumChain` is local to the calling origin** — it updates
  only that origin's entry and emits `chainChanged` scoped `only_origin=origin`.
  This is why 1inch switching to zkSync no longer drags every other tab along.
- **UI-driven chain flips** (`set_rpc_chain` from the toolbar combo) broadcast
  `chainChanged` with `only_unscoped=True` — reaching only dapps that haven't
  pinned themselves. Dapps that explicitly switched keep their choice.
- The user's **persisted default** chain (set in the toolbar) is unaffected by
  any dapp switch and survives restarts.

### 6.3 Locally-handled vs proxied methods

Handled in-process: `eth_accounts` / `eth_requestAccounts` (→
`[default_account]`), `eth_chainId`, `net_version`,
`wallet_switchEthereumChain`, `wallet_addEthereumChain`, `eth_subscribe` /
`eth_unsubscribe` (wallet events), and the signing methods. **Everything else is
proxied** to the current chain's upstream RPC URL for that origin.

### 6.4 Signing methods & refusals

`eth_sendTransaction`, `personal_sign`, `eth_signTypedData_v3/v4` are parsed into
typed requests and handed to the UI via the bridge (§6.6). **`eth_sign` and
`eth_signTransaction` are refused** — the former is the blind-hash footgun
modern wallets reject; the latter is superseded by sign-and-broadcast. When no
`SignerBridge` is wired (headless tests), signing methods return `-32601` rather
than crash.

### 6.5 Fail-fast host cooldown

When an upstream is unreachable (e.g. DNS down), dapps poll several methods per
second and each would otherwise hang ~15s, freezing the asyncio thread and the
app. `_proxy` records `_host_last_fail[host]` on a transient error
(`ClientConnectorError` incl. DNS, `ClientOSError`, `ServerDisconnectedError`,
`asyncio.TimeoutError`) and, for `_FAIL_FAST_S = 5s`, short-circuits further
calls to that host with a clean `-32603` "upstream temporarily unreachable". The
first call after the window re-probes; success clears the entry. **Classification
is `isinstance`-based**, not name-equality, so subclasses like
`ClientConnectorDNSError` are caught.

### 6.6 `SignerBridge` — the cross-thread bus — `signing.py`

A `QObject` bridging the asyncio RPC thread and the Qt main thread:

```
aiohttp coroutine          [qeth-rpc thread]
  → submit_async(req) → request_received.emit(req, future)   (queued to main thread)
  → MainWindow opens a confirmation dialog                   [main thread]
  → user confirms → worker thread signs (+broadcasts for txs)
  → bridge.resolve(future, result)  /  bridge.reject(future, err)
  → await asyncio.wrap_future(future) resumes                [qeth-rpc thread]
  → JSON-RPC returns {"result": "0x…"}
```

Signals: `request_received(req, future)` and `chain_added(chain_id)`. Request
types: `SigningRequest` (tx → hash), `MessageSigningRequest` (`personal_sign` →
65-byte sig), `TypedDataSigningRequest` (EIP-712 → sig). Param parsing
normalizes addresses to EIP-55 checksum (web3.py refuses non-checksummed),
accepts `personal_sign` args in either order, and accepts typed data as JSON
string or object.

### 6.7 EIP-1193 events over WebSocket

After `eth_subscribe`, the server pushes `eth_subscription` notifications for
`accountsChanged` (global — fires on default-account change), `chainChanged`
(hex id) and `networkChanged` (decimal id) — the latter two scoped per §6.2.
Dead sockets are reaped from the subscription/origin maps on send failure.

### 6.8 `wallet_addEthereumChain`

If the chain id is already known, it's a no-op (the dapp's relay URL is often a
restricted WC endpoint that 403s us — we keep our working DRPC URL). Otherwise a
new `Chain` is built from `chainName`/`rpcUrls[0]`/`nativeCurrency.symbol`/
`blockExplorerUrls[0]`, appended to `store.chains`, and `chain_added` fires so
the combo updates live (§5.2).

---

## 7. Worker-thread management

Plugins never block the UI thread; every network/RPC call runs in a `QThread`.
The host's `start_worker` (ui.py:429+) adds the thread to a **`set[QThread]`**
and connects `finished` to both `discard(worker)` and `deleteLater()`. This is
load-bearing: parking a worker in a single `self._worker =` slot would let the
next refresh overwrite (and GC) a still-running thread, whose QThread destructor
`abort()`s the whole process. Workers self-evict on completion, so arbitrary
numbers run concurrently with no central manager. See CLAUDE.md "Long-lived
QThreads".

> **Signals carrying chain values use `Signal(object, …)`, never
> `Signal(int, …)`** (qint32 overflow), and the same applies to `dict`/`list`
> *contents* — PySide6 marshals container members too. See CLAUDE.md.

---

## 8. Accounts & signing

### 8.1 Wallets plugin — `plugins/wallets.py`

Manages the account tree (group containers → optional scheme subgroups → address
leaves). Three mutually-exclusive sources per account: **Ledger**, **hot
wallet**, **watch-only/imported**. Selection drives the whole app —
`selected_address_changed` → host → `on_account_changed` on Tokens/Transactions.
The **default account** (browser-facing, returned by `eth_accounts`) is set via
double-click or "Connect to browser" and is disabled for watch-only accounts.
Labels are editable inline and persisted; a post-import ENS reverse lookup fills
blank labels without ever clobbering a user edit.

**Keyboard:** `Ctrl+C` copies / `Del` removes the selected address — shortcuts
carried on the copy/remove actions but scoped to the tree
(`WidgetWithChildrenShortcut`), so they act only when the accounts panel has
focus and don't shadow copy/delete in the token/transaction tables. The
add-account picker is a menu with per-source icons (hardware device for Ledger,
key for hot wallet, eye for watch-only, import glyph for Brownie/Frame).

### 8.2 Signers

- **`ledger.py`** — `LedgerWorker` enumerates accounts across derivation
  templates (Ledger Live / Legacy / BIP44), auto-detecting until 3 consecutive
  empty-nonce accounts (cap 100). One fresh dongle handle per run.
  - **Availability pre-check** (`is_ledger_available()`, ledger.py:14) — before
    a scan or a sign, the device is probed (connected + unlocked + Ethereum app
    open). If it isn't ready the UI shows a "connect your Ledger" prompt
    **instead of letting the operation fail** — so a dapp signing request isn't
    lost to a sleeping device and doesn't need re-submitting from the browser.
    The probe deliberately clears ledgereth's module-level dongle cache so the
    next real call starts fresh (the "fresh handle per call" invariant — reusing
    a cached handle is what made every *second* sign fail until a restart).
  - **Error decoding** (`_explain_ledger_error`, ledger.py:47) — maps ledgereth
    exceptions to one action-oriented sentence; the screensaver/sleep family
    (`0x6804`, `0x6d00`, …) and the otherwise-undocumented `0x55xx` range are
    named explicitly rather than surfacing a raw "0x5515 UNKNOWN". Unknown
    shapes still print the raw SW code so future cases stay diagnosable.
  - A signing failure inside the **send dialog** raises an OK popup *over* the
    dialog without closing it, so the user can retry on the same tx.
- **`hot_wallet.py`** — Web3 Secret Storage v3 keystores at
  `~/.qeth/keystores/<addr>.json` (scrypt + AES-256-CBC). Keys generated from
  `os.urandom(32)`; decryption runs on a worker thread (scrypt blocks) and maps
  failures to "Wrong passphrase".
- **`import_sources.py`** — imports from **Brownie** (`~/.brownie/accounts/*`,
  same v3 format, copied as-is) and **Frame** (`~/.config/frame/signers/*`,
  decrypted with the Frame passphrase and re-encrypted under a qeth passphrase).

### 8.3 Message signing UX — `plugins/sign_message.py`

Two entry points: a **review dialog** for dapp-initiated `personal_sign` /
typed-data (renders UTF-8 or hex, pretty-prints EIP-712 domain+message), and a
**compose dialog** for locally-initiated signing that sniffs personal-sign vs
EIP-712 from the pasted content. Result dialog shows the 65-byte `r‖s‖v`
signature with copy.

**EIP-712 on Ledger** (`LedgerSigner.sign_typed_data`, ledger.py:363) — qeth
hashes the typed data with `eth_account.encode_typed_data` and passes the
**domain separator (`header`) and struct hash (`body`) as two separate 32-byte
values** to `sign_typed_data_draft`. This matters: an earlier version sliced
`body` as if it held both halves (sending the struct hash *as* the domain and
empty bytes *as* the message), which the Ledger Ethereum app rejected with an
"invalid data" status word — which is why Holyheld-style v4 signing failed on a
Nano S while Frame worked. The Ethereum app does the full struct-hash walk
on-device for small payloads; larger ones fall back to a **blind-sign** prompt
the user must enable in device settings. (Lengths are asserted == 32 before
sending, so a malformed encode fails loudly rather than reaching the device.)

---

## 9. Token discovery pipeline — `plugins/tokens.py`

The heart of the app. On selecting a wallet, the panel renders the disk cache
immediately, then runs a multi-stage refresh that corrects it to chain-head
truth.

### 9.1 The discovery chain

For a `(chain, address)` view:

1. **`TokenListWorker`** — native balance via `EthClient.get_balance` + the
   source's ERC-20 list (Etherscan/Blockscout). Its balances are **discarded**
   (they lag); only the **contract set** is kept.
2. The contract set is the **union** of: source-listed contracts + user
   **force-shown** (pinned) + **sibling-held** contracts (§9.4) +
   **receipt_extras** (§9.3). De-duplicated.
3. **`MetadataWorker`** — multicall `name/symbol/decimals` for any contract not
   already in `TokenMetadataCache` (immutable, permanent). Prefilled at startup
   from curated lists (`_prefill_metadata_from_token_lists`) so first refresh of
   a chain usually skips this entirely.
4. **`BalanceWorker`** — multicall `balanceOf` + `eth_getBalance` for the full
   set, at chain head.
5. **`RiskWorker`** — GoPlus, **only for non-whitelisted** contracts (whitelist
   ⇒ assumed safe).
6. **`PricesWorker`** — DefiLlama for the displayed set + native.
7. **`_on_combined_ready`** — applies visibility (hidden/dust/force-show), sorts
   by USD value, renders once, and saves the **normal-mode visible set** to the
   wallet cache (never the show-all superset).

A **per-view in-flight guard** (`_discovery_in_flight`) prevents stacking
duplicate chains when `_refresh` fires repeatedly for the same view. Each run
captures its state in a per-call `pv` dict, and stale results are dropped by
comparing `view_key` against `_displayed_view`.

### 9.2 Two refresh triggers

- **60-second timer** (`_on_refresh_tick`) — periodic full refresh of the
  current view (self-dedupes via the in-flight guard).
- **Receipt-driven** (`note_receipt_logs`, §9.3) — fires the instant a tx the
  wallet broadcast confirms.

### 9.3 Receipt-driven crediting

When the Transactions plugin confirms a receipt it calls
`note_receipt_logs(chain, receipt)`, which scans the logs for ERC-20 `Transfer`
events (`topic0 = 0xddf2…b3ef`, **exactly 3 topics** to exclude ERC-721) that
touch any of the wallet's addresses. For each match it:

- records the token contract into `_receipt_contracts` so the next discovery's
  union (§9.1 step 2) **forces it into the multicall** — chain-head visibility
  without waiting for the indexer;
- on the **receiving** side, calls `_apply_receipt_credit_to_cache` to bump (or
  add) the balance directly in the disk cache;
- if the affected wallet is the active view, calls
  `_invalidate_view_and_refresh` to repaint now.

> **`_apply_receipt_credit_to_cache` skips tokens with no locally-cached
> metadata** (tokens.py:531). A brand-new, non-curated token has no metadata, so
> the instant credit is skipped and the token surfaces only after the full
> discovery cycle (which fetches metadata on-chain). For curated tokens
> (metadata prefilled) it appears almost immediately — bounded only by the
> Transactions plugin's **10-second receipt poll** (§10.2), not by token-list
> fetching.

### 9.4 Sibling cross-check

`_sibling_held_contracts` unions every contract ever cached by *any other*
wallet on the chain into the discovery set — so an intra-qeth A→B transfer shows
up on B at chain-head speed instead of waiting for the indexer. **Zero-balance
siblings are deliberately included**: if A sent its whole USDC stash to B and A
refreshed first, a `balance > 0` filter would drop USDC and B would never query
it.

### 9.5 Visibility

- **Hidden** — filtered out pre-render unless "show all" is on.
- **Force-shown (pinned)** — always rendered, even at zero/dust.
- **Dust** — below `$0.01` (USD-gated, not balance-gated) dropped in normal mode
  unless pinned.
- **Scam alarm** — `is_likely_scam` (curated lists + GoPlus, §4.2/§4.4).

`_invalidate_view_and_refresh` clears `_displayed_view` + the in-flight guard and
re-runs a full discovery; it's the common path behind hide/pin/show-all toggles,
custom-token add, and receipt credits.

**Keyboard:** `Ctrl+C` copies the selected token's contract address — a Copy
action on the table, tree-scoped (`WidgetWithChildrenShortcut`) like the
accounts panel (§8.1), reusing the Copy button's handler.

---

## 10. Transactions plugin — `plugins/transactions.py`

### 10.1 History rendering

Renders the per-wallet tx cache (§2.5) merged with fetched pages from the
history source (§4.6). **Bounded initial render** (`INITIAL_VISIBLE = 200`)
avoids O(N²) row re-measurement on large histories; more rows reveal on
scroll-to-bottom, then network pages are walked. `update_tx_by_hash` repaints a
single row (used for pending→confirmed) instead of rebuilding the table.
**Keyboard:** `Ctrl+C` copies the selected transaction's hash (table-scoped,
reusing the Copy-Tx-Hash handler).

### 10.2 Pending → confirmed / dropped, and re-broadcast

`PendingTxWatcher` sweeps the cache for `pending=True` entries **every 10s**
(`POLL_INTERVAL_MS`), with an immediate tick on start (so app-restart pending
txs are checked at once). `_in_flight_hashes` dedupes overlapping ticks. Each
pending tx gets a `PendingProbeWorker` that does a three-way diagnosis (not just
a receipt poll), which matters because load-balanced RPCs like DRPC sometimes
**ack a tx they never propagate** — it would otherwise show "forever pending":

1. **Receipt present** → confirmed. `_confirmed_from_receipt` merges block
   number, gas used, effective gas price and success (from `receipt.status`)
   into the cached tx, flips `pending=False`, repaints the row, and **forwards
   the receipt to the Tokens plugin** (§9.3).
2. **No receipt, but `tx.nonce < getTransactionCount(from, "latest")`** → the
   nonce was already mined by a *different* tx (replacement / the user re-sent),
   so this hash can never confirm → **`dropped`** (a terminal state, distinct
   from a reverted tx). The nonce check is the reliable death signal: mined-nonce
   is consistent across a load-balanced RPC's backends, unlike a mempool query.
   `from_addr` is stored lowercased, so it's **checksummed before the lookup**
   (web3.py rejects non-checksum addresses — the trap that otherwise made the
   probe fail silently).
3. **No receipt, nonce still open** → genuinely unconfirmed → **re-broadcast** the
   stored raw signed tx (`Transaction.raw_signed`, persisted at `add_pending`
   time, threaded out of `SignAndBroadcastWorker`). No re-sign needed — same
   bytes, same hash; idempotent ("already known" / "nonce too low" swallowed).
   Capped at ~30 ticks (`REBROADCAST_MAX_ATTEMPTS`), then a one-time "giving up"
   warning. `raw_signed` is public data (no key material), cleared on
   confirm/drop.

Status column renders themed icons with Unicode-glyph fallback (§5.3 pattern):
`content-loading`/⏳ pending, `dialog-ok`/✓ success, `dialog-error`/✗ reverted,
`user-trash`/⊘ dropped — never blank, tooltip carries the meaning.

> The lag between a tx mining and its tokens appearing is dominated by this 10s
> poll — qeth learns of confirmation on the next tick, then surfaces tokens
> synchronously. It does **not** poll token balances on a separate timer in
> response to confirmation.

### 10.3 Decoded-call rendering & gas policy

`_render_decoded` shows the called function as an annotated tree (bold name,
blue types, green values) using the ABI from `abi.py`; for curated tokens it
annotates `uint` args on transfer/approve-family functions as `X SYMBOL`, with
the `2²⁵⁶−1` sentinel shown as "unlimited". `address` args that are **one of the
user's own wallets** (`known_addresses`) render **bold+italic** so the user can
see at a glance when a call touches their own accounts. A bold-capable monospace
family is picked explicitly (`_pick_mono_font`) — the generic `monospace` alias
is Regular-only on Linux and would drop the bold.

**`apply_gas_policy`** (transactions.py:1809+) deliberately diverges from the
traditional "match the dapp" approach:

- **Gas limit** = `max(estimate × 1.5, dapp-requested gas)` — the dapp can ask
  for more but never less than 1.5× the node estimate.
- **EIP-1559, `baseFee > 0`**: `maxFeePerGas = baseFee × 2`;
  `maxPriorityFeePerGas = baseFee × 0.05` (`(ref*5)//100`). The **5% tip is
  always applied even if the dapp requested something different** — this was an
  explicit decision after a dapp set a 2 gwei tip on a 0.1 gwei base fee. The
  dapp can still raise `maxFee` (taken if higher), and the user can edit both in
  the dialog.
- **EIP-1559, `baseFee == 0`** (BSC-style): `maxFee = gasPrice × 2`,
  `priority = gasPrice` — the percent-of-base formula would yield a tip of zero
  and the tx would be rejected for too-low priority.
- **Legacy (non-1559)**: `gasPrice = current × 1.35` (≥ the dapp's).

Expected fee shown in the dialog is `base + tip` (not max), with a USD estimate;
the value being sent is shown separately, never folded into the fee. The
editable gas controls (limit, max fee, priority/gas price, network base fee)
live behind a **collapsed "Gas settings" expander** (§5.8) since the auto policy
is sensible; only the **Expected fee** (and, for native sends, **Total to send**)
stays visible as the always-on summary.

### 10.4 Send dialog recipient affordances — `SendTokenDialog`

The user-driven send dialog (counterpart to the dapp `SignTransactionDialog`)
tints the recipient field as you type, set as a self-consistent (bg, fg) colour
pair so it reads in any palette:

- **Red** — the recipient is a **token contract** (the asset's own contract, or
  any address on the curated lists, held or not). Sending tokens/ETH to a token
  contract usually burns the funds; this **outranks** the green hint. Detection
  is local (no network).
- **Green** — the recipient is **one of the user's own wallets** (matched
  case-insensitively against the account list passed in as `known_addresses`).
- Cleared otherwise. The hint only restyles on an actual state transition (not
  every keystroke).

Both this dialog and the dapp `SignTransactionDialog` are **tabbed
(Details | Events)**: the editable form + decoded call + gas controls live on
Details, and an **Events** tab previews the logs the tx would emit *before
broadcast* via simulation (§10.5, §10.6).

### 10.5 Events view — `_EventsView`

`_EventsView` is a reusable pane (fed by `set_logs()` / `set_placeholder()`)
that renders a decoded list of event logs Python-style — `*USDS(0xdC03…)
.Transfer(from=…, to=…, value=… # 5000 USDC)`. It is used in three places off
one implementation:

- **Confirmed-tx details** (its own "Events" tab) — logs come from the receipt
  (`LogsFetchWorker` → `eth_getTransactionReceipt`).
- **Send & dapp-Sign dialogs** ("Events" tab) — logs come from simulation
  (§10.6), so it's a *pre-broadcast* preview.

Behaviour:

- **Default filter:** only Transfer / Approval-family events that **touch one of
  our wallets** (`known_addresses`); a **"Show all events"** toggle reveals
  every log.
- **Naming:** the Transfer/Approval family decodes with no ABI (§4.5); other
  events are named only under "show all", which triggers a **lazy per-contract
  ABI fetch** (`AbiCache` first, then Blockscout) and re-renders as ABIs land.
- **Token context:** a contract that's a known token is prefixed with its
  **logo + symbol** (icon embedded as a `QTextDocument` image resource), and
  fungible amounts get the human-readable `# 5000 USDC` / `# unlimited USDC`
  comment — same treatment as the decoded call (§10.3), including the
  bold+italic own-address highlight and the bold-capable mono font.

### 10.6 Transaction simulation — `simulate.py`

`simulate_logs(chain, from, to, data, value)` returns the event logs a
not-yet-broadcast tx would emit (the `decode_event`-ready
`{address, topics, data}` shape), or `None` for a contract creation, a revert,
or when no route can run it. Two routes, picked **per RPC URL**:

1. **Fast path — `eth_simulateV1`.** One request: the node runs the call against
   latest state and returns the logs. No per-slot round-trips, no rate-limit
   burst. Support isn't universal (DRPC/mevblocker/publicnode yes;
   cloudflare-eth `-32601`; Arbitrum-on-DRPC `400`), so it's **probed by using
   it** and the result cached in `_SIMV1_SUPPORT` (probe-don't-hardcode).
2. **Fallback — local revm fork (`pyrevm`, optional dep).** Universal: forking
   uses only standard state reads every endpoint exposes. Two non-obvious
   fixes baked in:
   - **Block env** — pyrevm forks *state* at latest but leaves the block env
     zeroed (`timestamp == 1`), so contracts doing time math (oracle staleness,
     deadlines, TWAP) falsely revert; we fetch the real latest block and
     `set_block_env` before the call.
   - **Rate-limit retry** — the per-slot read burst can trip a throttled
     endpoint; transient `code 15` / `429` are retried with backoff, a genuine
     revert fails fast (with the decoded `Error(string)` / `Panic` reason
     logged).

`simulation_available(chain)` reports whether *either* route can run (so the UI
shows a "no preview" note only when neither can). The preview runs **off-thread**
(`SimulateWorker`), **lazily** on Events-tab open, and is **cached per tx-params**
— the Send dialog re-simulates only after recipient/amount change; toggling tabs
doesn't refork. A definitive `simulateV1` revert is *not* retried via the fork.
The whole feature degrades to "no preview" when neither route is available
(`pyrevm` is an optional `[simulate]` extra). Wired into the dialogs by
`_EventPreviewMixin`.

---

## 11. End-to-end flows (quick reference)

**Select a wallet** → `show_cached` paints instantly → discovery chain (§9.1)
corrects balances/prices → `_on_combined_ready` re-renders and re-saves cache.

**Send/claim a tx (via UI or dapp)** → signed+broadcast in a worker → added to
tx cache as `pending` → UI flips to the tx's chain/account → `PendingTxWatcher`
(≤10s) confirms → row updates + `note_receipt_logs` credits/forces the new
tokens → tokens appear (instantly if curated, after a discovery cycle if brand
new).

**Preview a tx's events before sending** → open the **Events** tab in the Send
or dapp-Sign dialog → `simulate_logs` runs off-thread (`eth_simulateV1` if the
RPC supports it, else a local pyrevm fork) → predicted logs decode through
`_EventsView` (§10.5) — the same pane the confirmed-tx details use — so the user
sees the Transfers/Approvals the tx *will* emit. Cached until inputs change.

**Dapp connects & switches chain** → JSON-RPC over `:1248` →
`wallet_switchEthereumChain` pins **only that origin** (§6.2) and emits a scoped
`chainChanged`; other tabs and the wallet UI are unaffected.

**Dapp requests a signature** → parsed to a typed request → `SignerBridge`
queues it to the main thread → confirmation dialog → worker signs (+broadcasts
for txs) → future resolves → JSON-RPC returns the hash/signature.

**Upstream RPC goes down** → first failure trips the per-host fail-fast cooldown
(§6.5) → subsequent dapp polls get an immediate clean error instead of hanging →
the app stays responsive → first call after 5s re-probes.
