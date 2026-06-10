# qeth

A Qt (PySide6) Ethereum wallet for the Linux desktop — hardware-wallet (Ledger)
support and a Frame-compatible JSON-RPC server on `127.0.0.1:1248`, so the Frame
browser extension and dapps connect unchanged.

## Install

Grab the package for your distro from the
**[latest release](https://github.com/michwill/qeth/releases/latest)**. There
are two kinds:

- **Native — `.rpm` / `.deb`.** Link the **system Qt**, so your desktop's Qt
  theme (qt6ct / Kvantum / KDE Breeze) applies. Preferred where available.
- **Portable — Flatpak / AppImage.** Bundle their own Qt; run on any distro.

Examples below use `0.11.3` — substitute the version you downloaded.

### Fedora (and RHEL / Alma / Rocky family)

```sh
sudo dnf install ./qeth-0.11.3-1.fc44.x86_64.rpm
```

Uses the distro's PySide6 + most of the eth stack; vendors only what Fedora
doesn't ship. Built on Fedora 44, works on current Fedora.

### Debian / Ubuntu 24.04 / Linux Mint 22

This package runs on **Python 3.11 from the deadsnakes PPA**. Ubuntu 24.04 /
Mint 22 ship Python 3.12, for which no compatible PySide6 6.4 exists, so the
package builds PySide6 from source against the system Qt 6.4 and runs it under
python3.11 ([why](dist/deb/README.md)). One-time PPA setup, then install:

```sh
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y ./qeth_0.11.3_amd64.deb
```

`apt` pulls `python3.11` from deadsnakes and the system Qt 6 libraries
automatically. (A future Mint/Ubuntu LTS that ships PySide6 won't need the PPA.)

### Any distro — Flatpak

```sh
flatpak install --user ./qeth-0.11.3.flatpak
flatpak run io.github.michwill.qeth
```

Needs `flatpak` with [Flathub](https://flatpak.org/setup/) configured (it pulls
the KDE 6.10 runtime). The sandbox can't read your qt6ct/Kvantum config, so qeth
applies its own theming tweaks instead.

### Any distro — AppImage

```sh
chmod +x qeth-0.11.3-x86_64.AppImage
./qeth-0.11.3-x86_64.AppImage
```

Self-contained — runs on any reasonably recent x86-64 Linux (glibc ≥ 2.34).

### From source

```sh
uv venv --system-site-packages      # pulls in system PySide6 for native theming
uv sync --inexact                   # installs the pinned deps from uv.lock
uv run python -m qeth
```

(Falls back to `uv pip install -e '.[bundled]'` if you have no system PySide6.)

## Notes

- Config and caches live in `~/.qeth/`.
- The native `.rpm`/`.deb` give native theming because they use the system Qt;
  the Flatpak/AppImage are portable but ship their own Qt.
- Packaging recipes: [`dist/rpm/`](dist/rpm/), [`dist/deb/`](dist/deb/),
  [`dist/flatpak/`](dist/flatpak/), [`dist/appimage/`](dist/appimage/).
