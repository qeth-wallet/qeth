"""Desktop notifications via the freedesktop notification service.

Qt's ``QSystemTrayIcon.showMessage`` does not get a *custom* icon through to
some notification daemons — notably xfce4-notifyd, where the message shows
but the icon is dropped. So instead of going through Qt we talk to
``org.freedesktop.Notifications`` directly, attaching the composed icon (token
/ coin logo + direction badge) as an ``image-path``.

We shell out to ``notify-send`` (libnotify; preferred — it takes plain string
arguments, so an adversarial token symbol can't be misparsed) or ``gdbus``
(glib; near-universally present, incl. the Flatpak runtimes) rather than
PySide6's QtDBus, whose ``QDBusArgument`` can't marshal the ``Notify``
signature's bare ``uint32`` ``replaces_id``. When neither tool exists,
``send()`` returns ``False`` and the caller falls back to the Qt tray.

A side benefit: this works with no system tray at all (GNOME without an
AppIndicator extension, etc.) — the notification service is independent of
the tray.
"""

import logging
import shutil
import subprocess
from pathlib import Path

from PySide6.QtGui import QPixmap

log = logging.getLogger("qeth.notify")

# Rotating icon-file slots: image-path points at a file the daemon reads when
# it shows the notification, so we can't overwrite a single path under a burst
# (a swap fires two). A small ring of slots keeps a recent one alive long
# enough without unbounded temp files.
_ICON_DIR = Path.home() / ".qeth" / "notify"
_SLOTS = 8
_TIMEOUT_MS = 6000


def _gvariant_path(path: str) -> str:
    """A filesystem path as a GVariant ``image-path`` hint dict for gdbus.
    Our paths live under ~/.qeth/notify with controlled names (no quotes),
    so single-quoting is sufficient."""
    return "{'image-path': <'%s'>}" % path


class DesktopNotifier:
    """Sends sent/received desktop notifications with a custom icon, bypassing
    Qt's tray (which loses the icon on xfce4-notifyd). Fire-and-forget — a
    slow daemon never blocks the UI thread."""

    def __init__(self) -> None:
        self._notify_send = shutil.which("notify-send")
        self._gdbus = shutil.which("gdbus")
        self._slot = 0

    @property
    def available(self) -> bool:
        return bool(self._notify_send or self._gdbus)

    def send(
        self, title: str, body: str, pixmap: "QPixmap | None" = None,
    ) -> bool:
        """Dispatch a notification; return True iff a backend handled it (the
        caller falls back to the Qt tray on False). Never raises."""
        if not self.available:
            return False
        icon_path = self._write_icon(pixmap)
        try:
            argv = self._argv(title, body, icon_path)
            subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            log.exception("desktop notification dispatch failed")
            return False

    # --- internals --------------------------------------------------------

    def _argv(self, title: str, body: str, icon_path: "str | None") -> "list[str]":
        if self._notify_send:
            argv = [self._notify_send, "--app-name=qeth",
                    "-t", str(_TIMEOUT_MS)]
            if icon_path:
                argv += ["-i", icon_path]
            # `--` guards a summary/body that begins with '-' (scam token
            # symbols are arbitrary) from being read as an option.
            argv += ["--", title, body]
            return argv
        # gdbus: knows the introspected signature, so plain strings pass
        # through for the s-typed summary/body/app_icon; only the a{sv} hints
        # need GVariant text.
        assert self._gdbus is not None
        return [
            self._gdbus, "call", "--session",
            "--dest", "org.freedesktop.Notifications",
            "--object-path", "/org/freedesktop/Notifications",
            "--method", "org.freedesktop.Notifications.Notify",
            "qeth", "0", icon_path or "", title, body, "[]",
            _gvariant_path(icon_path) if icon_path else "{}", str(_TIMEOUT_MS),
        ]

    def _write_icon(self, pixmap: "QPixmap | None") -> "str | None":
        if pixmap is None or pixmap.isNull():
            return None
        try:
            _ICON_DIR.mkdir(parents=True, exist_ok=True)
            path = _ICON_DIR / f"{self._slot}.png"
            self._slot = (self._slot + 1) % _SLOTS
            if pixmap.save(str(path), "PNG"):
                return str(path)
        except Exception:
            log.exception("notification icon write failed")
        return None
