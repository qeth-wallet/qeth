Name:           qeth
Version:        0.20.0
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

# The vendor dir is self-contained manylinux wheels: their .so files RPATH their
# own bundled libs (the hashed lib*-<hash>.so copies), which no system package
# provides. Exclude the whole vendor tree from automatic dependency generation —
# the wheels are self-sufficient; the real system deps are the explicit Requires.
%global __requires_exclude_from ^%{_prefix}/lib/%{name}/vendor/.*$
%global __provides_exclude_from ^%{_prefix}/lib/%{name}/vendor/.*$

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
# QR air-gapped signer decode stack (the [qr] extra): CBOR + the zxing-cpp QR
# reader. Fedora ships both, so pip vendors nothing extra for it.
BuildRequires:  python3-cbor2
BuildRequires:  python3-zxing-cpp

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
# QR air-gapped signer: the [qr] decode stack (CBOR + zxing-cpp reader) and the
# camera. python3-pyside6 already auto-Requires libQt6Multimedia.so.6, but name
# qt6-qtmultimedia explicitly so the camera backend (its bundled ffmpeg/
# gstreamer media plugins, which pull libavcodec) is a guaranteed, documented
# dependency — the QR scanner's live camera must always work.
Requires:       python3-cbor2
Requires:       python3-zxing-cpp
Requires:       qt6-qtmultimedia
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
# [simulate] ships the pure-Python py-evm fork engine: event previews on
# RPCs without eth_simulateV1, and Helios-verified previews when the user
# has a helios binary installed. [qr] adds the air-gapped QR signer decode
# stack (cbor2 + zxing-cpp) — both satisfied by the system BuildRequires, so
# nothing extra is vendored.
%{_builddir}/qeth-venv/bin/python -m pip install --no-warn-script-location --no-compile '.[simulate,qr]'

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

# "verify" variant: bundle a Helios light client for proof-verified previews
# out of the box. Build with: rpmbuild --define "bundle_helios /path/to/helios"
%if %{defined bundle_helios}
install -Dm0755 %{bundle_helios} %{buildroot}%{_prefix}/lib/%{name}/helios
%endif

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
* Sun Jul 19 2026 Michael Egorov <michwill@yieldbasis.com> - 0.20.0-1
- Air-gapped QR signer (Keystone / Keycard): account import plus transaction,
  message, and typed-data signing over an animated-QR + camera exchange.
- ENS tab management: set ETH address / text / IPFS records, add and remove
  subdomains, set manager, renew and transfer names; resolution via a mainnet
  light client with a verified badge.
- On-chain pricing for vault / LP tokens the market APIs miss (ERC-4626, Yearn
  and Yield Basis shares, Curve and Uniswap-V2 LPs), plus discovery of tokens
  from your own transaction history, with composed vault / stacked-LP icons.
- Accounts: Ctrl+F find bar, per-device subtrees with editable labels.
- Keyless contract / recipient identity on the details, send, and signing rows.
- Security: dependency bump clearing the Pillow advisories, and an allowlist so
  non-transferable governance locks stop reading as "suspected scam".

* Thu Jul 09 2026 Michael Egorov <michwill@yieldbasis.com> - 0.16.0-1
- Air-gapped QR signing: large transactions (e.g. a multicall carrying Merkle-
  Patricia proofs) that stalled some hardware wallets now transmit reliably --
  the animated QR uses fewer, denser frames (~QR version 12) and re-injects the
  self-recovering fragments each cycle so a device that locks on late still
  completes.
- Token prices: fixed a DefiLlama API change (over-long batch request URLs now
  return 404) that made every price request fail and emptied the token list.
  Prices are batched by URL length, and the panel now falls back to the last-
  known cached price during a price-source outage instead of hiding every token.

* Thu Jul 09 2026 Michael Egorov <michwill@yieldbasis.com> - 0.15.1-1
- Signing now routes to the account ROW you selected: when one address is held
  by both a Ledger and an Air-gapped (QR) record, picking the Air-gapped row and
  hitting Send no longer demands the Ledger device ("Ledger not connected").
- QR air-gapped signer: camera capture + decode pipeline retuned for reliable
  reads; already-added accounts are greyed out in the Ledger/QR/import scan lists.
- Sign dialog: the ERC-20 approve allowance is now editable, and calldata the
  ABI doesn't describe is surfaced explicitly as "additional calldata".
- Chain RPC dialog: a live reachability verdict for the URL you type or paste.
- Verified previews (Helios): reroute off a proof-incapable RPC so verified
  sims work, and respawn the sidecar when a chain's execution-RPC changes.
- Transactions: recover a TOKEN->native swap's received ETH via a node trace
  when the explorer's internal-tx feed lags.

* Sun Jul 05 2026 Michael Egorov <michwill@yieldbasis.com> - 0.15.0-1
- Air-gapped QR signer now works from every distributed package: the
  QtMultimedia camera backend (qt6-qtmultimedia → ffmpeg) and the QR decode
  stack (python3-zxing-cpp + python3-cbor2) are now hard dependencies.
- Context menus and action buttons now match across the Tokens, Accounts and
  ENS panels (Send on token/native rows, Sign/QR/Label on accounts, and
  Add-record / Add-subdomain on ENS names).
- ENS verified (checkmark) badge is reliable again: proof-emit + wallet-tree
  rebuild race fixes and a longer Helios readiness wait.

