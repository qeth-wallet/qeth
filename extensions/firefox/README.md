# Firefox package

**No `.xpi` is shipped right now.** This directory holds the committed,
Mozilla-signed Firefox distributable when there is one — self-distributed
(unlisted), installed via *about:addons → gear → Install Add-on From File…*.

Release Firefox installs only signed extensions, and every build has to be
signed by Mozilla. The current build (`0.23.1`) is uploaded to the unlisted
channel of `wallet@qeth.eth` and is **awaiting AMO review** — the account's
uploads are routed to manual review rather than auto-signed, so the signature
is not instant. The signed `.xpi` gets committed here, and attached to the
GitHub release, as soon as it is issued.

The previously shipped `qeth-0.22.0.xpi` was **removed**, not just superseded:
Mozilla soft-blocked `0.20.0`, `0.21.0`, `0.22.0` and `0.22.1` (block record
`1237064`, residue of a reversed false-positive ban), so Firefox disables those
versions on install. Distributing one would hand users a dead extension. It
remains in git history if it is ever needed.

Check the block list — it is version-scoped (`is_all_versions: false`), so
versions above `0.22.1` are unaffected:

```sh
curl -s https://addons.mozilla.org/api/v5/blocklist/block/wallet@qeth.eth/
```

Note the `?guid=…` query form of that endpoint returns `Not found` even for a
genuinely blocked add-on — use the path form above.

In the meantime, Chrome users have the
[Chrome Web Store listing](https://chromewebstore.google.com/detail/qeth/epgcgaelolincjdknocjebnenahjhoop),
and Firefox developers can load `extensions/webext/` as a temporary add-on via
`about:debugging` (gone on restart).
