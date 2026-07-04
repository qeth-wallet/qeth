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


class TestX11BackingStoreHardening:
    """QT_X11_NO_MITSHM is set before QApplication so the window doesn't
    stop repainting after many hours of X11 uptime (MIT-SHM surface goes
    bad; only hide/show recovers it)."""

    def test_sets_on_linux_when_unset(self):
        from qeth.__main__ import _harden_x11_backing_store
        env = {}
        _harden_x11_backing_store(env, "linux")
        assert env["QT_X11_NO_MITSHM"] == "1"

    def test_respects_explicit_override(self):
        from qeth.__main__ import _harden_x11_backing_store
        env = {"QT_X11_NO_MITSHM": "0"}
        _harden_x11_backing_store(env, "linux")
        assert env["QT_X11_NO_MITSHM"] == "0"   # user choice left alone

    def test_noop_off_linux(self):
        from qeth.__main__ import _harden_x11_backing_store
        for plat in ("darwin", "win32"):
            env = {}
            _harden_x11_backing_store(env, plat)
            assert "QT_X11_NO_MITSHM" not in env


class TestFfmpegVaapiPriority:
    """VA-API is put first in the ffmpeg decode probe order so the camera
    doesn't hit the missing-VDPAU-shim warning on Intel/AMD (no native VDPAU) —
    the other paths stay as fallbacks for portability."""

    _VAR = "QT_FFMPEG_DECODING_HW_DEVICE_TYPES"

    def test_sets_vaapi_first_on_linux_when_unset(self):
        from qeth.__main__ import _prioritize_ffmpeg_vaapi
        env = {}
        _prioritize_ffmpeg_vaapi(env, "linux")
        order = env[self._VAR].split(",")
        assert order[0] == "vaapi"                      # working path tried first
        assert "vdpau" in order and order.index("vdpau") > order.index("vaapi")

    def test_respects_explicit_override(self):
        from qeth.__main__ import _prioritize_ffmpeg_vaapi
        env = {self._VAR: "cuda"}
        _prioritize_ffmpeg_vaapi(env, "linux")
        assert env[self._VAR] == "cuda"                 # user choice left alone

    def test_noop_off_linux(self):
        from qeth.__main__ import _prioritize_ffmpeg_vaapi
        for plat in ("darwin", "win32"):
            env = {}
            _prioritize_ffmpeg_vaapi(env, plat)
            assert self._VAR not in env
