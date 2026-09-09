# Firefox package

`qeth-0.23.1.xpi` is the Mozilla-signed, self-distributed Firefox build.
Release Firefox installs only signed extensions, so this is the file to use —
install it via *about:addons → gear → Install Add-on From File…*. It is not on
addons.mozilla.org: self-distribution means there is no public listing page to
link to, only this file.

Verify it is genuinely signed before trusting a copy — a signed package carries
Mozilla's signature block:

```sh
unzip -l qeth-0.23.1.xpi | grep META-INF
# META-INF/cose.sig, META-INF/mozilla.rsa, …
```

An `.xpi` without those is an unsigned zip that only loads through
`about:debugging` as a temporary add-on, and disappears on restart.

## Why the add-on id is `firefox@qeth.eth`

The extension was originally signed under `wallet@qeth.eth`. That id is dead
for new builds: Mozilla rejected its `0.22.2`, `0.22.3` and `0.23.1` uploads on
2026-09-07 (they remain unsigned), and its older signed versions — `0.20.0`,
`0.21.0`, `0.22.0`, `0.22.1` — are soft-blocked under block record `1237064`,
residue of a reversed false-positive ban, so Firefox disables them on install.
`firefox@qeth.eth` is the id Mozilla actually signs, and it carries no block
record. The source manifest's `browser_specific_settings.gecko.id` matches it,
so `build.py sign` targets the right add-on.

The id change costs nothing here: no working install of the old id exists, so
there are no auto-updates to break. Check either id's block status with the
**path** form of the endpoint — the `?guid=…` query form returns `Not found`
even for a genuinely blocked add-on:

```sh
curl -s https://addons.mozilla.org/api/v5/blocklist/block/firefox@qeth.eth/
# → {"detail":"Not found."}   (no block — the honest answer for this id)
curl -s https://addons.mozilla.org/api/v5/blocklist/block/wallet@qeth.eth/
# → {"id":1237064,"soft_blocked":["0.20.0","0.21.0","0.22.0","0.22.1"],…}
```

Blocks are version-scoped when `is_all_versions` is `false`, so the record
above never reaches this build.

Chrome users install from the
[Chrome Web Store](https://chromewebstore.google.com/detail/qeth/epgcgaelolincjdknocjebnenahjhoop).
