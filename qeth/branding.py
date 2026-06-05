"""Window icon that matches the user's light/dark theme.

Two SVG variants live in ``qeth/assets/logos/``:

- ``qeth-icon-mono.svg`` — dark glyph, for light themes.
- ``qeth-icon-reversed.svg`` — light glyph, for dark themes.

We pick one at app startup based on the current QPalette window-
background luminance. If the user flips themes mid-session the
icon stays as-set until next launch — keeping the code simple
beats catching a corner case ~nobody hits.

Why palette.window() rather than the WM's title-bar tint: Qt
exposes no portable way to read the title-bar colour (drawn by
the window manager, not Qt). On every mainstream Linux theme the
title bar and the window body track each other, so the palette
is close enough to right without per-DE plumbing.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QIcon, QPalette


_ASSETS = Path(__file__).parent / "assets" / "logos"
_ICON_LIGHT_BG = _ASSETS / "qeth-icon-mono.svg"      # dark glyph
_ICON_DARK_BG = _ASSETS / "qeth-icon-reversed.svg"   # light glyph


def is_dark_palette(palette: QPalette) -> bool:
    """True when the palette's window background is dark enough
    that a reversed (light) glyph reads better on top of it.
    ITU-R BT.601 perceived luminance, threshold 0.5."""
    c = palette.color(QPalette.ColorRole.Window)
    luminance = 0.299 * c.redF() + 0.587 * c.greenF() + 0.114 * c.blueF()
    return luminance < 0.5


def app_icon_for(palette: QPalette) -> QIcon:
    path = _ICON_DARK_BG if is_dark_palette(palette) else _ICON_LIGHT_BG
    return QIcon(str(path))
