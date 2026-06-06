"""Startup helpers in qeth.__main__."""

from __future__ import annotations

from PySide6.QtGui import QIcon

from qeth.__main__ import _ensure_legible_icon_theme


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
