import locale
import logging
import os
import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from .alerts import warn

from .branding import app_icon
from .rpc import RpcServer
from .store import Store
from .tray import install_tray
from .ui import MainWindow


def _raise_open_file_limit(target: int = 8192) -> int | None:
    """Lift the soft RLIMIT_NOFILE toward ``target`` (capped by the hard
    limit). macOS ships a low soft limit (256) while qeth opens many sockets
    at once — concurrent chainlist probes, the failover Sessions, the live
    watcher, prices — plus a pipe per QThread. Hit the cap and Qt can't make
    a thread pipe ("Too many open files") and DNS lookups start failing,
    which looks like the RPC being unreachable (see issue #24).

    Unix-only and best-effort: ``resource`` is absent on Windows, and a
    kernel may reject a raise we then swallow. Returns the new soft limit,
    or ``None`` if unchanged / unavailable."""
    try:
        import resource
    except ImportError:
        return None  # Windows — no POSIX rlimits
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    # hard may be RLIM_INFINITY (-1); clamp the request to a concrete number
    # either way — macOS rejects setrlimit to an unbounded soft value.
    desired = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if soft != resource.RLIM_INFINITY and soft >= desired:
        return soft
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
        return desired
    except (ValueError, OSError):
        return None


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


def _pin_ffmpeg_vaapi(environ, platform) -> None:
    """Stop the camera from logging "Failed to open VDPAU backend
    libvdpau_va_gl.so".

    To list available hardware video codecs, Qt's ffmpeg backend iterates
    *every* ffmpeg hw type and creates a device context for each to test it —
    including VDPAU. On an Intel/AMD GPU there's no native VDPAU driver, so
    libvdpau reaches for the ``va_gl`` shim (usually not installed) and writes
    that line straight to stderr. It's a raw C-library message, not a Qt/ffmpeg
    log, so no log level or probe *reorder* silences it — the whole-list probe
    simply must not run. And it runs whenever EITHER the decoding or encoding
    device-type var is unset (setting just one isn't enough — the other's query
    still fires it; that's why an earlier decoding-only attempt didn't help).

    So give ffmpeg an explicit list for BOTH decode and encode: that skips the
    probe entirely, and the list omits VDPAU so nothing ever creates a VDPAU
    context. Pin VA-API — the path that actually works on Intel/AMD (verified:
    ``vainfo`` loads the iHD driver). Other GPUs / unsupported codecs fall back
    to software decode, which is free for a low-res webcam QR scan and all we
    need (add ``cuda`` here if NVIDIA hardware decode is ever wanted).

    Linux-only (VA-API/VDPAU are Linux; macOS/Windows use their own decoders),
    read by the ffmpeg plugin at camera init, and ``setdefault`` so an explicit
    user override wins."""
    if platform.startswith("linux"):
        environ.setdefault("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", "vaapi")
        environ.setdefault("QT_FFMPEG_ENCODING_HW_DEVICE_TYPES", "vaapi")


def _running_bundled_qt(environ) -> bool:
    """True when Qt is bundled *without* the host's qt6ct/Kvantum platform-theme
    plugin — a Flatpak (``FLATPAK_ID``) or an AppImage (its runtime exports
    ``APPIMAGE``). In both, qeth adopts the host font + a legible icon theme
    itself; natively (neither set) Qt's own qt6ct plugin does it."""
    return bool(environ.get("FLATPAK_ID") or environ.get("APPIMAGE"))


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
    # Flatpak (sandbox has only Breeze/hicolor) or AppImage (bundled Qt, no
    # qt6ct plugin to read the user's theme). Native installs keep their real
    # desktop icon theme. TODO(appimage): an AppImage is *un*sandboxed, so the
    # user's real themes are on disk — honour their configured qt6ct icon_theme
    # before falling back to this legible default (needs VM verification).
    if not _running_bundled_qt(environ):
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


def _adopt_host_qt_font(app, environ) -> None:
    """Inside a Flatpak, adopt the font from the host's qt6ct/qt5ct config.

    The sandbox can't load the qt6ct platform-theme plugin (it isn't in the
    runtime, and qt6ct 0.9 doesn't build against the runtime's Qt 6.10), so
    the user's configured font never reaches the app and Qt falls back to a
    smaller default. qt6ct stores the general font as a ``QFont.toString()``
    value under ``[Fonts] general``, which ``QFont.fromString()`` round-trips
    — so we just read and apply it ourselves (those config dirs are mounted
    read-only in the manifest). A no-op natively (Qt's own qt6ct plugin
    handles it) or when no config / font is found. Applies equally to an
    AppImage (also a bundled Qt with no qt6ct plugin; reads the real config
    straight off disk since there's no sandbox)."""
    if not _running_bundled_qt(environ):
        return
    from pathlib import Path
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QFont
    cfg = environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    for name in ("qt6ct", "qt5ct"):
        conf = Path(cfg) / name / f"{name}.conf"
        if not conf.is_file():
            continue
        raw = QSettings(
            str(conf), QSettings.Format.IniFormat).value("Fonts/general")
        font = QFont()
        if raw and font.fromString(str(raw)):
            app.setFont(font)
            return


