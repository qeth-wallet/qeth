"""Startup helpers in qeth.__main__."""

from __future__ import annotations

import subprocess
import sys

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMainWindow

from qeth.__main__ import (
    _adopt_host_qt_font, _ensure_legible_icon_theme, _install_signal_shutdown,
)


class _FakeSignalModule:
    """Stand-in for the ``signal`` module so the SIGINT/SIGTERM wiring is
    testable without touching the real process-wide handlers."""

    SIGINT = object()
    SIGTERM = object()

    def __init__(self):
        self.previous = {self.SIGINT: object(), self.SIGTERM: object()}
        self.handlers = dict(self.previous)

    def getsignal(self, sig):
        return self.handlers[sig]

    def signal(self, sig, handler):
        assert sig in self.handlers
        old, self.handlers[sig] = self.handlers[sig], handler
        return old


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

    timer = _install_signal_shutdown(app, win, signal_module=fake_signal)
    try:
        assert timer.isActive()
        handler = fake_signal.handlers[fake_signal.SIGINT]
        assert callable(handler)
        # Firing SIGINT schedules window.close() → closeEvent persists state.
        handler(fake_signal.SIGINT, None)
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

    timer = _install_signal_shutdown(app, win, signal_module=fake_signal)
    try:
        fake_signal.handlers[fake_signal.SIGINT](fake_signal.SIGINT, None)
        qtbot.waitUntil(lambda: bool(quits))
        assert closed                # closeEvent still persisted the state
    finally:
        timer.stop()
        timer.deleteLater()


def test_sigterm_shuts_down_like_ctrl_c(qtbot, monkeypatch):
    """SIGTERM (kill / pkill / timeout / "End process") used to end the
    interpreter on the spot, skipping atexit — the Helios sidecar outlived qeth
    with a broken log pipe and aborted (apport: "closed unexpectedly"). It must
    close + quit like Ctrl+C, so atexit stops the sidecar."""
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

    timer = _install_signal_shutdown(app, win, signal_module=fake_signal)
    try:
        handler = fake_signal.handlers[fake_signal.SIGTERM]
        assert callable(handler)
        assert fake_signal.handlers[fake_signal.SIGINT] is handler
        handler(fake_signal.SIGTERM, None)
        qtbot.waitUntil(lambda: bool(quits))
        assert closed                # closeEvent persisted the state
    finally:
        timer.stop()
        timer.deleteLater()


def test_a_second_sigterm_gets_the_previous_action(qtbot, monkeypatch):
    """Only the first SIGTERM is graceful: it hands SIGTERM back to its
    previous action (the default terminate), so a second ``kill`` still ends a
    qeth whose event loop is wedged and would never run the scheduled quit.
    SIGINT keeps our handler — its default (KeyboardInterrupt) is useless."""
    app = QApplication.instance()
    assert isinstance(app, QApplication)
    monkeypatch.setattr(app, "quit", lambda: None)
    fake_signal = _FakeSignalModule()
    win = QMainWindow()
    qtbot.addWidget(win)

    timer = _install_signal_shutdown(app, win, signal_module=fake_signal)
    try:
        ours = fake_signal.handlers[fake_signal.SIGTERM]
        ours(fake_signal.SIGTERM, None)
        assert (fake_signal.handlers[fake_signal.SIGTERM]
                is fake_signal.previous[fake_signal.SIGTERM])
        assert fake_signal.handlers[fake_signal.SIGINT] is ours
    finally:
        timer.stop()
        timer.deleteLater()


def test_about_to_quit_restores_both_previous_handlers(qtbot):
    # A stand-in app: emitting the shared QApplication's aboutToQuit would also
    # fire every handler other tests left connected to it.
    class FakeApp(QObject):
        aboutToQuit = Signal()

        def quit(self):
            pass

    app = FakeApp()
    fake_signal = _FakeSignalModule()
    win = QMainWindow()
    qtbot.addWidget(win)

    timer = _install_signal_shutdown(app, win, signal_module=fake_signal)
    try:
        assert fake_signal.handlers != fake_signal.previous
        app.aboutToQuit.emit()
        assert fake_signal.handlers == fake_signal.previous
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


def test_desktop_file_name_matches_the_shipped_desktop_entry():
    # On Wayland the dock/taskbar matches a window to an installed .desktop
    # entry by the xdg-toplevel app_id, which Qt takes from desktopFileName —
    # unset, it was "python3" and GNOME showed a generic icon. The name main()
    # sets must stay the id every package installs the entry under (deb, rpm,
    # Gentoo and the Flatpak all install dist/flatpak/<id>.desktop).
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    src = (root / "qeth" / "__main__.py").read_text()
    names = re.findall(r'setDesktopFileName\("([^"]+)"\)', src)
    assert names == ["io.github.michwill.qeth"]
    assert (root / "dist" / "flatpak" / f"{names[0]}.desktop").is_file()
    manifest = (root / "dist" / "flatpak" / "io.github.michwill.qeth.yml").read_text()
    assert re.search(rf"^id: {re.escape(names[0])}$", manifest, re.M)
