"""Hermetic tests for qeth.ledger — no device required.

These pin the one fragile coupling in the module: qeth clears
``ledgereth.comms``'s module-level dongle-cache globals to dodge a
stale-USB-handle bug (where the second sign in a session fails without
ever waking the device). Assigning to a module global always succeeds,
so if a future ledgereth renames those globals our clear silently
becomes a no-op and the bug returns with zero error. We pin every dep by
hash, so that can only happen on a deliberate ``uv lock`` bump — and this
test turns that into a red test at bump time instead of a silent
regression in the field.
"""

import pytest

from qeth.ledger import _clear_dongle_cache


def test_ledgereth_still_exposes_the_dongle_cache_globals():
    comms = pytest.importorskip("ledgereth.comms")
    assert hasattr(comms, "DONGLE_CACHE"), (
        "ledgereth.comms.DONGLE_CACHE is gone — qeth's dongle-cache "
        "clear is now a no-op; second-sign-fails will resurface"
    )
    assert hasattr(comms, "DONGLE_CONFIG_CACHE"), (
        "ledgereth.comms.DONGLE_CONFIG_CACHE is gone — see above"
    )


def test_clear_dongle_cache_nulls_globals_and_closes_handle(monkeypatch):
    comms = pytest.importorskip("ledgereth.comms")

    closed = []

    class _FakeDongle:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(comms, "DONGLE_CACHE", _FakeDongle())
    monkeypatch.setattr(comms, "DONGLE_CONFIG_CACHE", object())

    _clear_dongle_cache()

    assert comms.DONGLE_CACHE is None
    assert comms.DONGLE_CONFIG_CACHE is None
    assert closed == [True], "the cached handle should be closed on clear"
