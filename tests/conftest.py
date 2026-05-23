"""Test fixtures.

The qeth package defaults a bunch of on-disk paths to ``~/.qeth/...``
(config, wallet cache, tokenlists, token-metadata, risk). The
``tmp_qeth`` fixture redirects every one of them under pytest's
``tmp_path``, so a test never touches the developer's real wallet state
and the tests are hermetic / parallelizable.
"""

from pathlib import Path

import pytest


@pytest.fixture
def tmp_qeth(tmp_path, monkeypatch) -> Path:
    """Redirect all qeth on-disk locations under ``tmp_path``."""
    import qeth.store
    import qeth.token_metadata
    import qeth.tokenlists
    import qeth.wallet_cache
    import qeth.risk

    monkeypatch.setattr(qeth.store, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(qeth.store, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(qeth.wallet_cache, "CACHE_DIR", tmp_path / "wallets")
    monkeypatch.setattr(qeth.token_metadata, "CACHE_DIR", tmp_path / "token_metadata")
    monkeypatch.setattr(qeth.tokenlists, "CACHE_DIR", tmp_path / "tokenlists")
    monkeypatch.setattr(qeth.risk, "CACHE_DIR", tmp_path / "risk")
    return tmp_path
