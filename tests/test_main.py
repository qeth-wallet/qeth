"""Startup helpers in qeth.__main__."""

from __future__ import annotations

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


def test_sigint_shutdown_closes_window_via_close_event(qtbot):
    app = QApplication.instance()
    assert isinstance(app, QApplication)
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
