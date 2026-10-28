# qeth — browser extension

Connects dapps in Chrome-family browsers and Firefox to the running qeth
desktop wallet (Frame-compatible JSON-RPC server on `127.0.0.1:1248`), the
same role the Falkon connector plays inside Falkon. One MV3 codebase, no
bundler, no npm.

The extension's version always equals the app version (`qeth/__init__.py`
`__version__`) — `build.py` stamps it into every package and `build.py sync`
writes it back into `manifest.json` and the Falkon connector's
`metadata.desktop`, so the two extensions never drift (a test gate enforces
it).

## How it works

```
page (MAIN world)          isolated world            extension background        qeth
provider.js ── postMessage ── relay.js ── runtime Port ── background.js ── WS ── :1248
(config.js sets flags/logo)   (per-frame)              (id remap, sub demux,
                                                        __frameOrigin stamp)
```

- `provider.js` is shared **byte-for-byte** with
  `extensions/falkon/qeth_connector/provider.js`; `config.js` flips it into
  WebSocket push mode (no polling, no direct-fetch fallback). A test gate and
  `build.py` enforce the mirror — fix the provider in the Falkon copy and
  re-copy it here.
- The background holds one WebSocket to `ws://127.0.0.1:1248`. An extension
  context can open insecure loopback WS (whitelisted in the manifest CSP
  `connect-src`); a page cannot (mixed content).
- Each frame gets its own `chrome.runtime` Port, so responses/pushes never
  cross frames and page ids can't collide. The background remaps ids, stamps
  the dapp origin as `__frameOrigin` from the unforgeable `port.sender`, and
  demultiplexes `eth_subscription` pushes back to the subscribing frame.
- Per-origin chain scoping (a dapp's `wallet_switchEthereumChain` doesn't drag
  other tabs) relies on the server change in the `rpc: scope WS event
  subscriptions per-subscription origin` commit.

## Build

```sh
python build.py                # → ../chrome/qeth-<version>-chrome.zip    (Chrome)
                               #   out/qeth-<version>-firefox.zip         (Firefox)
python build.py sync           # stamp the app version into the source files
python build.py submit         # build, then submit Firefox to AMO (see below)
```

**Two packages, one codebase.** Chrome MV3 uses a service-worker background;
Firefox MV3 has no service-worker support and needs the event-page form
(`background.scripts`). Declaring both keys in one manifest makes Chrome log
`'background.scripts' requires manifest version of 2 or lower`, so the source
`manifest.json` is Chrome-clean (service worker only — the unpacked dev target)
and `build.py` generates the Firefox variant. Chrome loads unpacked during
development and needs no signing.

## Install

**Chrome-family:** `chrome://extensions` → enable Developer mode → **Load
unpacked** → select this directory. (Or load the built zip.)

