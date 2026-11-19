"""Plugin choices for Settings. Changes take effect on the next launch."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtWidgets import QCheckBox, QWidget

from .plugins.registry import BUILTIN_MANIFESTS, PluginManifest


def optional_manifests() -> list[PluginManifest]:
    """The plugins the user is allowed to turn off (everything but the
    required ones), in manifest order."""
    return [
        m for m in sorted(BUILTIN_MANIFESTS, key=lambda m: m.order) if not m.required
    ]


def build_plugin_checkboxes(
    disabled_plugins: Iterable[str],
    parent: QWidget | None = None,
) -> dict[str, QCheckBox]:
    """Build staged choices for optional plugins; checking doesn't write a store."""
    disabled = set(disabled_plugins)
    checks = {}
    for m in optional_manifests():
        check = QCheckBox(m.title, parent)
        check.setChecked(m.id not in disabled)
        check.setToolTip(m.description)
        checks[m.id] = check
    return checks
