import locale
import logging
import os
import sys

from PySide6.QtWidgets import QApplication
from .alerts import warn

from .branding import app_icon_for
from .rpc import RpcServer
from .store import Store
from .tray import install_tray
from .ui import MainWindow


def _harden_x11_backing_store(environ, platform) -> None:
    """Disable Qt's MIT-SHM (shared-memory) X11 backing store. Left on,
    the window stops repainting after many hours of uptime — the
    SHM-backed surface gets into a bad state and only a hide/show
    (minimise to tray and back) recreates it. Turning SHM off trades a
    touch of paint latency for a window that stays drawable indefinitely.

    xcb-only, so it's a harmless no-op on Wayland/macOS/Windows (we still
    gate on Linux for tidiness). ``setdefault`` leaves an explicit user
    override (``QT_X11_NO_MITSHM=0``) alone. Must run *before*
    QApplication — Qt's xcb plugin reads the var at init."""
    if platform.startswith("linux"):
        environ.setdefault("QT_X11_NO_MITSHM", "1")


def _ensure_legible_icon_theme(environ) -> None:
    """Inside a Flatpak, pick an icon theme that actually renders.

    The PySide6 Flatpak runs on ``org.kde.Platform``, whose only icon
    themes are Breeze (monochrome) + a sparse hicolor. Breeze's glyphs are
    meant to be *recoloured* to the palette by KDE Frameworks' icon loader,
    which a plain PySide6 app doesn't have — so you get the ``breeze-dark``
    variant's light glyphs on a light background: near-invisible action
    icons (copy, +/-, …).

    Force a legible theme: prefer a full-colour one if the user installed
    its ``org.freedesktop.Platform.Icontheme.*`` extension (Papirus /
    Adwaita), else pin the light ``breeze`` variant so glyphs are
    dark-on-light. A no-op outside the sandbox (``FLATPAK_ID`` unset), so
    native installs keep inheriting the user's real desktop icon theme.

    Must run after QApplication exists (the icon engine needs it) but
    before any widgets are built, so their icons resolve against the
    chosen theme."""
    if not environ.get("FLATPAK_ID"):
        return
    from PySide6.QtGui import QIcon, QPalette
    from PySide6.QtWidgets import QApplication
    # Match the monochrome-Breeze backstop to the palette: dark glyphs on a
    # light window, light glyphs on a dark one — otherwise we'd just swap
    # invisible-on-white for invisible-on-black. Full-colour themes
    # (Papirus / Adwaita) read fine either way, so they're preferred.
    dark = QApplication.palette().color(
        QPalette.ColorRole.Window).lightness() < 128
    breeze = "breeze-dark" if dark else "breeze"
    # Probe each candidate by asking for an icon we actually use; the first
    # that resolves it (so is installed and has our action icons) wins.
    # breeze/breeze-dark is always in the runtime, so it's the backstop.
    for name in ("Papirus", "Adwaita", breeze):
        QIcon.setThemeName(name)
        if not QIcon.fromTheme("edit-copy").isNull():
            return


def main() -> int:
    _harden_x11_backing_store(os.environ, sys.platform)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Honor the user's LC_TIME for strftime("%x %X") in tx timestamps.
    # Python starts in the POSIX C locale until something flips it;
    # without this call all timestamps would render as MM/DD/YY.
    try:
        locale.setlocale(locale.LC_TIME, "")
    except locale.Error:
        # Misconfigured environment (e.g. LC_ALL set to a locale that
        # isn't installed). Fall back silently; strftime works in C.
        pass

    app = QApplication(sys.argv)
    app.setApplicationName("qeth")
    app.setOrganizationName("qeth")
    # Sandboxed runs only: make sure theme icons are legible before any
    # widget is built. No-op natively.
    _ensure_legible_icon_theme(os.environ)
    # Pick the light- or dark-bg variant of the window icon based
    # on the current palette. Theme swaps mid-session don't update
    # it — restart picks up the new one.
    app.setWindowIcon(app_icon_for(app.palette()))

    # Pull the web3/eth_abi/requests stack now, while no window
    # exists yet. They're deferred at import time (qeth.chain only
    # loads them on first EthClient construction); leaving the
    # trigger to a worker thread firing right after win.show() puts
    # the ~400 ms of Python module init in GIL contention with the
    # main thread's first paint, leaving the window frame visible
    # but its contents blank until the import finishes.
    from .chain import _ensure_heavy_imports
    _ensure_heavy_imports()

    store = Store.load()
    rpc = RpcServer(store)
    rpc.start()

    if rpc.error:
        warn(
            None,
            "qeth — JSON-RPC failed to start",
            f"Could not bind to {rpc.host}:{rpc.port}.\n\n{rpc.error}\n\n"
            "Frame may already be running. The wallet UI will still work, "
            "but dapps won't be able to connect.",
        )

    win = MainWindow(store, rpc)
    win.show()
    # Minimise → tray when the platform has one. Keep a reference
    # so Python doesn't GC the controller; Qt's parent ownership
    # ties its lifetime to the window.
    _tray = install_tray(app, win)  # noqa: F841 — kept-alive ref
    try:
        return app.exec()
    finally:
        rpc.stop()


if __name__ == "__main__":
    sys.exit(main())
