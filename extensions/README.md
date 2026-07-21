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
