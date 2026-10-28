# qeth browser integrations

Ways to connect a browser's dapps to a running qeth wallet (the Frame-compatible
JSON-RPC server on `127.0.0.1:1248`).

```
extensions/
  webext/     shared MV3 source for Chrome + Firefox (provider, relay,
              background, popup, icons, build.py). ONE codebase — the browsers
              differ only in manifest.json's background key, which build.py
              generates per target.
  chrome/     committed Chrome distributable: qeth-<version>-chrome.zip
              (load unpacked, or upload to the Chrome Web Store)
  firefox/    no package — Firefox installs from the AMO listing
              (addons.mozilla.org/firefox/addon/qeth). Holds a README on
              the retired self-distributed .xpi and the review history
  falkon/     Falkon connector — a native Python plugin (its own source), same
              role inside the Falkon browser
```

Both browsers' packages are built from `webext/` — they aren't edited by hand:

```sh
cd webext
python build.py            # → ../chrome/qeth-<version>-chrome.zip
                           #   + out/qeth-<version>-firefox.zip (the AMO upload)
```

`provider.js` is shared **byte-for-byte** between `webext/` and `falkon/`; a test
gate and `build.py` enforce the mirror. See each dir's README for details.

## Tron

Both connectors also serve Tron dapps, TronLink-style. Every page gets a small
stub:

- `window.tron` (TIP-1193, flagged `isTronLink`);
- a TIP-6963 announcement;
- `window.tronLink` / `window.tronWeb`, only where nothing else defined them.

A real TronWeb is loaded into a frame only once its page uses Tron. Its node
traffic and signing go through qeth; see `docs/tron.md`, "Phase 4". The
library is the **unmodified** npm dist, vendored as `webext/tronweb/` and
`falkon/qeth_connector/tronweb/`: `TronWeb.js`, its licence files, and
`SOURCE.txt` with the provenance. Store reviewers verify third-party code by
checksum. `build.py` pins its sha256 (`TRONWEB_SHA256`) and refuses to build
if the file was edited or Falkon's copy differs. To bump it:

```sh
curl -sO https://registry.npmjs.org/tronweb/-/tronweb-<v>.tgz   # check the npm integrity
tar xzf tronweb-<v>.tgz
cp package/dist/TronWeb.js package/dist/TronWeb.js.LICENSE.txt package/LICENSE \
   webext/tronweb/
cp webext/tronweb/* falkon/qeth_connector/tronweb/    # after updating SOURCE.txt
```

Then update `TRONWEB_SHA256` and `SOURCE.txt`, and run
`uv run pytest -m browser tests/test_webext_tron_browser.py`. That runs real
Chromium + Firefox beside a live qeth; it doesn't need port 1248.

Loading TronWeb into a frame uses `chrome.scripting`, so the extension
declares the **`scripting`** permission. Neither store shows an install
warning for it (the host permissions are unchanged). It is still a manifest
change for the next store review.

## Releasing / publishing

The extension version is stamped from the app version, so a release is: bump
`qeth/__init__.py` `__version__`, then regenerate + republish these packages.
Building them is **separate** from `scripts/release.sh` (which builds the desktop
rpm/deb/flatpak/AppImage assets) — the extensions go to AMO / the Chrome Web
Store, which sign them and update installed copies. The committed Chrome zip is
also attached to the GitHub release (`gh release upload`) for loading unpacked.

A store version can **trail** the app version, and that's expected: a review
can sit for weeks. The extension is a thin relay to `127.0.0.1:1248`, so an
older build keeps working with a newer app.

1. **Rebuild** (version auto-syncs from `__version__`):

   ```sh
   cd extensions/webext
   python build.py      # → ../chrome/qeth-<v>-chrome.zip + out/qeth-<v>-firefox.zip
   ```

   Commit the refreshed `chrome/` package (build.py drops the prior `qeth-*` so
   only the current version stays tracked).

2. **Firefox** — upload `out/qeth-<v>-firefox.zip` as a new version of the
   listing in the [AMO Developer Hub](https://addons.mozilla.org/developers/),
   picking **"On this site"** on the submission wizard's first screen. "On your
   own" is the unlisted channel, and a version string is consumed by whichever
   channel gets it first — an unlisted `<v>` means the listing can never have
   `<v>` (that cost 0.22.2 and 0.23.2). Once the listed version passes review,
   AMO signs it and updates installed copies itself; there is no file to commit
   or attach.

   **Don't run `python build.py sign` for a release.** It uploads to the
   **unlisted** channel of the manifest's add-on, `wallet@qeth.eth` — the
   listing — so it burns the version string in exactly that way. It is the
   self-distribution flow qeth no longer ships. (It also rebuilds the
   committed Chrome zip before touching AMO.)

   A listed add-on must satisfy the
   [data collection policy](https://extensionworkshop.com/documentation/publish/add-on-policies/#data-collection-and-transmission-disclosure-and-control):
   the manifest declares `data_collection_permissions` and needs
   `strict_min_version` ≥ `140.0` for Firefox's built-in consent prompt. See
   `firefox/README.md` for what qeth declares and why `none` would be wrong.

   Review state needs no credentials — `current_version` is what users get:

   ```sh
   curl -s https://addons.mozilla.org/api/v5/addons/addon/wallet@qeth.eth/
   ```

   With the JWT credentials, a pending version shows in
   `GET /api/v5/addons/addon/wallet@qeth.eth/versions/?filter=all_with_unlisted`
   as `file.status: unreviewed` until it is approved.

3. **Chrome** — Google signs at upload; there is no local signing (self-hosted
   `.crx` is blocked for normal users). Upload `chrome/qeth-<v>-chrome.zip` to
   the [CWS Developer Dashboard](https://chrome.google.com/webstore/devconsole/)
   ($5 one-time registration) and publish **Unlisted** (installable by direct
   link, not shown in search). The dashboard version must match `__version__`.

4. **Falkon** — a source plugin, no packaged file; users symlink or copy
   `falkon/qeth_connector` into `~/.config/falkon/plugins/`.
