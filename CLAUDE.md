# qeth — Claude context

Qt-based Ethereum wallet with Ledger support and a Frame-compatible
JSON-RPC server. Desktop Linux primary; targets system PySide6 so the
user's Qt theme applies.

## Layout

- `qeth/__main__.py` — entry point (`uv run python -m qeth`)
- `qeth/ui.py` — PySide6 main window, dialogs, token panel, QThread workers
- `qeth/store.py` — JSON config at `~/.qeth/config.json` (accounts, chains, default account, hidden/shown token overrides)
- `qeth/chain.py` — sync JSON-RPC client (`EthClient`) shaped like `w3.eth.*`; the seam for swapping to web3.py later
- `qeth/async_chain.py` — async transport (`AsyncWeb3` over a `WebSocketProvider` or async-http failover stack) for the live watcher; mirrors `chain.py`'s UA / failover / PoA plumbing. `ws_urls_for(chain)` resolves explicit `Chain.ws_url` → inherited default → derived `https→wss`.
- `qeth/plugins/transactions/live_watcher.py` — the WebSocket live-update watcher: a `QThread` running one asyncio loop that subscribes per active chain to `newHeads` (→ pending-tx confirmation, the async port of `PendingProbeWorker`) and the on-screen account's ERC-20 `Transfer` logs (→ live token balances). **On by default**; `QETH_LIVE_WS=0` disables it. A pure accelerator over the always-on polling floor. Design + rationale in `docs/ws-subscriptions.md`.
- `qeth/chains.py` — `Chain` dataclass (incl. `ws_url` for the live watcher) + `DEFAULT_CHAINS` (Ethereum, OP, Polygon, Arb, Base, all on DRPC)
- `qeth/ledger.py` — Ledger signing + account discovery via `ledgereth`. Driven from QThread workers, but **all** ledgereth/hidapi calls route through `ledger_hid.py` (never touch HID from a worker directly — see the thread-safety convention below).
- `qeth/ledger_hid.py` — single-thread Ledger/HID execution service. Funnels every ledgereth call (discovery, signing, the availability probe) through one process-lifetime thread via `run_ledger_hid_job()`, and clears ledgereth's dongle cache after each job. hidapi's macOS backend ties the HID handle to the thread that opened it, so this is load-bearing on macOS.
- `qeth/token_discovery/` — every way tokens are found: `sources.py` (per-holder explorer sources: Etherscan v2 / Blockscout + the `BLOCKSCOUT_INSTANCES` / `ETHERSCAN_V2_*` endpoint maps reused across the codebase; `TokenSource` ABC), `tokenlists.py` (curated whitelists from Uniswap / CoinGecko / Curve / 1inch, merged + disk-cached at `~/.qeth/tokenlists/`), `toptokens.py` (top-by-market-cap head), `own_history.py` (`discover_own_tokens` — vault/LP tokens the user obtained via their OWN txs, reconstructed from the tx + activity caches). Public API re-exported from the package `__init__`.
- `qeth/pricing/` — USD price discovery: `base.py` (`Price` / `PriceSource`), `native.py` (native-asset CoinGecko id), `defillama.py` (the free keyless primary source), `onchain.py` (on-chain vault/LP pricing — ERC-4626, Curve LP, UniV2 LP — via `EthClient` multicall, with `ChainedPriceSource` falling back to it when DefiLlama has no quote).
- `qeth/plugins/transactions/contract_identity.py` — "what is this contract": name/verified (Etherscan v2 `getsourcecode`) + deployer/date (`getcontractcreation`) + public name-tags for the address and its deployer (Blockscout OLI metadata service — free; "AladdinDAO: Deployer", "Binance: Hot Wallet"), permanently disk-cached at `~/.qeth/contract_id/`. `describe_identity()` renders a multi-line badge: headline (name-tag › ContractName › ⚠ unverified) / provenance (deployed-date · deployer-label-or-cluster) / familiarity ("you've interacted N×" from the local tx cache, ⚠ on first). Shown on the Contract: row of the details + signing + send dialogs.
- `qeth/plugins/approvals/` — the Approvals tab (revoke.cash-style ERC-20 allowance manager). `discovery.py` (Qt-free): `approve_pairs_in(txs, account)` pulls (token, spender) pairs straight from the account's own `approve()`/`increaseAllowance()` txs — no token×spender cross-product — and `fetch_allowances(client, owner, pairs)` multicalls `allowance(owner, spender)`, keeping the nonzero ones. `__init__.py`: `ScanWorker` progressively pages the account's FULL tx history (explorer before_block cursor walk, newest-first), emits each batch for the plugin to `merge_txs` into the shared `TransactionCache` on the MAIN thread (the cache has no lock), and every few pages re-checks newly-found pairs → `rows_ready`; a snapshot that's already `_is_full_history` skips all fetching. `ApprovalsPanel` renders an always-expanded token→spender `QTreeWidget` — two columns (identity, allowance), coin icons via the shared `IconCache`, checkable leaves, `ItemIsAutoTristate` token nodes — with a bottom scan progress bar + Stop. Spender leaves show WHO the contract is via keyless Blockscout name-tags (`ContractIdentitySource.fetch_labels`, batched in the worker), revealing the spender's actual address on hover/selection (double-click or the explorer button/menu opens it on the chain's block explorer). Allowances render compact via `_format_allowance` (`format_balance`'s `× 10ⁿ`, "unlimited" at ≥ 2²⁵⁵ — the approve dialog's threshold — so near-max sentinels don't show as 70-digit numbers) and the tree middle-elides with the horizontal scrollbar off (the Accounts-tab trick), so nothing ever scrolls sideways. Each leaf can **Modify** (opens the shared review+sign flow — `host.request_transaction` — pre-filled `approve(spender, 0)`, editable) or **Revoke** (same, set to zero); checking several leaves and hitting Revoke runs them through `revoke_queue.py`'s `RevokeQueue` — one sign dialog at a time, advancing the moment each *broadcasts* (nonce-safe: `add_pending` records the pending tx before `on_broadcast`, so the next dialog resolves to N+1 via `pending_nonce_floor`). Optimism is bounded by an on-chain re-read: broadcast → "pending…", confirm → debounced `ReconcileWorker` reads `allowance` fresh (0 removes the leaf, else updates it). `on_cancel` (threaded through `Host.request_transaction`) aborts a batch; `_invalidate` aborts on account/chain change + shutdown. Side benefit: a completed scan leaves the account's whole history cached. Depends only on the shared token/tx modules + the `transactions` plugin (`requires=("transactions",)`).
- `qeth/icons.py` — disk+memory icon cache (`~/.qeth/icons/`) + `bundled_native_icon` / `bundled_chain_icon` lookups
- `qeth/rpc.py` — aiohttp HTTP+WS JSON-RPC server on `127.0.0.1:1248` (Frame-compatible)
- `qeth/assets/{native,chains}/*.png` — bundled logos; shipped via `pyproject.toml` `package-data`

