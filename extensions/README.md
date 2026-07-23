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
  firefox/    committed Firefox distributable: qeth-<version>.xpi
              (AMO-signed, unlisted / self-distribution — install via
              about:addons → Install Add-on From File…)
  falkon/     Falkon connector — a native Python plugin (its own source), same
              role inside the Falkon browser
```

The `chrome/` and `firefox/` packages are built from `webext/` — they aren't
edited by hand:

```sh
cd webext
python build.py            # → ../chrome/qeth-<version>-chrome.zip
python build.py sign       # AMO-sign → ../firefox/qeth-<version>.xpi
                           #   (needs QETH_AMO_JWT_ISSUER / _SECRET)
```

`provider.js` is shared **byte-for-byte** between `webext/` and `falkon/`; a test
gate and `build.py` enforce the mirror. See each dir's README for details.

## Releasing / publishing

The extension version always equals the app version, so a release is: bump
`qeth/__init__.py` `__version__`, then regenerate + republish these packages.
This is **separate** from `scripts/release.sh` (which builds the desktop
rpm/deb/flatpak/AppImage assets) — the extensions go to AMO / the Chrome Web
Store, not the GitHub release asset set.

1. **Rebuild + sign** (version auto-syncs from `__version__`):

   ```sh
   cd extensions/webext
   python build.py                         # → ../chrome/qeth-<v>-chrome.zip
   # AMO creds live in ~/Documents/Mozilla/keys.py (jqt_issuer / jwt_secret):
   export QETH_AMO_JWT_ISSUER=user:NNNN:NN QETH_AMO_JWT_SECRET=…
   python build.py sign                    # → ../firefox/qeth-<v>.xpi  (AMO-signed)
   ```

   Commit the refreshed `chrome/` + `firefox/` packages (build.py drops the
   prior `qeth-*` so only the current version stays tracked).

2. **Firefox** — the `sign` step already uploaded + signed the version on AMO
   (unlisted add-on `firefox@qeth.eth`). Distribute the committed
   `firefox/qeth-<v>.xpi` as a file: host it / attach it to the GitHub release /
   link it from the site. Users install via `about:addons → Install Add-on From
   File…`.

3. **Chrome** — Google signs at upload; there is no local signing (self-hosted
   `.crx` is blocked for normal users). Upload `chrome/qeth-<v>-chrome.zip` to
   the [CWS Developer Dashboard](https://chrome.google.com/webstore/devconsole/)
   ($5 one-time registration) and publish **Unlisted** (installable by direct
   link, not shown in search — the analog of the Firefox unlisted flow). The
   dashboard version must match `__version__`.

4. **Falkon** — a source plugin, no packaged file; users symlink or copy
   `falkon/qeth_connector` into `~/.config/falkon/plugins/`.