**Firefox (temporary):** build first (`python build.py`), then
`about:debugging#/runtime/this-firefox` → **Load Temporary Add-on** → pick the
`out/qeth-webext-<version>-firefox.zip` (the source `manifest.json` is
Chrome-flavoured, so don't load this directory directly in Firefox). Gone on
restart — for a permanent install use the
[AMO listing](https://addons.mozilla.org/firefox/addon/qeth/).

**Firefox host access:** host permissions are user-grantable on Firefox, and
without them the content scripts never inject (dapps won't see qeth). Grant via
the extension's toolbar popup (**Grant site access**) or `about:addons` →
qeth → Permissions. Chrome grants them at install.

## Firefox signing

Release Firefox refuses unsigned extensions except as temporary add-ons. qeth's
signed build is the AMO **listing** (`wallet@qeth.eth`, slug `qeth`).
`python build.py submit` uploads `out/qeth-<version>-firefox.zip` to it as a new
**listed** version, and AMO signs it on approval and updates installed copies.
It needs API credentials from
<https://addons.mozilla.org/developers/addon/api/key/> exported as
`QETH_AMO_JWT_ISSUER` / `QETH_AMO_JWT_SECRET` (read only from the environment —
never stored in the repo); without them it just builds the zip, to upload by
hand in the Developer Hub as an "On this site" version. `--release-notes` and
`--reviewer-notes` take this version's notes as plain-text files. See
`../README.md`, *Releasing / publishing*.

The `gecko.id` in the manifest (`wallet@qeth.eth`) binds every signed version to
the add-on on the AMO account and must stay stable — it's an identifier, not a
real address, so it needn't be a domain you own (AMO validates only its shape).
Changing it once users have installed breaks their auto-updates, and two ids
mean two add-ons that install side by side: `extensions/firefox/README.md`
covers the retired self-distributed build signed under `firefox@qeth.eth`.

Chrome Web Store publishing was a one-time \$5 registration; the store signs and
hosts the same zip. qeth is live at
<https://chromewebstore.google.com/detail/qeth/epgcgaelolincjdknocjebnenahjhoop>
— upload a new zip in the [dashboard](https://chrome.google.com/webstore/devconsole/)
to update it.

## Icons

Rendered from `icons/qeth-icon.svg` (shared with the Falkon connector):

```sh
for n in 16 32 48 128; do
  rsvg-convert -w $n -h $n icons/qeth-icon.svg -o icons/icon${n}.png
  magick icons/icon${n}.png -modulate 100,18 -alpha on \
    -channel A -evaluate multiply 0.85 +channel icons/icon${n}-off.png
done
```

The `-off` (dimmed) set is the toolbar icon when qeth is unreachable; the
full-colour set shows once connected.

## Automated coverage

`tests/test_webext_browser.py` (opt-in, `-m browser`) loads this extension into
headless **Chromium and Firefox** via Selenium against a stub `RpcServer`, and
covers a chunk of the matrix below automatically: EIP-6963 announce, injected
`window.ethereum`/`isMetaMask`, `eth_chainId`/`eth_accounts`/`eth_requestAccounts`
round-trip, `wallet_switchEthereumChain` → `chainChanged` push, the signer-absent
`-32601`, Safe-App-style **iframe inertness**, and disconnect→reconnect. Run it:

```sh
uv sync --group webext      # selenium is in the opt-in `webext` group, not `dev`
uv run pytest -m browser -v
```

Drivers (`chromedriver`/`geckodriver`) must be on `PATH`, and port **1248** free
(stop qeth first — the harness self-skips if it's held). The rows below that stay
**manual**: real dapps, popup/toolbar-icon visuals, `personal_sign`/`eth_sendTransaction`
approvals (need the running app + its sign dialog), service-worker force-eviction,
the Firefox event-page idle→alarm reconnect timing, and store-packaged installs.

## Manual test matrix

Run on **Chromium and Firefox**, with qeth running, unless noted:

- Toolbar icon is grey when qeth is stopped, turns colour once it starts;
  the popup shows chain + account (or the "start qeth" hint).
- A dapp's EIP-6963 picker lists **qeth** (icon, `org.qeth`); the legacy
  injected path (`window.ethereum` / `isMetaMask`) also connects.
- `personal_sign` and `eth_sendTransaction` round-trip, including a prompt held
  open > 60 s (the background must survive; on Firefox the relay's in-flight
  ping keeps the event page alive).
- A dapp `wallet_switchEthereumChain` updates **only that origin** — a second
  tab on another origin is unaffected.
- Flipping the chain in the qeth UI reaches only dapps that haven't pinned
  their own chain; an account switch pushes `accountsChanged` with no reload.
- A Safe App iframe (`app.safe.global`) stays **inert** — the multisig shows,
  not the signer EOA.
- Stop qeth → dapps see `disconnect`, icon greys, in-flight calls reject
  (4900); restart → auto-reconnect ≤ 5 s, `connect` re-emitted, chain/account
  refreshed.
- Force-stop the service worker (`chrome://serviceworker-internals`), then flip
  the chain in qeth → the dapp still updates after the worker revives.
- Two cross-origin dapp iframes in one tab → no response bleed between them.
- Firefox: revoke then re-grant site access via the popup; and after the event
  page idles > ~1 min, a qeth chain flip arrives after the keepalive-alarm
  reconnect (expect ~1 min latency there).
