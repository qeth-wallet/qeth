# qeth — Claude context

Qt-based Ethereum wallet with Ledger support and a Frame-compatible
JSON-RPC server. Desktop Linux primary; targets system PySide6 so the
user's Qt theme applies.

## Layout

- `qeth/__main__.py` — entry point (`uv run python -m qeth`)
- `qeth/ui.py` — PySide6 main window, dialogs, token panel, QThread workers
- `qeth/store.py` — JSON config at `~/.qeth/config.json` (accounts, chains, default account, hidden/shown token overrides)
- `qeth/chain.py` — sync JSON-RPC client (`EthClient`) shaped like `w3.eth.*`; the seam for swapping to web3.py later
- `qeth/chains.py` — `Chain` dataclass + `DEFAULT_CHAINS` (Ethereum, OP, Polygon, Arb, Base, all on DRPC)
- `qeth/ledger.py` — Ledger account discovery via `ledgereth`, runs in a `QThread`
- `qeth/tokens.py` — token discovery sources (currently Blockscout); `TokenSource` abstract base
- `qeth/tokenlists.py` — curated token whitelists from Uniswap / CoinGecko / Curve / 1inch, merged + disk-cached at `~/.qeth/tokenlists/`
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

`--system-site-packages` is load-bearing on Linux desktops. The PyPI
`PySide6` wheel ships its own Qt without the qt6ct/KDE platform-theme
plugin, so the user's qt6ct/Breeze/Kvantum setup is silently ignored.
System Qt has the plugin and everything renders correctly. Fallback
for systems without system PySide6: `uv pip install -e '.[bundled]'`.

## Conventions

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
fine only for small, bounded primitives (chain ids, list indices,
derivation indices, table rows).

### Long-lived QThreads

Don't park a `QThread` in a single attribute slot (`self._worker = …`)
that gets reassigned on the next refresh. Overwriting drops the
previous worker; if it's still running, Qt's QThread destructor
`abort()`s the whole process. Track workers in a `set[QThread]` and
let them self-evict via the `finished` signal (`MainWindow._start_worker`
in this codebase is the pattern).

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
