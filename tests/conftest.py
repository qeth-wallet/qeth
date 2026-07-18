"""Test fixtures.

The qeth package defaults a bunch of on-disk paths to ``~/.qeth/...``
(config, wallet cache, tokenlists, token-metadata, risk). The
``tmp_qeth`` fixture redirects every one of them under pytest's
``tmp_path``, so a test never touches the developer's real wallet state
and the tests are hermetic / parallelizable.

UI tests use pytest-qt's ``qtbot`` fixture. They run on Qt's
"offscreen" platform plugin so they don't pop windows on the
developer's screen and work in CI / SSH sessions. The env var has to
be set before QApplication is constructed; pytest-qt creates that
during the first ``qtbot`` use, so importing this module is early
enough.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Force the offscreen platform plugin before any Qt initialization.
# Must come before pytest-qt's QApplication is created. Leaves an
# already-set value alone (someone might want to run a visible test).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Sandbox Qt's *data* dir (QStandardPaths AppData / GenericData) into a throwaway
# session dir so nothing in the suite can write to the developer's real
# ~/.local/share. tmp_qeth only rebinds qeth's own module-level CACHE_DIR
# constants — it can't touch QStandardPaths, which resolves via XDG_DATA_HOME,
# and that's usually unset -> ~/.local/share. A stray Qt/QtWebEngine write (e.g.
# a QWebEngineProfile) is how a `<stdin>/QtWebEngine/...` dir once landed there.
# FORCE-set (not setdefault) — overriding the real-home default is the point.
#
# We deliberately DON'T touch XDG_CONFIG_HOME / XDG_CACHE_HOME: Qt resolves fonts
# through them (fontconfig user config + cache), and pointing them at an empty
# dir yields a broken default font family ("QFont::fromString: Invalid
# description") that shifts every pixel-based render test. AppData is the real
# leak vector anyway; AppConfig/Cache would only ever get a namespaced
# `pytest-qt-qapp/` dir, not a cryptic one, and nothing in the suite writes them.
# Must run before Qt init (pytest-qt builds the QApplication on first qtbot use).
_xdg_sandbox = tempfile.mkdtemp(prefix="qeth-test-xdg-")
os.environ["XDG_DATA_HOME"] = os.path.join(_xdg_sandbox, "data")
atexit.register(lambda: shutil.rmtree(_xdg_sandbox, ignore_errors=True))

# The ws live watcher is on by default in the app, but tests must not open
# real ws connections (or spawn QThreads the fixtures don't manage). Disable
# it for the suite; the watcher's own tests opt back in via monkeypatch.
os.environ.setdefault("QETH_LIVE_WS", "0")

# aiohttp defaults to the c-ares (pycares) resolver whenever aiodns is
# importable — and it is here, pulled in via --system-site-packages. That
# resolver's Channel spins up a shutdown thread
# (pycares._run_safe_shutdown_loop) on teardown/GC, which segfaults flakily
# when it overlaps a Qt event loop (a later UI test spinning qtbot). Force
# aiohttp's ThreadedResolver so no pycares Channel is ever created in the suite.
import aiohttp.connector as _aiohttp_connector
import aiohttp.resolver as _aiohttp_resolver
_aiohttp_connector.DefaultResolver = _aiohttp_resolver.ThreadedResolver
_aiohttp_resolver.DefaultResolver = _aiohttp_resolver.ThreadedResolver

# Same for the Helios verified-simulation sidecar: the developer machine
# may have a real `helios` binary installed, and any simulate_logs call
# on a supported chain would otherwise SPAWN it mid-test. Helios's own
# tests opt back in / inject fakes via monkeypatch.
os.environ.setdefault("QETH_HELIOS", "0")

# Local-only test config from a gitignored `.env` at the repo root. Mainly a
# fast local archive node for the anvil fork — e.g.
# `QETH_ANVIL_FORK_RPC=http://10.0.0.5:8545` — so tests/test_live_anvil.py
# forks instantly with no public-RPC rate limits. `setdefault` so an explicit
# shell env var still wins, and it's loaded *after* the lines above so they
# keep priority (a stray QETH_LIVE_WS in .env can't enable ws in the suite).
_dotenv = Path(__file__).resolve().parent.parent / ".env"
if _dotenv.exists():
    for _line in _dotenv.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            os.environ.setdefault(_key.strip(), _val.strip())


@pytest.fixture
def tmp_qeth(tmp_path, monkeypatch) -> Path:
    """Redirect all qeth on-disk locations under ``tmp_path``."""
    import qeth.abi_cache
    import qeth.activity_cache
    import qeth.ens_app
    import qeth.hot_wallet
    import qeth.store
    import qeth.token_metadata
    import qeth.token_discovery.tokenlists
    import qeth.transactions_cache
    import qeth.wallet_cache
    import qeth.risk

    monkeypatch.setattr(qeth.store, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(qeth.store, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(qeth.wallet_cache, "CACHE_DIR", tmp_path / "wallets")
    monkeypatch.setattr(qeth.token_metadata, "CACHE_DIR", tmp_path / "token_metadata")
    monkeypatch.setattr(qeth.token_discovery.tokenlists, "CACHE_DIR",
                        tmp_path / "tokenlists")
    monkeypatch.setattr(qeth.risk, "CACHE_DIR", tmp_path / "risk")
    monkeypatch.setattr(qeth.transactions_cache, "CACHE_DIR",
                        tmp_path / "transactions")
    monkeypatch.setattr(qeth.activity_cache, "ACTIVITIES_DIR",
                        tmp_path / "activities")
    monkeypatch.setattr(qeth.abi_cache, "CACHE_DIR", tmp_path / "abi")
    monkeypatch.setattr(qeth.ens_app, "CACHE_DIR", tmp_path / "ens")
    monkeypatch.setattr(qeth.hot_wallet, "KEYSTORE_DIR",
                        tmp_path / "keystores")
    return tmp_path


class _FakeRpc:
    """Stand-in for qeth.rpc.RpcServer in UI tests. Doesn't bind a
    socket — MainWindow only reads ``host``/``port``/``error`` and
    calls ``start``/``stop``/``broadcast_*``."""
    host = "127.0.0.1"
    port = 0
    error = None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def broadcast_accounts_changed(self, accounts) -> None:
        pass

    def broadcast_chain_changed(self, chain_id) -> None:
        pass

    def set_rpc_chain(self, chain_id) -> None:
        pass


@pytest.fixture
def fake_rpc() -> _FakeRpc:
    return _FakeRpc()


@pytest.fixture
def hermetic_mainwindow(monkeypatch):
    """Neutralize the background workers MainWindow kicks off at
    startup so UI tests don't hit Blockscout / DefiLlama / token-list
    sources. Each network-bound QThread's ``run`` becomes a no-op;
    they still get ``start()``'d and emit ``finished``, so the
    self-eviction wiring stays exercised.
    """
    from qeth.plugins import ens as ens_plugin
    from qeth.plugins import tokens as tokens_plugin
    from qeth.plugins import transactions as transactions_plugin

    def _noop_run(self):  # pragma: no cover - simple no-op
        return

    for mod, cls_names in (
        (tokens_plugin, [
            "TokenListsLoader", "TokenListWorker", "BalanceWorker",
            "PricesWorker", "RiskWorker", "MetadataWorker",
            "OwnTokenDiscoveryWorker",
        ]),
        (transactions_plugin, ["TransactionsWorker"]),
        (ens_plugin, ["EnsNamesWorker", "EnsRecordsWorker", "EnsVerifyWorker"]),
    ):
        for cls_name in cls_names:
            cls = getattr(mod, cls_name, None)
            if cls is not None:
                monkeypatch.setattr(cls, "run", _noop_run)


@pytest.fixture
def mainwindow(qtbot, tmp_qeth, fake_rpc, hermetic_mainwindow):
    """A live MainWindow with all on-disk state under tmp_path and all
    network workers neutralized. Use ``qtbot`` to drive interactions."""
    from qeth.store import Store
    from qeth.ui import MainWindow

    store = Store.load()
    win = MainWindow(store, fake_rpc)
    qtbot.addWidget(win)
    return win
