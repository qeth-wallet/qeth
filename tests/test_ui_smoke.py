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


def test_qt_data_location_sandboxed_away_from_home(qtbot):
    """conftest redirects XDG_DATA_HOME into a throwaway dir, so a stray Qt /
    QtWebEngine write (a QWebEngineProfile, an app-data file) can never land in
    the developer's real ~/.local/share (see the historical `<stdin>/QtWebEngine`
    leftover). qtbot ensures the QApplication (hence an app-name in the resolved
    paths) exists. (Config/Cache are intentionally left real so Qt fonts resolve
    — see the conftest note.)"""
    from PySide6.QtCore import QStandardPaths as QSP
    real_data = os.path.join(os.path.expanduser("~"), ".local", "share")
    for loc in (QSP.StandardLocation.AppDataLocation,
                QSP.StandardLocation.AppLocalDataLocation,
                QSP.StandardLocation.GenericDataLocation):
        path = QSP.writableLocation(loc)
        assert path and not path.startswith(real_data), (loc, path)


def test_mainwindow_builds(mainwindow):
    """Just constructing MainWindow under offscreen + tmp paths +
    no-op workers is itself a non-trivial assertion: it means the
    whole widget tree, signals, splitter restore and timer setup
    all initialize without raising."""
    assert mainwindow.windowTitle() == "qeth — Ethereum wallet"


def test_join_workers_joins_running_and_skips_finished(mainwindow):
    """On quit, _join_workers waits for still-running workers (else Qt aborts on
    a live QThread → SIGABRT / macOS 'closed unexpectedly'), but doesn't wait on
    ones that already finished."""
    calls = {"running": 0, "done": 0}

    class _FakeWorker:            # a plain class stays hashable (SimpleNamespace isn't)
        def __init__(self, name, running):
            self.name, self.running = name, running

        def isRunning(self):
            return self.running

        def wait(self, ms):
            calls[self.name] += 1
            return True

    mainwindow._active_workers = {_FakeWorker("running", True),
                                  _FakeWorker("done", False)}
    mainwindow._join_workers()
    assert calls == {"running": 1, "done": 0}   # only the live thread is joined


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
    must be set — one unset var still triggers the probe. VA-API is first (so an
    Intel box selects it and never reaches VDPAU); VDPAU stays LAST in the decode
    list as a fallback for VDPAU-only machines, but is out of the encode list."""

    _DEC = "QT_FFMPEG_DECODING_HW_DEVICE_TYPES"
    _ENC = "QT_FFMPEG_ENCODING_HW_DEVICE_TYPES"

    def test_vaapi_first_both_vars_on_linux(self):
        from qeth.__main__ import _set_ffmpeg_hwaccel
        env = {}
        _set_ffmpeg_hwaccel(env, "linux")
        for var in (self._DEC, self._ENC):
            types = env[var].split(",")
            assert types[0] == "vaapi"            # working Intel/AMD path first
            assert "cuda" in types                # non-Intel path kept available

    def test_vdpau_is_a_last_resort_decode_fallback(self):
        from qeth.__main__ import _set_ffmpeg_hwaccel
        env = {}
        _set_ffmpeg_hwaccel(env, "linux")
        dec = env[self._DEC].split(",")
        assert dec[-1] == "vdpau"                 # kept, but only after all else
        assert "vdpau" not in env[self._ENC]      # no VDPAU encoder exists

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
