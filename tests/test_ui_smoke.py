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


class TestFfmpegHwaccel:
    """Set an explicit ffmpeg hw-codec list for BOTH decode and encode, so the
    camera never runs the all-types availability probe that creates a VDPAU
    context (→ the "libvdpau_va_gl.so" stderr warning on Intel/AMD). Both vars
    must be set — one unset var still triggers the probe. The list keeps the
    real hardware paths (VA-API first, then CUDA/QSV) but omits VDPAU."""

    _DEC = "QT_FFMPEG_DECODING_HW_DEVICE_TYPES"
    _ENC = "QT_FFMPEG_ENCODING_HW_DEVICE_TYPES"

    def test_sets_both_vars_on_linux(self):
        from qeth.__main__ import _set_ffmpeg_hwaccel
        env = {}
        _set_ffmpeg_hwaccel(env, "linux")
        for var in (self._DEC, self._ENC):
            types = env[var].split(",")
            assert types[0] == "vaapi"            # working Intel/AMD path first
            assert "cuda" in types                # non-Intel path kept available
            assert "vdpau" not in types           # the one troublemaker, excluded

    def test_respects_explicit_override(self):
        from qeth.__main__ import _set_ffmpeg_hwaccel
        env = {self._DEC: "cuda", self._ENC: "cuda"}
        _set_ffmpeg_hwaccel(env, "linux")
        assert env[self._DEC] == "cuda" and env[self._ENC] == "cuda"

    def test_noop_off_linux(self):
        from qeth.__main__ import _set_ffmpeg_hwaccel
        for plat in ("darwin", "win32"):
            env = {}
            _set_ffmpeg_hwaccel(env, plat)
            assert self._DEC not in env and self._ENC not in env
