"""System tray icon for qeth.

When the platform exposes a system tray, minimising the main
window redirects to the tray: the window is hidden (so it drops
out of the taskbar entirely) and the tray icon becomes the only
on-screen affordance until the user brings it back. Left-click
the tray icon to toggle visibility; right-click for a Show / Hide
/ Exit menu.

If no system tray is available (some GNOME setups without an
AppIndicator extension, headless sessions, …) ``install_tray``
returns None and the window keeps its standard taskbar-minimise
behaviour. No fallback shim needed.

Close [X] still quits the app — only minimise is redirected. If
we ever want "close also hides", that becomes another opt-in.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QMenu, QSystemTrayIcon,
)


def install_tray(app: QApplication, window: QMainWindow):
    """Wire tray support to ``window``. Returns the controller so
    the caller can keep a reference; returns None when there's no
    system tray, in which case the window's normal minimise
    behaviour is left alone."""
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    return _TrayController(app, window)


class _TrayController(QObject):
    def __init__(self, app: QApplication, window: QMainWindow):
        super().__init__(window)
        self._app = app
        self._win = window

        self.tray = QSystemTrayIcon(app.windowIcon(), self)
        self.tray.setToolTip("qeth — Ethereum wallet")

        menu = QMenu()
        self._show_act = menu.addAction("Show", self._show)
        self._hide_act = menu.addAction("Hide", self._hide)
        menu.addSeparator()
        menu.addAction("Exit", app.quit)
        # Refresh enable/disable just before the menu paints —
        # avoids having to subscribe to every window state change.
        menu.aboutToShow.connect(self._refresh_menu_state)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()

        window.installEventFilter(self)

    # --- Qt overrides -----------------------------------------

    def eventFilter(self, obj, event):  # noqa: N802 — Qt name
        if obj is self._win and event.type() == QEvent.Type.WindowStateChange:
            if self._win.windowState() & Qt.WindowState.WindowMinimized:
                # Defer the hide so we finish handling the
                # current state-change event first.
                QTimer.singleShot(0, self._dehydrate_to_tray)
        return False

    # --- behaviour --------------------------------------------

    def _dehydrate_to_tray(self) -> None:
        # Clear the minimised bit before hiding so a later show
        # comes back as a normal window rather than minimised.
        self._win.setWindowState(
            self._win.windowState() & ~Qt.WindowState.WindowMinimized
        )
        self._win.hide()

    def _on_activated(self, reason) -> None:
        # Left-click toggles. Right-click opens the menu and
        # never reaches here. Middle / double-click ignored.
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        if self._is_user_visible():
            self._hide()
        else:
            self._show()

    def _show(self) -> None:
        self._win.show()
        self._win.setWindowState(
            self._win.windowState() & ~Qt.WindowState.WindowMinimized
        )
        self._win.raise_()
        self._win.activateWindow()

    def _hide(self) -> None:
        self._win.hide()

    def _refresh_menu_state(self) -> None:
        visible = self._is_user_visible()
        self._show_act.setEnabled(not visible)
        self._hide_act.setEnabled(visible)

    def _is_user_visible(self) -> bool:
        """True iff the window is on-screen *and* not minimised."""
        return (
            self._win.isVisible()
            and not bool(self._win.windowState() & Qt.WindowState.WindowMinimized)
        )
