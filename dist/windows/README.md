# Windows build

Two downloads from one PyInstaller **onedir** folder (`qeth-windows/` with
`qeth.exe` and its `_internal/` runtime):

- `qeth-<version>-setup.exe` — the **installer** (Inno Setup, `qeth.iss`).
  Per-user by default into `%LOCALAPPDATA%\Programs\qeth`, no admin prompt
  (machine-wide via its dialog or `/ALLUSERS`); Start-menu entry, optional
  desktop icon, uninstaller in Settings → Apps, in-place upgrades (the fixed
  `AppId`). Uninstalling never touches `~\.qeth`.
- `qeth-<version>-windows-x64.zip` — the same folder, **portable**.

Not onefile: that unpacks the whole runtime to `%TEMP%` on every launch,
which is slow and a classic antivirus false positive.

On Windows (Git Bash), from the repository root:

```sh
./dist/windows/build.sh
ISCC='C:\path\to\ISCC.exe' ./dist/windows/installer.sh   # → out/qeth-<v>-setup.exe
```

On Linux, the same recipe runs under Wine (see `scripts/wine.sh`):

```sh
scripts/wine.sh setup              # once: the Wine prefix, Windows uv/Python, Inno Setup
scripts/wine.sh build              # → dist/windows/out/qeth-windows
scripts/wine.sh installer          # → dist/windows/out/qeth-<v>-setup.exe
scripts/wine.sh smoke              # run the bundle the way CI does, isolated
scripts/wine.sh smoke installer    # install it, run the installed copy, uninstall
```

The build uses an isolated environment with the locked `windows-build`,
`bundled`, `simulate`, `qr` and `trezor` dependency sets — the same extras as
the macOS app, plus Trezor. Output goes under `dist/windows/out/`.

`smoke` runs the built `qeth.exe` on a clean Wine prefix (no Python, like a
fresh Windows machine) inside a private network namespace — its own loopback,
so port 1248 is free even next to a running qeth — on a hidden Xvfb display.
It passes when the JSON-RPC server answers, the main window is up, and the
process is still running 15 s later; `smoke installer` also requires the
Start-menu entry and uninstall registration, and that uninstalling removes
the whole folder. CI (`.github/workflows/windows.yml`, `bundle` job) does the
same on a real Windows runner (`smoke.sh`) and uploads both downloads.

## Not there yet

- **No "verify" variant.** Helios publishes no Windows release, so the build
  has no bundled light client; transaction previews run unverified.
- **Unsigned.** Windows SmartScreen warns when the installer (or the zip's
  `qeth.exe`) is first run ("More info → Run anyway"). CA code-signing keys
  must live on an HSM since 2023, so signing means a cloud service (Azure
  Trusted Signing, SignPath for open source) — not a `.pfx` in a repository
  secret.
