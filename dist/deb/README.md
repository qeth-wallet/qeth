# qeth `.deb`

Two **natively-themed** Debian-family packages — both link the **system Qt** so
the user's qt6ct/Kvantum/Breeze theme applies, and both install a real menu entry
+ dock icon. Which one you want depends on the base distro:

| Build | Target | Script | Filename |
|---|---|---|---|
| **Mint / Ubuntu** | Mint 22, Ubuntu 24.04 (Qt 6.4, py3.12) | `build-deb.sh` | `qeth_<v>_amd64.deb` |
| **LMDE / Debian 13** | LMDE 7 "Gigi", Debian 13 "Trixie" (Qt 6.8, py3.13) | `build-deb-debian.sh` | `qeth_<v>_debian13_amd64.deb` |

The two are **not interchangeable**: the Mint deb depends on deadsnakes
`python3.11`, the LMDE deb on Debian 13's native `python3-pyside6.*` — install the
one matching your distro. The rest of this section is the Mint/Ubuntu build; the
LMDE build is documented at the bottom.

## Mint 22 / Ubuntu 24.04

A **natively-themed** Debian/Ubuntu package: unlike the flatpak/AppImage it links
the **system Qt 6.4**, so the user's qt6ct/Kvantum/Breeze theme applies, and it
installs a real menu entry + dock icon.

## Why it builds PySide6 from source

Fedora ships `python3-pyside6` + most of the eth stack, so the [RPM](../rpm/)
just `Requires:` them. Ubuntu 24.04 / Mint 22 ship **neither**:

- **No PySide6 that fits the distro's combo.** apt has Qt 6.4 + Python 3.12, but
  the stock PySide6 6.4 wheel is `Requires-Python <3.12`, and py3.12 support
  starts at PySide6 6.6 (which needs Qt 6.6). Nothing lines up — which is why
  Debian/Ubuntu skipped PySide6 for this LTS.
- **No current eth stack** (apt's pydantic is 1.x, web3 7.x needs 2.x; etc.).

So this build runs under the **deadsnakes python3.11** and compiles **PySide6 6.4
from source against the system Qt 6.4** (abi3 bindings), vendoring PySide6 + the
eth stack privately under `/usr/lib/qeth/vendor`. The bindings link the system
Qt, so `Depends: libqt6*` and theming is native.

## Build

```sh
# 1. deadsnakes python3.11 + Qt6 build deps (one-time)
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt-get install -y python3.11 python3.11-venv python3.11-dev \
    qt6-base-dev qt6-base-private-dev qt6-declarative-private-dev \
    qt6-multimedia-dev \
    libclang-14-dev clang-14 cmake ninja-build dpkg-dev

# 2. build the .deb (compiles PySide6 the first time, ~15 min; reused after)
./dist/deb/build-deb.sh            # -> dist/deb/qeth_<version>_amd64.deb
```

`build-deb.sh` calls `build-pyside.sh` to produce the PySide6 venv (cached at
`/tmp/qeth-pyside-venv`, override with `QETH_PYSIDE_VENV`), then `pip install`s
qeth + deps into it and assembles the package.

> **`qt6-declarative-private-dev` matters:** without the QtQml *private* headers,
> the `libpysideqml` support lib fails to configure (`Qt::QmlPrivate` points at a
> non-existent include path) and the whole PySide6 build aborts — even though
> qeth never uses QML.

## Install / run

```sh
sudo apt install ./qeth_<version>_amd64.deb
qeth
```

`Depends: python3.11` — from **deadsnakes**, not Ubuntu's archive, so the user
needs that PPA too (a future option is to bundle the interpreter). Runtime Qt
deps (`libqt6widgets6`, …) are stock.

## LMDE 7 / Debian 13 "Trixie"

Debian 13 makes this **far simpler than the Mint build** — no from-source PySide6
compile at all. Trixie ships **PySide6 6.8 natively**, as the split
`python3-pyside6.*` module packages built for its stock `python3` (3.13). So,
exactly like the [Fedora RPM](../rpm/), the LMDE `.deb` just `Depends:` on the
system bindings and runs under the system interpreter; the bindings link the
system Qt, so theming is native.

Debian doesn't package the eth stack (web3 / eth-account / eth-abi / eth-keys /
rlp / ledgereth), a new-enough `hexbytes` (≥1), or `zxing-cpp`, and some of its
`pydantic`/`aiohttp`/`eth-utils` live only in backports — so, like the Mint deb,
the **full Python closure minus PySide6** is vendored from PyPI wheels under
`/usr/lib/qeth/vendor` (it shadows any system copy via `PYTHONPATH`). Vendoring
the whole closure keeps the package installable on any stock Debian 13 with only
the **main** repo enabled.

### Build

```sh
# one-time: system PySide6 modules + venv/packaging tools (main repo only)
sudo apt-get install -y python3 python3-venv python3-pip dpkg-dev \
    python3-pyside6.qtcore python3-pyside6.qtgui python3-pyside6.qtwidgets \
    python3-pyside6.qtnetwork python3-pyside6.qtmultimedia gstreamer1.0-plugins-good

# build the .deb (~30s — no Qt compile; just pip-vendors the eth stack)
./dist/deb/build-deb-debian.sh            # -> dist/deb/qeth_<version>_debian13_amd64.deb
```

Add `QETH_BUNDLE_HELIOS=/path/to/helios` for the `-verify` variant (bundles a
Helios light client so transaction previews are proof-verified out of the box).

### Install / run

```sh
sudo apt install ./qeth_<version>_debian13_amd64.deb
qeth
```

`Depends:` are all stock Debian 13 (`python3 (>= 3.13)`, the five
`python3-pyside6.*` modules qeth imports, `gstreamer1.0-plugins-good` for the QR
camera) — **no PPA required**, unlike the Mint build.
