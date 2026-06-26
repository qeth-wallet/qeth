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
        # Checkable toggle for the sent/received desktop notifications, backed
        # by the store flag (persisted). The tray is where notifications live,
        # so this is the natural home for the switch.
        self._notify_act = menu.addAction("Transaction notifications")
        self._notify_act.setCheckable(True)
        self._notify_act.setChecked(self._notifications_enabled())
        self._notify_act.toggled.connect(self._on_notify_toggled)
        menu.addSeparator()
        menu.addAction("Exit", app.quit)
        # Refresh enable/disable just before the menu paints —
        # avoids having to subscribe to every window state change.
        menu.aboutToShow.connect(self._refresh_menu_state)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()

        window.installEventFilter(self)

    # --- notifications ----------------------------------------

    def show_message(
        self, title: str, body: str, icon=None, msecs: int = 6000,
    ) -> None:
        """Raise a native desktop notification via the tray. On Linux this
        routes to the freedesktop notification daemon. ``icon`` is a custom
        ``QIcon`` (the token/coin logo with a ↑/↓ direction badge); without
        one we fall back to the generic Information icon."""
        if icon is not None:
            self.tray.showMessage(title, body, icon, msecs)
        else:
            self.tray.showMessage(
                title, body, QSystemTrayIcon.MessageIcon.Information, msecs)

    def _store(self):
        return getattr(self._win, "store", None)

    def _notifications_enabled(self) -> bool:
        store = self._store()
        return bool(getattr(store, "notifications_enabled", True)) \
            if store is not None else True

    def _on_notify_toggled(self, on: bool) -> None:
        store = self._store()
        if store is None:
            return
        store.notifications_enabled = on
        store.save()

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
        # Just hide — do NOT touch the window state here.
        #
        # Calling setWindowState() to clear the minimised bit and hide() in the
        # same breath is trouble either way round: clear-then-hide races the
        # WM's restore-map against the hide-unmap (rare empty frame stuck on
        # screen), and hide-then-clear runs setWindowState on an already
        # unmapped window, where Qt's X11 backend blocks the GUI thread waiting
        # on a WM reply that never arrives — the app hangs on every minimise.
        #
        # The canonical pattern: hide() on minimise, showNormal() on restore
        # (see _show). showNormal() clears the minimised bit as it remaps, so
        # the window always comes back as a normal, restored window.
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
        # showNormal() maps the window AND clears the minimised bit in one
        # documented call — no separate setWindowState (which can hang on a
        # not-yet-mapped window). Restores to the previous normal geometry.
        self._win.showNormal()
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