## Dev / Run

Use **uv** (not raw `python -m venv` / `pip`):

```bash
uv venv --system-site-packages    # IMPORTANT: pulls in system PySide6
uv sync --inexact                 # installs from uv.lock (reproducible)
uv run python -m qeth
```

`uv.lock` is committed and pins every transitive dependency by hash.
`uv sync --inexact` reads the lock and installs exactly those
versions without touching packages already present from
`--system-site-packages` (notably system PySide6). Use `uv lock`
to regenerate after editing dependencies in `pyproject.toml`.

**Bumping / security.** `uv lock --upgrade && uv sync --inexact && uv audit`.
`uv audit` checks the locked deps against the advisory DB (it's how the
aiohttp CVEs were caught). A `[tool.uv] exclude-newer = "1 week"` supply-chain
policy means a bump never adopts a release younger than ~1 week (rolling,
evaluated at resolution time — no date to maintain); `ty` is exempted and
fresh security fixes are pulled forward per-package — see the comments in
`pyproject.toml`.

`--system-site-packages` is load-bearing on Linux desktops. The PyPI
`PySide6` wheel ships its own Qt without the qt6ct/KDE platform-theme
plugin, so the user's qt6ct/Breeze/Kvantum setup is silently ignored.
System Qt has the plugin and everything renders correctly. Fallback
for systems without system PySide6: `uv pip install -e '.[bundled]'`.

