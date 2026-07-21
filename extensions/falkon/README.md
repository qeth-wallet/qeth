# qeth connector for Falkon

A Falkon browser plugin that lets web dapps talk to a running **qeth**
wallet — the same role Frame's browser extension plays for the Frame
desktop app.

It injects a standard wallet provider (`window.ethereum`, EIP-1193, plus
EIP-6963 discovery) into every page. **No changes to the wallet core** —
the plugin is pure browser glue around qeth's loopback JSON-RPC server on
`127.0.0.1:1248`.

## The CSP problem, and how this solves it

A provider injected into the page's own JavaScript is bound by the dapp's
**Content-Security-Policy** (`connect-src`) and Chromium's **Private
Network Access**. A strict dapp (e.g. one whose CSP lists only its own API
hosts) therefore *blocks* any `fetch`/`WebSocket` to `127.0.0.1:1248` —
even though the provider shows up in the wallet picker, the connection
fails. A real browser extension like Frame dodges this because its content
script runs in an isolated world with extension privileges; an injected
page script gets no such exemption (verified: both main and isolated
worlds are blocked by page CSP in QtWebEngine).

So the connector does the networking in **native Python**, reached over
Falkon's per-page **QWebChannel** — whose transport is internal Qt IPC,
not a CSP-governed network call:

```
  dapp (MainWorld) ── window.ethereum ──▶ provider.js
        │   window.postMessage  (crosses JS worlds; not subject to CSP)
        ▼
  relay.js (Falkon SafeJsWorld) ── window.external.extra.qeth ──┐
                                          (Falkon maps every     │ QWebChannel
                                           registered qz_<id>     │ (Qt IPC)
                                           extra object here)     ▼
                                                       QethBridge  (Python QObject)
                                                            │  QNetworkAccessManager (HTTP)
                                                            ▼     — native Qt, outside Chromium:
                                                       qeth  127.0.0.1:1248   no CSP, no PNA
```

The bridge talks to qeth over plain HTTP (Qt's own network stack). We
deliberately avoid QtWebSockets — it isn't present in every PySide6 build
(it's absent from the system Qt this was developed against) — so live
wallet events are surfaced by the provider **polling** the locally-served,
cheap `eth_chainId` / `eth_accounts` rather than a push subscription. When
you change account or chain in the qeth UI, dapps see it within a few
seconds.

## What it does

- Injects `provider.js` at document-creation in the page's main world, so
  `window.ethereum` exists before the dapp's own scripts run.
- Announces qeth via **EIP-6963** (`eip6963:announceProvider`) with the
  wallet name and logo, so modern dapps list it in their picker with no
  `window.ethereum` races. (Top frame only — see the sub-frame note below.)
- Sets `window.ethereum.isMetaMask = true` so dapps that only wire up the
  injected wallet for MetaMask (Web3Modal / Reown AppKit / wagmi's
  `injected` connector — e.g. Holyheld) accept qeth instead of falling
  back to a WalletConnect QR. Modern dapps still get the real "qeth"
  identity via EIP-6963, so nothing that already works regresses. (Top
  frame only — see the sub-frame note below.)
- **Stays inert inside cross-origin sub-frames** — most importantly a Safe
  App running in an iframe inside the Gnosis Safe UI (`app.safe.global`).
  `window.ethereum` still exists there (so a dapp that touches it at startup
  doesn't break), but it does *not* claim to be MetaMask, is *not* announced
  via EIP-6963, and reports no account until an explicit
  `eth_requestAccounts`. Otherwise a dapp's wallet library would auto-pick
  our always-authorized injected provider ahead of its **Safe connector**
  and show the signer EOA instead of the multisig. This mirrors Frame, whose
  injected provider is likewise not-MetaMask and unauthorized-until-approved
  inside a frame — so the Safe App resolves its address over the Safe Apps
  SDK (postMessage to the parent Safe) and shows the multisig.
- Carries each request's real dapp **Origin** through to qeth, so
  per-origin chain selection (`wallet_switchEthereumChain`) scopes to the
  requesting dapp — one dapp switching chains doesn't move the others.
- Emits `connect` / `chainChanged` / `accountsChanged` to the dapp, and
  keeps a **direct `fetch` fallback** for the case where the relay never
  loads (e.g. a permissive-CSP site, or running outside this plugin).

Signing (`eth_sendTransaction`, `personal_sign`, `eth_signTypedData*`) is
handled by qeth itself: the provider forwards the request and qeth pops
its own confirmation UI. Whatever qeth's server supports, the dapp gets.

There's nothing to configure, but the plugin's **Settings** button
(Preferences → Extensions) opens a small read-only status dialog showing
whether the qeth wallet is reachable, and if so the current account and
network — handy for confirming the link is live.

## Install

Falkon loads Python plugins from `~/.config/falkon/plugins/`. Symlink (or
copy) the plugin directory there, keeping the directory name a valid
Python module name (`qeth_connector`):

```bash
mkdir -p ~/.config/falkon/plugins
ln -s "$PWD/qeth_connector" ~/.config/falkon/plugins/qeth_connector
# or copy:  cp -r qeth_connector ~/.config/falkon/plugins/
```

Then in Falkon: **Preferences → Extensions**, enable **Python Plugins**
if needed, and tick **qeth**. (Falkon must be built with PySide6
Python-plugin support — KDE's packages are. If the *Python Plugins*
checkbox is absent, your Falkon lacks it.)

Restart Falkon, start qeth, and visit a dapp — qeth should appear in the
wallet picker (or as the injected `window.ethereum`), and connect even on
strict-CSP sites.

## Requirements

- Falkon with Python (PySide6) plugin support — uses `QtWebEngineCore`,
  `QtWebChannel`, and `QtNetwork` (all standard; **not** QtWebSockets).
- qeth running (its RPC server listens on `127.0.0.1:1248`).

## Files

- `qeth_connector/__init__.py` — the Falkon plugin: registers the native
  bridge on Falkon's web channel and injects the two scripts.
- `qeth_connector/bridge.py` — `QethBridge`, the native HTTP relay to qeth.
- `qeth_connector/provider.js` — injected EIP-1193 / EIP-6963 provider
  (page main world).
- `qeth_connector/relay.js` — SafeJsWorld relay bridging postMessage to
  the native bridge.
- `qeth_connector/settings.py` — read-only connection-status dialog
  (the extension's Settings button).
- `qeth_connector/qeth-icon.svg` — wallet logo (also the EIP-6963 icon).
- `qeth_connector/metadata.desktop` — Falkon plugin manifest.

## Notes / limitations

- Live events arrive via polling (a few seconds' latency), not a push
  subscription — a deliberate trade to avoid the QtWebSockets dependency.
- Node-level subscriptions (`newHeads`, `logs`) aren't streamed; they'd
  need a persistent socket. Wallet-state events (account/chain) are
  covered by polling.
- qeth's server also sends `Access-Control-Allow-Private-Network: true`
  (in `qeth/rpc.py`). The bridge path doesn't need it — native Qt
  networking is exempt from Private Network Access — but it lets the
  direct-`fetch` fallback work on permissive-CSP sites.
