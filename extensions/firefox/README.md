# Firefox package

`qeth-0.23.1.xpi` is the Mozilla-signed, self-distributed build — install it via
*about:addons → gear → Install Add-on From File…*. Release Firefox installs only
signed extensions, so this file, not a source build, is what works today.

Verify it is genuinely signed before trusting a copy:

```sh
unzip -l qeth-0.23.1.xpi | grep META-INF
# META-INF/cose.sig, META-INF/mozilla.rsa, …
```

An `.xpi` without those is an unsigned zip that only side-loads through
`about:debugging` and disappears on restart.

## Two add-on ids are in play — install only one

| id | what it is |
| --- | --- |
| `firefox@qeth.eth` | the signed `.xpi` in this directory — self-distributed, works now |
| `wallet@qeth.eth` | the AMO **listing** (slug `qeth`), where the public submission goes |

The source manifest names `wallet@qeth.eth`, because `build.py sign` reads the
guid from it and that is the add-on carrying the listing metadata, screenshots
and the `qeth` slug. The committed `.xpi` predates that and is signed under
`firefox@qeth.eth`, which is the id Mozilla was willing to sign while the
listing was blocked.

**Firefox treats them as two unrelated add-ons.** Installing both leaves two
copies injecting two providers into every page, so once the listing is approved,
install from AMO and remove the self-distributed copy — and this directory's
`.xpi` should be dropped rather than kept alongside it.

## Why the listing was rejected, and what changed

Mozilla reviewed `0.22.3` on 2026-09-07 and rejected it under the
[data collection and transmission policy](https://extensionworkshop.com/documentation/publish/add-on-policies/#data-collection-and-transmission-disclosure-and-control)
— not for anything the extension sends to a server, but for sending anything at
all without asking. Two findings:

- `personallyIdentifyingInfo: provider.js:427` — the EIP-6963 provider `uuid`,
  a `crypto.randomUUID()` regenerated per page load that never leaves the page.
- `browsingActivity: background.js:73` — `wsSend`, which puts the envelope on
  the socket. The envelope carries `__frameOrigin`, the dapp origin the wallet
  needs to scope permissions per site.

Both go to `ws://127.0.0.1:1248` and nowhere else; the manifest CSP permits no
other destination. But the policy counts data *"handled outside of the add-on or
the local browser"*, and the qeth desktop app is a separate process — so
declaring `none` would be a false statement, and a false declaration is itself a
violation. The manifest therefore declares what the reviewer classified:

```json
"data_collection_permissions": { "required": ["browsingActivity", "personallyIdentifyingInfo"] }
```

`strict_min_version` moved `128.0` → `140.0` because Firefox's built-in consent
prompt starts at 140; below that an add-on has to ship its own consent screen.
That costs nothing now — ESR 153 is current and ESR 140 is already end-of-life.

## Block-list residue

`wallet@qeth.eth` carries block `1237064` over `0.20.0`–`0.22.1`, residue of a
reversed false-positive ban. It is version-scoped (`is_all_versions: false`), so
it never reaches a new version. Check with the **path** form — the `?guid=…`
query form answers `Not found` even for a genuinely blocked add-on:

```sh
curl -s https://addons.mozilla.org/api/v5/blocklist/block/wallet@qeth.eth/
```

Chrome users install from the
[Chrome Web Store](https://chromewebstore.google.com/detail/qeth/epgcgaelolincjdknocjebnenahjhoop).
