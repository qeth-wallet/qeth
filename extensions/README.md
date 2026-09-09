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
              about:addons → Install Add-on From File…). Holds only a
              README while no signed, unblocked build exists — see
              firefox/README.md
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

The extension version is stamped from the app version, so a release is: bump
`qeth/__init__.py` `__version__`, then regenerate + republish these packages.
Building them is **separate** from `scripts/release.sh` (which builds the desktop
rpm/deb/flatpak/AppImage assets) — the extensions go to AMO / the Chrome Web
Store. Their committed packages are then attached to the GitHub release
(`gh release upload`), since Firefox self-distribution has no store link.

A published package can **trail** the app version, and that's expected: an AMO
review can sit for weeks, and only a Mozilla-signed `.xpi` installs in release
Firefox. Ship the newest build that is both **signed and not blocked**, and
leave it in place until a better one exists. Two ways a candidate fails:

- **Unsigned** — an AMO upload is not a signed package. Check for
  `META-INF/mozilla*` in the zip (`build.py`'s `_is_signed`); AMO's download URL
  also ends `.zip` until signing renames it `.xpi`. An unsigned build only
  side-loads via `about:debugging` and disappears on restart.
- **Blocked** — a signed version can still be soft-blocked, which makes Firefox
  disable it on install. Check before shipping, and note the endpoint's
  **`?guid=…` query form answers `Not found` even for a blocked add-on** — use
  the path form:

  ```sh
  curl -s https://addons.mozilla.org/api/v5/blocklist/block/wallet@qeth.eth/
  ```

  It is version-scoped (`is_all_versions: false`), so a fresh version clears a
  block that covers the old ones. `wallet@qeth.eth` carries block `1237064`
  over `0.20.0`-`0.22.1`; `firefox@qeth.eth` carries none.

When nothing qualifies, ship no `.xpi` at all rather than a dead one, and say so
in the README + release notes.

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

   Note that `sign` rebuilds the Chrome zip too — it runs the same
   `_replace_dist(chrome)` as a bare `build`, so it **deletes the committed
   Chrome package** before uploading anything to AMO. To sign without touching
   it (e.g. re-signing a build whose Chrome zip is already published), drive
   `build(out_dir, "firefox")` + `sign(zip, out_dir)` directly with scratch
   dirs instead of going through `main()`.

2. **Firefox** — the `sign` step uploads to the **unlisted** channel of the
   add-on named by the source manifest's `browser_specific_settings.gecko.id`
   (currently `wallet@qeth.eth`), so there is nothing to pass. Unlisted uploads
   are normally auto-signed within minutes, in which case `sign` downloads the
   `.xpi` and you're done: distribute it as a file — attach it to the GitHub
   release — and users install via `about:addons → Install Add-on From File…`.

   Since the 2026-07 review episode this account's uploads have instead been
   routed to **manual review**, so `sign` can time out on its 5-minute poll with
   the version left at `file.status: unreviewed`. That is not a failure: the
   version is uploaded and queued. Don't re-upload (the version string is
   consumed either way) — poll for the signature and fetch it when it lands:

   ```sh
   # status; url flips .zip → .xpi once signed
   GET /api/v5/addons/addon/wallet@qeth.eth/versions/?filter=all_with_unlisted
   ```

   Unlisted versions are hidden from the default listing and from
   `current_version`, hence `?filter=all_with_unlisted`.

   A **listed** (public) submission is a different flow and `sign` cannot do it
   — it hardcodes `{"channel": "unlisted"}`, and AMO's write API throttles hard
   enough that the version upload is worth doing in the dashboard anyway. Pick
   "On this site" on the submission wizard's first screen ("On your own" is
   unlisted; a version string is consumed by whichever channel gets it first).
   A listed add-on must satisfy the
   [data collection policy](https://extensionworkshop.com/documentation/publish/add-on-policies/#data-collection-and-transmission-disclosure-and-control):
   the manifest declares `data_collection_permissions` and needs
   `strict_min_version` ≥ `140.0` for Firefox's built-in consent prompt. See
   `firefox/README.md` for what qeth declares and why `none` would be wrong.

3. **Chrome** — Google signs at upload; there is no local signing (self-hosted
   `.crx` is blocked for normal users). Upload `chrome/qeth-<v>-chrome.zip` to
   the [CWS Developer Dashboard](https://chrome.google.com/webstore/devconsole/)
   ($5 one-time registration) and publish **Unlisted** (installable by direct
   link, not shown in search — the analog of the Firefox unlisted flow). The
   dashboard version must match `__version__`.

4. **Falkon** — a source plugin, no packaged file; users symlink or copy
   `falkon/qeth_connector` into `~/.config/falkon/plugins/`.