**Caveat it creates:** because the venv inherits the system site-packages,
a module qeth imports but doesn't *declare* can be silently satisfied by a
distro package — and break on a self-contained / non-Linux install. This
already bit `cryptography` (Frame-import only; now the `frame` extra +
guarded import). When adding an import, declare it (core dep or an extra);
don't lean on system-site-packages.

### Static checks / gates

`uv run pytest` enforces, over the whole package:

- **mypy** (types) — `tests/test_typing.py`, config in `[tool.mypy]`.
- **ruff** (lint) — `tests/test_lint.py`, ruleset in `[tool.ruff.lint]`:
  `E9` + `F` (pyflakes) + `RUF012` + `UP` (py310 annotation modernization).
  `E402` (lazy imports are intentional) and `UP031` are deliberately off.
  ruff is a standalone binary (no `python -m ruff`) — the gate resolves it
  via `shutil.which`.

`scripts/check.sh` runs ruff + mypy + **ty** (Astral's preview type checker)
in one shot. ty is **not** a gate (it's 0.0.x); run it for extra signal.
It doesn't follow `include-system-site-packages` into the system PySide6, so
the script injects that path (derived from PySide6's location) — without it
ty floods ~100 phantom unresolved-import errors. The package is ty-clean
when checked that way. mypy, ruff, and ty are all **dev-only** deps
(`[dependency-groups] dev`), so a runtime/`--no-dev` install pulls none of
them. Optional features that need a heavy dep go behind an extra mirrored
into `dev` (`simulate` → py-evm, `frame` → cryptography) so their tests run.

**Extra signal (not gates), all via `uvx` so they don't touch project deps:**
- `uvx deptry .` — declared-vs-imported dependency hygiene (catches the
  cryptography-class "imported but undeclared" bug). Config in `[tool.deptry]`.
  Run after touching imports/deps.
- `uvx detect-secrets scan --baseline .secrets.baseline` — secret scan. The
  committed `.secrets.baseline` records the known test-fixture keys/hex (e.g.
  `_TEST_PRIV`), so a run flags only NEW potential secrets. Regenerate with
  `uvx detect-secrets scan > .secrets.baseline` after adding intentional
  fixtures.

## Conventions

### Plugin architecture: registry + manifests, restart-to-apply toggles

The UI is composed of `Plugin`s (`qeth/plugin.py`) mounted in `Slot`s; each
plugin owns its widget, workers, caches, and lifecycle, and reaches siblings
only through the typed `Host` protocol (implemented by MainWindow). **Adding a
plugin = a package under `qeth/plugins/<id>/` + one `PluginManifest` entry in
`qeth/plugins/registry.py`.** The manifest carries `id, title, factory
(lazy-imports the package), slot, order, required, requires, hides_chain_selector,
persists_header, description` — the same shape a setuptools entry-point would
resolve to, so external/paid "assembly" plugins can be discovered later with no
change to consumers.

- **On/off**: `Store.disabled_plugins` (a set of ids) is read once at startup by
  `registry.enabled_manifests(store)`; a disabled optional plugin is never
  constructed *and* never imported (only the lazy factory imports it).
  **Restart-to-apply** — toggling only writes the store. Toggles live in two
  places sharing one builder (`qeth.plugin_toggle.build_plugin_toggle_menu`): the
  tray "Plugins" submenu and the config **gear** on the right slot's tab row
  (`Slot.set_corner_widget`). `wallets` + `transactions` are `required=True`
  (account source / composer+sign+watcher service layer) and can't be disabled.
- **Reaching an optional sibling**: `host.plugin(id) -> Plugin | None` (never a
  `host.<name>_plugin` attribute). Callers MUST handle None — e.g. the 7
  TransactionsPlugin→TokensPlugin live-balance relays. MainWindow's
  `tokens_plugin`/`ens_plugin` convenience properties are `Optional`; every
  consumer guards them. App-wide services a disabled plugin would otherwise own
  live on MainWindow (the token `IconCache`), so siblings keep working.
- **Not yet done**: per-plugin package moves (helpers still at `qeth/` top level);
  a multi-select selection broadcast (deferred to the portfolio plugin).

### On-chain math: always `Decimal`, never `float`

Native amounts (wei → ether) and ERC-20 amounts must go through
`qeth.chain.wei_to_ether(wei) -> Decimal` or
`TokenBalance.balance` (also `Decimal`). Double float has ~15–17
sig digits; wei has 18 decimal places. `wei / 1e18` silently corrupts
the last digits, and the bug compounds when the value reaches tx
construction or comparisons. `float` is fine for non-money: HTTP
timeouts, TTLs, UI sizes, animation frames.

### PySide6 signals: `Signal(object, ...)` for big ints (and any container holding them)

`Signal(int, ...)` maps to `qint32` and overflows past ~2.1×10⁹. Any
signal carrying wei, gas, block numbers, nonces, or token amounts
must use `Signal(object, ...)`.

The trap is bigger than it looks: PySide6 also marshals the *contents*
of container parameters declared as `dict` / `list` / `tuple`, so
`Signal(int, dict)` overflows when any dict value is a big int (we
hit qint64 overflow at ~3.2×10¹⁹ on raw ERC-20 balances). Treat
container parameters carrying chain values the same way — use
`Signal(..., object, ...)` not `Signal(..., dict, ...)`. `int` is
fine only for small, bounded primitives (list indices, derivation
indices, table rows, page numbers).

**Chain ids are NOT bounded** — dapps add chains via
`wallet_addEthereumChain` with ids above qint32 (Palm = 11297108109),
so a chain-id parameter uses the string-typed 64-bit form:
`Signal(QULONGLONG, ...)` with `from qeth import QULONGLONG` (an
`Any`-typed `"qulonglong"` — PySide6 accepts C++ type names as
strings, but the stubs type Signal's args as `type`, so the bare
string literal trips mypy). `"qulonglong"` round-trips any practical
chain id exactly — verified up to 2⁶³⁺. Money values stay `object`:
uint256 outgrows *any* Qt integer type — 10 ETH = 10¹⁹ wei already
exceeds qint64.

### Dialogs subclass `qeth.dialog.Dialog`, not `QDialog`

Every dialog inherits `Dialog` (`qeth/dialog.py`), which on first show
applies two house standards to its layout, both font-derived (so they
track the user's Qt font, not a pixel count):

- **Edge margins** — half a line-height on every side. Don't set the
  outer `setContentsMargins` per dialog (that's what made some dialogs
  crowd their content against the frame, others over-pad).
- **Paragraph spacing** (the GNOME-2-HIG rule). Treat each logical group
  — a value, a description, an input, the button row — as a *paragraph*:
  tight gaps *within* a paragraph, a wider gap *between* them. The base
  couples the two: the top-level box layout gets `group_spacing` (the
  BETWEEN-paragraph gap) and every form gets `item_spacing` rows +
  `label_spacing` columns (the WITHIN-paragraph rhythm). `group_spacing`
  is **2×** `item_spacing` — a clear paragraph break. Don't hand-set a
  form's `setVerticalSpacing` — the base owns it now. (A always-present-
  but-empty `QLabel` between paragraphs reads as an *extra* gap because
  an empty label still claims a font line — `setVisible(bool(text))` it;
  see the status-label note below.)

So just build `VBox[ form, …, buttonBox ]` and inherit it: the button row
sits a clear gap below the content — no per-dialog `setSpacing`/
`addSpacing` for the outer rhythm (those fight or double the standard).
**A label that captions a non-form widget** (a QTextEdit, a list) must be
grouped *with* it in a sub-`VBox` at `item_spacing(self)` so the two read
as one paragraph — otherwise the between-paragraph gap pushes them apart
(see `prompt_text`, `ChainRpcDialog`'s picker, `SignatureResultDialog`).
**An always-present-but-usually-empty status label** (validation hints)
must `setVisible(bool(text))` — an empty `QLabel` still claims a font line,
which reads as a stray paragraph gap (see `AddHotWalletDialog._set_status`).

Two timing details the base handles, both load-bearing on large-font
themes: the spacing is applied from `setVisible` (**before** Qt sizes the
window — doing it in `showEvent`, after the geometry was committed,
overflowed and clipped content); and `showEvent` grows the window height
to a word-wrapped label's real `heightForWidth` (Qt's auto-size uses the
label's wide ~1-line hint, so a label that wraps to several lines at the
real width would otherwise clip). A dialog with a genuinely bespoke layout
can set `_auto_spacing = False` to keep only the margins. Mixin dialogs
keep the mixin first: `class SendTokenDialog(_EventPreviewMixin, Dialog)`.

For one-off text prompts use `dialog.prompt_text(...)` (a `Dialog`-based
`QInputDialog.getText` replacement, so it inherits the margins) rather
than `QInputDialog`. Pass `password=True` to mask, `wide=True` for a
field holding a full address. Any field that holds a 0x address sets
`setMinimumWidth(address_field_min_width(self))` so the address shows
without horizontal scroll.

### Long-lived QThreads

Don't park a `QThread` in a single attribute slot (`self._worker = …`)
that gets reassigned on the next refresh. Overwriting drops the
previous worker; if it's still running, Qt's QThread destructor
`abort()`s the whole process. Track workers in a `set[QThread]` and
let them self-evict via the `finished` signal (`MainWindow._start_worker`
in this codebase is the pattern).

### Ledger / hidapi: one thread, always

hidapi (the USB-HID layer `ledgereth` sits on) is **not** thread-safe — its
macOS backend ties the open handle to the thread's CoreFoundation run-loop,
so a handle opened on one transient Qt worker and touched from another
corrupts state and hangs/crashes. **Never call `ledgereth` (or `init_dongle`)
from a worker thread directly.** Route every Ledger op through the
single-thread service: `run_ledger_hid_job(fn)` (`qeth/ledger_hid.py`) runs
`fn` serialized on one process-lifetime thread and blocks for the result.
Discovery batches paths into one job; signing does the device-holds check +
the sign in one job (shared dongle). The service clears ledgereth's dongle
cache after every job, so the old per-call cache-clearing is centralized
there — don't re-add it at call sites.

### Type checking: mypy enforced over the whole package

`tests/test_typing.py` runs `mypy` (config in `pyproject.toml`
`[tool.mypy]`) and fails the suite on any type error, so type hints are
enforced, not decorative. The `files = [...]` list covers the whole
package — Qt-free core **and** PySide6 UI layer — with no per-module
error-code exceptions. `check_untyped_defs` is on, so even unannotated
function bodies are checked. When adding a module, add it to `files`;
run `uv run mypy` to check directly.

- `chain.py` is the typed↔untyped seam: it `cast()`s our plain
  `str`/`dict` to web3's `ChecksumAddress`/`TxParams`/`BlockIdentifier`
  at the call boundary, and declares the lazy `_ensure_heavy_imports`
  names under `if TYPE_CHECKING:` so mypy resolves them.

**Use scoped Qt enum access** — `Qt.AlignmentFlag.AlignLeft`, not the
deprecated flat alias `Qt.AlignLeft`. The flat form works at runtime but
isn't in the PySide6 stubs, so it trips `attr-defined`. (The codebase was
migrated off the flat aliases; keep new code scoped. To find the scope
for a flat member, `type(Qt.AlignLeft).__name__` → `AlignmentFlag`.)

Qt gotchas the enforced check surfaces:
- Widgets/actions built lazily in a `_build()` method are `Optional[...]`
  — guard or `assert ... is not None` (capture a local first inside
  nested closures, which don't inherit the guard's narrowing).
- `QByteArray` → `bytes(qba.data())`, not `bytes(qba)`.
- A mixin that depends on its host class's attributes declares them under
  `if TYPE_CHECKING:` (see `_EventPreviewMixin`).
- Qt method bindings are positional-only (kwargs raise at runtime, and
  mypy won't catch it — stub/runtime gap); constructors usually take
  kwargs. `parent()`/`instance()` return the base type — narrow with
  `isinstance` before calling subclass methods.

### Use the chain abstraction, don't reinvent JSON-RPC

`qeth/chain.py` `EthClient` already has `get_balance`,
`get_block_number`, `chain_id`, `get_transaction_count`, `gas_price`,
`max_priority_fee`, `estimate_gas`, `call`, `send_raw_transaction`,
plus a low-level `rpc(method, params)` escape hatch. Don't write
ad-hoc `urllib` + JSON wrappers in new code — extend `EthClient`.

### Token discovery is a pluggable abstraction

Add new providers by implementing `qeth.tokens.TokenSource`
(`list_balances(chain, address) -> [TokenBalance]`, `supports(chain)`).
Add new curated whitelists by implementing `TokenListSource.fetch_entries`.
Per-source failures must be tolerated — one bad source never takes
the index down.

### Frame compatibility

The JSON-RPC server listens on `127.0.0.1:1248` (HTTP + WebSocket on
the same port, CORS open) so the Frame browser extension connects
unchanged. Wallet methods (`eth_accounts`, `eth_chainId`,
`wallet_switchEthereumChain`, `wallet_addEthereumChain`) are handled
locally; everything else is proxied to the current chain's RPC URL.
Signing methods currently return `-32601 "Signing not implemented in MVP"`.

`wallet_switchEthereumChain` from dapps changes the runtime chain
only — the user's persisted default (set via the toolbar) survives
restarts. UI changes persist; RPC chain switches are session-only.

## External services / URLs

- **DRPC** (`*.drpc.org`) — default RPC for all five chains. Cloudflare
  in front rejects the default `Python-urllib/x.y` User-Agent with HTTP
  403 / "error code: 1010"; any urllib code must set
  `User-Agent: qeth/0.1`. `aiohttp`'s default UA passes through.
- **Blockscout** — token-discovery (`eth.blockscout.com`,
  `optimism.blockscout.com`, etc.). Etherscan-compatible v1 API at
  `/api?module=account&action=tokenlist`; returns mixed ERC-20/721/1155
  so filter on `type`. Mainnet is slow for high-activity addresses.
- **Blockscout metadata service** (`metadata.services.blockscout.com/api/v1/metadata?addresses=…&chainId=…`)
  — the Open Labels Initiative dataset. Public address name-tags
  ("AladdinDAO: Deployer", "Binance: Hot Wallet"), **free + keyless**,
  batched, chain-aware. This is where qeth gets deployer/address labels —
  Etherscan has the same data but paywalls it behind PRO
  (`module=nametag` → "API Exclusive endpoint"). Response: `addresses[CHECKSUM].tags[]`,
  each `{name, tagType, ordinal}`; use `tagType=="name"`, highest ordinal.
- **ENS name discovery** — two keyless sources, merged in `EnsNamesWorker`.
  **BENS** (`bens.services.blockscout.com/api/v1/{chainId}/addresses:lookup?owned_by=true`)
  is keyed on the registry **controller** (the `owner` field; it does *not*
  populate `registrant`), so it misses a name you hold as the registrant (the
  NFT) but whose manager is delegated elsewhere — a common DAO setup, e.g.
  crv.eth (owner 0x7a16…, manager 0x3941…). To close that gap qeth enumerates
  the BaseRegistrar (`0x57f1…ea85`) ERC-721s the address holds via Blockscout's
  NFT API (`/api/v2/addresses/{addr}/nft?type=ERC-721`; tokenId =
  uint256(labelhash), no name in the metadata) and reverses each tokenId to a
  name via the **ENS metadata service** (`metadata.ens.domains/mainnet/{registrar}/{tokenId}`
  → `{name}`). Mainnet only; skips labelhashes BENS already returned. Wrapped
  names need neither path — BENS returns them with `owner=NameWrapper` because
  it matches on the wrapped owner internally. See `lookup_registrant_names`.
- **Curve** — official domain is `curve.finance` (**not** `curve.fi`,
  which 404s on most paths I tried). API base
  `https://api.curve.finance/v1/`, OpenAPI spec at
  `/v1/openapi.json` — pull that when looking for an endpoint rather
  than guessing path shapes. Per-chain tokens at
  `/v1/getTokens/all/{ethereum|optimism|polygon|arbitrum|base}`.
- **TrustWallet assets** — bundled logos sourced from
  `github.com/trustwallet/assets/master/blockchains/<slug>/info/logo.png`.
- **Token icons** — `logoURI` field in the tokenlists.org schema
  (Uniswap, CoinGecko, 1inch). Curve has no `logoURI`; we derive
  `curve-assets/main/images/assets/<addr_lower>.png` which works for
  Ethereum addresses and 404s silently elsewhere (the icon cache
  swallows failures).
