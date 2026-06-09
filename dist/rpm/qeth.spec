Name:           qeth
Version:        0.11.0
Release:        1%{?dist}
Summary:        Qt Ethereum wallet with Ledger support and a Frame-compatible JSON-RPC server

License:        GPL-3.0-or-later
URL:            https://github.com/michwill/qeth
Source0:        %{name}-%{version}.tar.gz

# Vendored deps include compiled extensions (pycryptodome) -> arch-specific.
ExclusiveArch:  x86_64

# The vendored deps are pre-built manylinux wheels — no useful debuginfo, and
# eu-strip/find-debuginfo choke on their ELF ("illformed file", no build-id).
# Nothing here is compiled from source, so skip the debug subpackage + strip.
%global debug_package %{nil}
%global __strip /bin/true

BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  gcc
# System deps present at build time so the --system-site-packages venv treats
# them as satisfied and pip vendors ONLY what Fedora lacks / pins differently.
BuildRequires:  python3-pyside6
BuildRequires:  python3-pydantic
BuildRequires:  python3-aiohttp
BuildRequires:  python3-requests
BuildRequires:  python3-cytoolz
BuildRequires:  python3-pyserial
BuildRequires:  python3-pillow
BuildRequires:  python3-eth-hash
BuildRequires:  python3-eth-account
BuildRequires:  python3-eth-abi
BuildRequires:  python3-eth-keys
BuildRequires:  python3-hexbytes
BuildRequires:  python3-ckzg

# Runtime: pull the same stack from the distro (system Qt -> native theming).
Requires:       python3
Requires:       python3-pyside6
Requires:       python3-pydantic
Requires:       python3-aiohttp
Requires:       python3-requests
Requires:       python3-cytoolz
Requires:       python3-pyserial
Requires:       python3-pillow
Requires:       python3-eth-hash
Requires:       python3-eth-account
Requires:       python3-eth-abi
Requires:       python3-eth-keys
Requires:       python3-hexbytes
Requires:       python3-ckzg
# qt6ct bridges the user's Qt theme to the app; not strictly required.
Recommends:     qt6ct

%description
qeth is a Qt (PySide6) Ethereum wallet for the Linux desktop with Ledger
support and a Frame-compatible JSON-RPC server. This package uses the system
PySide6 and most of the eth stack from Fedora, vendoring only the deps Fedora
does not ship (web3, ledgereth, …) or whose version web3 pins differently
(eth-utils, rlp) into a private directory.

%prep
%autosetup -n %{name}-%{version}

%build
# Pure Python; the runtime tree is assembled in %%install.

%install
# Build a venv that SEES the system (BuildRequires) packages; installing qeth
# into it pulls only the deps Fedora doesn't satisfy. Its venv-local
# site-packages is therefore exactly the private vendor set + qeth itself.
python3 -m venv --system-site-packages %{_builddir}/qeth-venv
%{_builddir}/qeth-venv/bin/python -m pip install --no-warn-script-location --no-compile .

VENDOR=%{buildroot}%{_prefix}/lib/%{name}/vendor
install -d "$VENDOR"
cp -a %{_builddir}/qeth-venv/lib/python*/site-packages/. "$VENDOR/"
# Drop venv/pip bookkeeping — keep qeth + the runtime deps only.
rm -rf "$VENDOR"/pip "$VENDOR"/pip-*.dist-info \
       "$VENDOR"/setuptools "$VENDOR"/setuptools-*.dist-info \
       "$VENDOR"/_distutils_hack "$VENDOR"/pkg_resources \
       "$VENDOR"/*.pth
find "$VENDOR" -name '__pycache__' -type d -prune -exec rm -rf {} +

# Launcher (sets PYTHONPATH to the vendor dir, runs python -m qeth).
install -Dm0755 dist/rpm/qeth.launcher %{buildroot}%{_bindir}/%{name}

# Desktop entry (Exec=qeth, StartupWMClass=qeth) + icon — native menu + dock.
install -Dm0644 dist/flatpak/io.github.michwill.qeth.desktop \
        %{buildroot}%{_datadir}/applications/io.github.michwill.qeth.desktop
install -Dm0644 qeth/assets/logos/qeth-icon-rounded.svg \
        %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/io.github.michwill.qeth.svg

%files
%license LICENSE
%{_bindir}/%{name}
%{_prefix}/lib/%{name}/
%{_datadir}/applications/io.github.michwill.qeth.desktop
%{_datadir}/icons/hicolor/scalable/apps/io.github.michwill.qeth.svg

%changelog
* Tue Jun 09 2026 Michael Egorov <michwill@yieldbasis.com> - 0.11.0-1
- Initial Fedora package: system PySide6 + eth stack, vendor web3/ledgereth/etc.