* Thu Jul 03 2026 Michael Egorov <michwill@yieldbasis.com> - 0.14.0-1
- ENS name management: the ENS panel now shows owner + manager roles in the
  tree, discovers names you own as registrant but don't manage (e.g. crv.eth)
  and subdomains of your names, and lets you set the manager, transfer, extend,
  and reassign an unwrapped subdomain's manager. Expiry is read from the
  on-chain nameExpires (not the indexer's grace-inclusive date).
- Race-condition audit and hardening pass across the transaction, ENS, store,
  and lifecycle paths: block-ordered balance writes, per-generation epochs that
  drop stale async landings, same-nonce collision fixes, and a single-instance
  guard that hands off to the running window.
- Token-balance correctness: every balance path is now block-ordered, discovery
  merges instead of replacing, Arbitrum balances stamp the L2 block (fixes a
  stuck-balance bug), and displayed balances reconcile on a timer so a silently
  dropped WebSocket log subscription can't leave a stale ERC-20 balance.

* Sat Jun 13 2026 Michael Egorov <michwill@yieldbasis.com> - 0.13.0-1
- Desktop notifications for sent/received ETH and tokens, with the token/coin
  icon and a direction badge. Delivered via the freedesktop notification
  service so the icon renders (Qt's tray drops it on some daemons); needs no
  system tray. Toggle in the tray menu.
- Verified ENS resolution: when a Helios light client is available, ENS
  name<->address resolution is proof-verified (strict, no offchain/CCIP) and
  badged "verified" in the Send and Add-account dialogs.
- Sending to an ENS name that resolves to a token contract now shows a red
  "token contract" warning naming the token, instead of a reassuring
  "verified" pill (the funds-burning destination wins over the mapping check).
- Inbound ETH now refreshes the balance live (a once-a-minute read over the
  WebSocket that also keeps the connection warm), not only on the slow sweep.

* Sat Jun 13 2026 Michael Egorov <michwill@yieldbasis.com> - 0.12.0-1
- Transaction event previews now run on a pure-Python py-evm engine (replaces
  the pyrevm fork) — so the preview works in every package, on every Python,
  not just dev checkouts.
- Verified previews via Helios: when a `helios` light client is installed, the
  preview routes through it, proof-verifying every touched state slot against
  sync-committee roots (a compromised RPC can't fake it). Shows a "verified"
  badge. Ethereum/OP/Base/Linea; auto-detected; QETH_HELIOS=0 to disable.
- A complex verified preview (e.g. a DeFi withdrawal) lands in ~1.5s via a
  prestateTracer-based prefetch; an animated spinner shows progress.
- TAC chain: token balances, history and contract identity via its Blockscout.

* Fri Jun 12 2026 Michael Egorov <michwill@yieldbasis.com> - 0.11.4-1
- Gas dialog: tiny Gnosis/L2 fees survive the fee spinboxes (precision widens
  to 1 wei) instead of quantizing to a 0 tip the chain rejects ("FeeTooLow").
- Broadcast: a first push that dies at the transport level no longer loses the
  signed tx — it is recorded as pending and re-broadcast in the background,
  regardless of which account is selected. Node rejections still surface in
  the dialog for a re-price.
- JSON-RPC proxy: fail over on provider-side errors hiding behind a parseable
  body (DRPC free-tier HTTP 408 "upgrade your tier" / 500 "please retry"),
  with a cooldown for overloaded hosts; reverts still forward verbatim.
- Wallet reads also rotate past a 200-bodied rate-limit error.

* Wed Jun 10 2026 Michael Egorov <michwill@yieldbasis.com> - 0.11.3-1
- Broadcast policy: transactions go ONLY to the user's chosen RPC, never a
  fallback — wallet sends, dapp eth_sendTransaction / raw eth_sendRawTransaction,
  and WebSocket re-broadcasts all pinned (protects private / MEV-shielded RPCs).
- Reliability: tx "dropped" verdicts need consecutive readings; dapp-added
  chain ids above qint32 no longer overflow internal signals.
- Performance: wallet switching under CPU load ~3x faster (memoized activity
  icons; skip no-op cache re-serialization).
- Storage: atomic writes everywhere (config/keystores/caches survive a crash
  mid-write); keystores written 0600 in a 0700 dir.

* Wed Jun 10 2026 Michael Egorov <michwill@yieldbasis.com> - 0.11.2-1
- Transaction list: retry a transient explorer error instead of aborting the
  load; backfill a partial cache stub on refresh; and walk the paging cursor by
  raw block so receive-heavy accounts load their full sent history rather than
  freezing at the few most-recent sends.
- Nonce/drops: trust our own broadcast record over a single node's pending view,
  so back-to-back sends get increasing nonces and confirmed txs aren't shown
  dropped.

* Tue Jun 09 2026 Michael Egorov <michwill@yieldbasis.com> - 0.11.1-1
- JSON-RPC proxy: fail over to the chain's fallback_rpcs on a transport error.
- Cache upstream DNS for 1h so short DNS outages are absorbed.
- Fix an eth_getLogs crash on a malformed/non-JSON upstream response.

* Tue Jun 09 2026 Michael Egorov <michwill@yieldbasis.com> - 0.11.0-1
- Initial Fedora package: system PySide6 + eth stack, vendor web3/ledgereth/etc.
