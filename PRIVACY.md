# qeth Browser Extension — Privacy Policy

_Last updated: 2026-07-24_

The qeth browser extension ("the extension") is a connector between web pages
(decentralized applications) and the qeth desktop wallet running on the user's
own computer. This policy describes how the extension handles data.

## Summary

The extension collects no personal data, contains no analytics or tracking, and
sends nothing to the developer or any third party. It communicates only with the
qeth desktop wallet on the user's own machine.

## What the extension does

The extension injects an Ethereum provider (EIP-1193) into web pages so that
decentralized applications can detect the wallet and request the user's Ethereum
account address and transaction approvals. It relays these JSON-RPC requests,
over a WebSocket, to the qeth desktop application listening locally at
`127.0.0.1:1248`. The desktop application — not the extension — holds the user's
keys and asks the user to approve or reject each request.

## Data collection

The extension does not collect, store, or transmit any personal or sensitive
user data. Specifically:

- It has no analytics, telemetry, or tracking of any kind.
- It sends no data to the developer or to any remote server. All network traffic
  is confined to the local wallet at `127.0.0.1:1248` (loopback), enforced by the
  extension's Content Security Policy.
- It does not read, collect, or transmit the content of web pages. The content
  script only bridges provider messages between the page and the local wallet.
- Private keys and secrets never pass through the extension; they remain in the
  qeth desktop application.

## Permissions

- **Host access (`http`/`https`):** required to inject the provider into web
  pages so dapps on any site can connect to the wallet. Used only to bridge
  provider messages, not to read or exfiltrate page content.
- **`alarms`:** used only to keep the background service worker alive with a
  periodic, local, side-effect-free request to the local wallet.

## Data sharing

None. The extension shares no data with the developer or any third party,
because it collects none.

## Changes

Any changes to this policy will be published at this URL.

## Contact

Questions: michwill@yieldbasis.com — or open an issue at
<https://github.com/qeth-wallet/qeth>.
