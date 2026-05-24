"""Smoke test for the Qt offscreen platform + pytest-qt setup.

If this passes, ``QApplication`` instantiates under the offscreen
platform plugin and we can drive widgets via ``qtbot``. If this
fails, the more elaborate UI tests will too, so check this one
first when debugging.
"""

import os


def test_offscreen_platform_active():
    # Set early in conftest.py; if anything else flipped it back,
    # the rest of the UI tests would pop visible windows.
    assert os.environ.get("QT_QPA_PLATFORM") == "offscreen"


def test_mainwindow_builds(mainwindow):
    """Just constructing MainWindow under offscreen + tmp paths +
    no-op workers is itself a non-trivial assertion: it means the
    whole widget tree, signals, splitter restore and timer setup
    all initialize without raising."""
    assert mainwindow.windowTitle() == "qeth — Ethereum wallet"
