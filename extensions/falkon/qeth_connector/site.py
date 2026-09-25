# ============================================================
# qeth connector — the site in the active tab
#
# Which dapp the status views describe: the active window's current tab.
# Separate from probe.py, which stays free of Falkon/Qt.
# ============================================================

from qeth_connector.probe import origin_of


def active_tab_origin():
    """The http(s) origin of the active window's current tab, or None."""
    try:
        import Falkon
        app = Falkon.MainApplication.instance()
        window = app.getWindow() if app is not None else None
        view = window.weView() if window is not None else None
        # Read while `window` / `view` are referenced: PyFalkon's wrappers die
        # with their owners' (see bridge._web_views).
        url = view.url().toString() if view is not None else None
    except (AttributeError, RuntimeError, ImportError):
        return None
    return origin_of(url)
