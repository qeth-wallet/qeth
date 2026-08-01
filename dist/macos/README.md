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
