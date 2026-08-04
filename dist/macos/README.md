# macOS application

macOS grants camera access to application bundles, not to qeth when it is run
through an unmodified Python interpreter. The bundle built here carries qeth's
stable application identity and `NSCameraUsageDescription`, allowing Qt to ask
for camera access when the QR scanner first opens.

From the repository root:

```sh
./dist/macos/build.sh
open dist/macos/out/qeth-macos.app
```

The PyInstaller build uses an isolated environment with uv's selected project
interpreter and the locked `macos-build`, `bundled`, and `qr` dependency sets.
It writes all output under `dist/macos/out/`. The result is ad-hoc signed for
local use, not Developer ID-signed or notarized for distribution.

## Signing and notarization (optional, CI)

The bundle CI builds is **ad-hoc signed**: usable, but macOS quarantines it on
download, so users must clear `com.apple.quarantine` by hand. Configuring five
repository secrets switches the job to Developer ID + notarization, after which
a download just opens. With no secrets set every signing step is skipped and
the output is unchanged, so this costs nothing until you enrol.

| secret | what it is |
|---|---|
| `MACOS_CERT_P12` | Developer ID Application cert + key, as a `.p12`, base64 |
| `MACOS_CERT_PASSWORD` | that `.p12`'s password |
| `APPLE_API_KEY_ID` | App Store Connect API key id |
| `APPLE_API_ISSUER_ID` | its issuer id |
| `APPLE_API_KEY_P8` | the `.p8` private key, base64 |

Enrolment is $99/yr and an **individual** account suffices — the organization
requirement in guideline 3.1.5(b) applies to App Store submissions, which these
are not. The certificate does not need a Mac: a CSR is a plain PKCS#10 request,
so `openssl req -new -newkey rsa:2048 -nodes -keyout devid.key -out devid.csr`
produces one to upload at developer.apple.com. Base64 the resulting `.p12` and
`.p8` with `base64 -w0` before pasting them into the secrets.

Nothing here needs a Mac to *sign*, either: `rcodesign` (the `apple-codesign`
Rust crate) signs, notarizes and staples Mach-O binaries, `.app` bundles and
`.dmg`s from Linux. CI uses Apple's own `codesign`/`notarytool` simply because
it already runs on a macOS runner and that path is better documented.

**Expect the first notarization to fail.** The hardened runtime is mandatory for
notarization and rejects PyInstaller bundles until the exceptions in
`qeth.entitlements` are present — they already are, taken from Electrum, which
has the same stack. If it still fails, `xcrun notarytool log <submission-id>`
names the offending binary. The usual remaining cause is `--deep` signing not
reaching something nested; Electrum's `contrib/osx/sign_osx.sh` walks and signs
each item individually and is the reference for that.
