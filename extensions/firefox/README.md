# Firefox package

qeth is listed on AMO at
[addons.mozilla.org/firefox/addon/qeth](https://addons.mozilla.org/firefox/addon/qeth/),
which installs the Mozilla-signed build and keeps it updated. That listing is the
Firefox distribution, so this directory holds no `.xpi`. To build and load one
yourself, see the main README's *Browser extension* section.

## Moving off the self-distributed `.xpi`

From 2026-09-09 to 2026-10-28, while the listing was blocked or behind, qeth
shipped a signed `.xpi` here instead (0.23.1, then 0.25.0). It was signed under
a different add-on id:

| id | what it is |
| --- | --- |
| `wallet@qeth.eth` | the AMO **listing** (slug `qeth`) and the source manifest's id — the one to install |
| `firefox@qeth.eth` | the retired self-distributed `.xpi`, which never updates itself |

**Firefox treats them as two unrelated add-ons.** Installing both leaves two
copies injecting two providers into every page, so anyone who installed the
file should remove it in *about:addons* and install from AMO. Nothing is lost:
the extension keeps no state of its own; accounts and per-site permissions
live in the qeth app. (Firefox's host-access grant is per add-on, though, so
the new copy may ask for it again — *Grant site access* in its toolbar popup.)

Signing follows the source manifest's `gecko.id` (`build.py sign` reads it), so
nothing builds under `firefox@qeth.eth` any more; `tests/test_webext.py` refuses
a committed `.xpi` signed under any id but the manifest's.

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

The listing went public with 0.23.3 on 2026-10-22, and 0.25.0 (the first with
the Tron provider) was approved on 2026-10-28.

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
