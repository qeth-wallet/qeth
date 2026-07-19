"""Shared plugin on/off menu — one builder used by both entry points (the tray
"Plugins" submenu and the in-window config gear), so they can never diverge.

Restart-to-apply: toggling only writes the store; the change takes effect on the
next launch. Each action is checkable (checked = enabled) and covers exactly the
optional (non-required) plugins.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QMenu, QWidget

from .plugins.registry import BUILTIN_MANIFESTS, PluginManifest


def optional_manifests() -> list[PluginManifest]:
    """The plugins the user is allowed to turn off (everything but the
    required ones), in manifest order."""
    return [m for m in sorted(BUILTIN_MANIFESTS, key=lambda m: m.order)
            if not m.required]


def build_plugin_toggle_menu(
    store,
    parent: QWidget | None = None,
    on_toggled: Callable[[str, bool], None] | None = None,
    *,
    title: str = "Plugins",
) -> QMenu:
    """A menu of checkable actions, one per optional plugin (checked = enabled).
    Toggling persists via ``store.set_plugin_enabled`` and then calls
    ``on_toggled(plugin_id, enabled)`` — the caller uses that to surface the
    "restart to apply" hint through its own status channel."""
    menu = QMenu(title, parent)
    menu.setToolTipsVisible(True)
    for m in optional_manifests():
        act = menu.addAction(m.title)
        act.setCheckable(True)
        act.setChecked(m.id not in store.disabled_plugins)
        if m.description:
            act.setToolTip(m.description)

        def _toggle(checked: bool, mid: str = m.id) -> None:
            store.set_plugin_enabled(mid, checked)
            if on_toggled is not None:
                on_toggled(mid, checked)

        act.toggled.connect(_toggle)
    return menu
