"""Startup helpers in qeth.__main__."""

from __future__ import annotations

import subprocess
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMainWindow

from qeth.__main__ import (
    _adopt_host_qt_font, _ensure_legible_icon_theme, _install_sigint_shutdown,
)


class _FakeSignalModule:
    """Stand-in for the ``signal`` module so the SIGINT wiring is testable
    without touching the real process-wide handler."""

    SIGINT = object()

    def __init__(self):
        self.previous_handler = object()
        self.handler = None

    def getsignal(self, sig):
        assert sig is self.SIGINT
        return self.previous_handler

    def signal(self, sig, handler):
        assert sig is self.SIGINT
        self.handler = handler
        return self.previous_handler


def test_icon_theme_is_a_noop_outside_flatpak(qtbot):
    # No FLATPAK_ID → the icon theme must be left exactly as the desktop
    # set it (native installs keep the user's real qt6ct/Kvantum theme).
    before = QIcon.themeName()
    _ensure_legible_icon_theme({})
    assert QIcon.themeName() == before


def test_icon_theme_pinned_inside_flatpak(qtbot):
    # FLATPAK_ID set → pin a legible theme. breeze/breeze-dark is the
    # guaranteed backstop in the runtime (variant chosen to contrast with
    # the palette); Papirus/Adwaita win only if their Icontheme extension is
    # installed. Whichever the probe lands on, it's never the unthemed default.
    before = QIcon.themeName()
    try:
        _ensure_legible_icon_theme({"FLATPAK_ID": "io.github.michwill.qeth"})
        assert QIcon.themeName() in (
            "Papirus", "Adwaita", "breeze", "breeze-dark")
    finally:
        QIcon.setThemeName(before)


def test_font_adopted_from_qt6ct_in_flatpak(qtbot, tmp_path):
    # FLATPAK_ID set + a qt6ct.conf present → adopt its [Fonts] general font.
    (tmp_path / "qt6ct").mkdir()
    (tmp_path / "qt6ct" / "qt6ct.conf").write_text(
        '[Fonts]\ngeneral="Courier New,16,-1,5,50,0,0,0,0,0"\n')
    app = QApplication.instance()
    before = app.font()
    try:
        _adopt_host_qt_font(
            app, {"FLATPAK_ID": "x", "XDG_CONFIG_HOME": str(tmp_path)})
        assert round(app.font().pointSizeF()) == 16
    finally:
        app.setFont(before)


def test_font_is_a_noop_outside_flatpak(qtbot, tmp_path):
    # No FLATPAK_ID → never touch the font (native Qt reads qt6ct itself).
    app = QApplication.instance()
    before = app.font()
    _adopt_host_qt_font(app, {"XDG_CONFIG_HOME": str(tmp_path)})
    assert app.font() == before


def test_sigint_shutdown_closes_window_via_close_event(qtbot, monkeypatch):
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    monkeypatch.setattr(app, "quit", lambda: None)   # keep the shared test app
    fake_signal = _FakeSignalModule()
    closed: list[bool] = []

    class Window(QMainWindow):
        def closeEvent(self, event):  # noqa: N802 - Qt override
            closed.append(True)
            super().closeEvent(event)

    win = Window()
    qtbot.addWidget(win)
    win.show()
    assert win.isVisible()

    timer = _install_sigint_shutdown(app, win, signal_module=fake_signal)
    try:
        assert timer.isActive()
        assert fake_signal.handler is not None
        # Firing SIGINT schedules window.close() → closeEvent persists state.
        fake_signal.handler(fake_signal.SIGINT, None)
        qtbot.waitUntil(lambda: bool(closed) and not win.isVisible())
    finally:
        timer.stop()
        timer.deleteLater()


def test_sigint_shutdown_quits_when_the_window_is_in_the_tray(
        qtbot, monkeypatch):
    """Minimised to the tray the window is hidden, and closing a hidden window
    never triggers quitOnLastWindowClosed — Ctrl+C left the app running (and
    swallowed every further Ctrl+C). It must still persist state AND quit."""
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    quits: list[bool] = []
    monkeypatch.setattr(app, "quit", lambda: quits.append(True))
    fake_signal = _FakeSignalModule()
    closed: list[bool] = []

    class Window(QMainWindow):
        def closeEvent(self, event):  # noqa: N802 - Qt override
            closed.append(True)
            super().closeEvent(event)

    win = Window()
    qtbot.addWidget(win)
    win.show()
    win.hide()                       # what the tray does on minimise

    timer = _install_sigint_shutdown(app, win, signal_module=fake_signal)
    try:
        assert fake_signal.handler is not None
        fake_signal.handler(fake_signal.SIGINT, None)
        qtbot.waitUntil(lambda: bool(quits))
        assert closed                # closeEvent still persisted the state
    finally:
        timer.stop()
        timer.deleteLater()


def test_asyncio_executor_survives_the_qt_import():
    """Python 3.15 binds concurrent.futures' ThreadPoolExecutor lazily, and
    once shiboken6 has replaced __import__ the lazy object never resolves —
    asyncio's run_in_executor (every aiohttp DNS lookup) then fails.
    qeth/__init__ resolves it first. Checked in a fresh interpreter started
    the way every entry point starts: qeth, then Qt. A no-op before 3.15."""
    code = (
        "import qeth, PySide6.QtCore, asyncio\n"
        "async def main():\n"
        "    return await asyncio.get_running_loop().run_in_executor(None, lambda: 42)\n"
        "print(asyncio.run(main()))\n"
    )
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=120)
    assert out.stdout.strip() == "42", out.stderr
