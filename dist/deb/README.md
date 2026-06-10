# qeth `.deb` (Mint 22 / Ubuntu 24.04)

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
