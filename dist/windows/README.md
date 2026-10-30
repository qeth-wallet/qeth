# Windows build

A PyInstaller **onedir** folder (`qeth-windows/` with `qeth.exe` and its
`_internal/` runtime), shipped zipped. Not onefile: that unpacks the whole
runtime to `%TEMP%` on every launch, which is slow and a classic antivirus
false positive.

On Windows (Git Bash), from the repository root:

```sh
./dist/windows/build.sh
dist/windows/out/qeth-windows/qeth.exe
```

On Linux, the same recipe runs under Wine (see `scripts/wine.sh`):

```sh
scripts/wine.sh setup    # once: the Wine prefix + Windows uv/Python
scripts/wine.sh build    # → dist/windows/out/qeth-windows
scripts/wine.sh smoke    # run it the way CI does, isolated
```

The build uses an isolated environment with the locked `windows-build`,
`bundled`, `simulate`, `qr` and `trezor` dependency sets — the same extras as
the macOS app, plus Trezor. Output goes under `dist/windows/out/`.

`smoke` runs the built `qeth.exe` on a clean Wine prefix (no Python, like a
fresh Windows machine) inside a private network namespace — its own loopback,
so port 1248 is free even next to a running qeth — on a hidden Xvfb display.
It passes when the JSON-RPC server answers, the main window is up, and the
process is still running 15 s later. CI (`.github/workflows/windows.yml`,
`bundle` job) does the same on a real Windows runner and uploads the zip.

## Not there yet

- **No "verify" variant.** Helios publishes no Windows release, so the build
  has no bundled light client; transaction previews run unverified.
- **Unsigned.** Windows SmartScreen warns on first launch ("More info → Run
  anyway"). CA code-signing keys must live on an HSM since 2023, so signing
  means a cloud service (Azure Trusted Signing, SignPath for open source) —
  not a `.pfx` in a repository secret.
- **No installer** — the zip only, for now.
