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

import os
import sys
from pathlib import Path

import pytest

# Force the offscreen platform plugin before any Qt initialization.
# Must come before pytest-qt's QApplication is created. Leaves an
# already-set value alone (someone might want to run a visible test).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def tmp_qeth(tmp_path, monkeypatch) -> Path:
    """Redirect all qeth on-disk locations under ``tmp_path``."""
    import qeth.abi_cache
    import qeth.hot_wallet
    import qeth.store
    import qeth.token_metadata
    import qeth.tokenlists
    import qeth.transactions_cache
    import qeth.wallet_cache
    import qeth.risk

    monkeypatch.setattr(qeth.store, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(qeth.store, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(qeth.wallet_cache, "CACHE_DIR", tmp_path / "wallets")
    monkeypatch.setattr(qeth.token_metadata, "CACHE_DIR", tmp_path / "token_metadata")
    monkeypatch.setattr(qeth.tokenlists, "CACHE_DIR", tmp_path / "tokenlists")
    monkeypatch.setattr(qeth.risk, "CACHE_DIR", tmp_path / "risk")
    monkeypatch.setattr(qeth.transactions_cache, "CACHE_DIR",
                        tmp_path / "transactions")
    monkeypatch.setattr(qeth.abi_cache, "CACHE_DIR", tmp_path / "abi")
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
    from qeth.plugins import tokens as tokens_plugin
    from qeth.plugins import transactions as transactions_plugin

    def _noop_run(self):  # pragma: no cover - simple no-op
        return

    for mod, cls_names in (
        (tokens_plugin, [
            "TokenListsLoader", "TokenListWorker", "BalanceWorker",
            "PricesWorker", "RiskWorker", "MetadataWorker",
        ]),
        (transactions_plugin, ["TransactionsWorker"]),
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