def _install_sigint_shutdown(app, window, signal_module=signal) -> QTimer:
    """Route Ctrl+C through the same close path as the window manager.

    Qt's event loop sits in C++ long enough that Python's default SIGINT
    handling isn't observed promptly. A 500 ms no-op QTimer gives the
    interpreter regular checkpoints, and the handler schedules
    ``window.close()`` so closeEvent persists UI state before app shutdown.
    The previous handler is restored on aboutToQuit. ``signal_module`` is
    injectable for testing."""
    previous_handler = signal_module.getsignal(signal_module.SIGINT)
    shutdown_requested = False

    def request_shutdown(_signum, _frame) -> None:
        nonlocal shutdown_requested
        if shutdown_requested:
            return
        shutdown_requested = True
        QTimer.singleShot(0, window.close)

    def restore_handler() -> None:
        signal_module.signal(signal_module.SIGINT, previous_handler)

    signal_module.signal(signal_module.SIGINT, request_shutdown)
    app.aboutToQuit.connect(restore_handler)

    # The interpreter-checkpoint heartbeat: parented to ``app`` so Qt keeps
    # it alive (no Python ref needed).
    timer = QTimer(app)
    timer.setInterval(500)
    timer.timeout.connect(lambda: None)
    timer.start()
    return timer


def main() -> int:
    _harden_x11_backing_store(os.environ, sys.platform)
    _pin_ffmpeg_vaapi(os.environ, sys.platform)
    _raise_open_file_limit()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Own the config/cache root owner-only (0700) before anything writes under
    # it, and tighten a root an older build left at the default umask.
    from .store import ensure_private_root
    ensure_private_root()
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
    # Sandboxed runs only (no-op natively): make theme icons legible and adopt
    # the host's configured font — both before any widget is built.
    _ensure_legible_icon_theme(os.environ)
    _adopt_host_qt_font(app, os.environ)
    # A single self-contained tile icon — legible on any surface, so we
    # don't have to (and can't portably) guess the taskbar/panel colour.
    app.setWindowIcon(app_icon())

    # Single-instance guard (4a): a second qeth sharing ~/.qeth would silently
    # clobber the first's config / wallet cache / tx cache (load-once,
    # whole-state-save, no cross-process merge). Hand off to the running
    # instance — raise its window — and exit, BEFORE any heavy import / RPC
    # bind / store load, so the second launch is cheap. Keyed on the config
    # root so only instances that actually share it are coalesced.
    from .single_instance import SingleInstanceGuard
    from .store import CONFIG_DIR
    guard = SingleInstanceGuard(str(CONFIG_DIR))
    if not guard.is_primary():
        return 0

    # Pull the web3/eth_abi/requests stack now, while no window
    # exists yet. They're deferred at import time (qeth.chain only
    # loads them on first EthClient construction); leaving the
    # trigger to a worker thread firing right after win.show() puts
    # the ~400 ms of Python module init in GIL contention with the
    # main thread's first paint, leaving the window frame visible
    # but its contents blank until the import finishes.
    from .chain import _ensure_heavy_imports
    _ensure_heavy_imports()

    # aiohttp picks the c-ares (pycares) resolver whenever aiodns is
    # importable — it is here, via system site-packages. That resolver's
    # Channel spins up a shutdown thread (pycares._run_safe_shutdown_loop) on
    # teardown/GC that can segfault when it overlaps the Qt event loop at app
    # exit. Force aiohttp's ThreadedResolver — plenty for our handful of
    # connections — before the RPC server / live watcher create any connector,
    # so no pycares Channel is ever created.
    import aiohttp.connector as _aiohttp_connector
    import aiohttp.resolver as _aiohttp_resolver
    _aiohttp_connector.DefaultResolver = _aiohttp_resolver.ThreadedResolver
    _aiohttp_resolver.DefaultResolver = _aiohttp_resolver.ThreadedResolver

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
    guard.set_window(win)   # a later launch's hand-off raises this window
    win.show()
    # Minimise → tray when the platform has one. Keep a reference
    # so Python doesn't GC the controller; Qt's parent ownership
    # ties its lifetime to the window.
    _tray = install_tray(app, win)  # noqa: F841 — kept-alive ref
    win.set_tray(_tray)             # the desktop-notification sink (or None)
    # Ctrl+C → window.close() (persists state) → app quits. Timer is
    # app-parented, so no kept-alive ref needed.
    _install_sigint_shutdown(app, win)
    try:
        return app.exec()
    finally:
        rpc.stop()


if __name__ == "__main__":
    sys.exit(main())
