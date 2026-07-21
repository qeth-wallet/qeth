# Type stub for the `Falkon` module — the browser-plugin API injected into the
# Python interpreter by the Falkon web browser at runtime (there is no PyPI
# package). Lets a type checker resolve `import Falkon` in
# extensions/falkon/qeth_connector/ instead of treating it as Any. Covers
# only the surface the connector uses.
from typing import Any


class PluginInterface:
    # Sentinel passed to init() when the plugin is enabled after startup (so
    # already-open windows must be seeded).
    LateInitState: Any


class AbstractButtonInterface:
    # A navigation-bar / status-bar button. Subclasses override id()/name()
    # and connect to ``clicked`` (emits a click controller).
    clicked: Any
    def __init__(self) -> None: ...
    def setIcon(self, icon: Any) -> None: ...
    def setTitle(self, title: str) -> None: ...
    def setToolTip(self, tip: str) -> None: ...


class ExternalJsObject:
    @staticmethod
    def registerExtraObject(name: str, obj: Any) -> None: ...

    @staticmethod
    def unregisterExtraObject(obj: Any) -> None: ...


class _Scripts:
    def find(self, name: str) -> list[Any]: ...
    def remove(self, script: Any) -> None: ...
    def insert(self, script: Any) -> None: ...


class _WebProfile:
    def scripts(self) -> _Scripts: ...


class MainApplication:
    @staticmethod
    def instance() -> "MainApplication | None": ...

    def webProfile(self) -> _WebProfile: ...

    # Plugin manager (carries mainWindowCreated / mainWindowDeleted signals).
    def plugins(self) -> Any: ...

    # Open browser windows (each has navigationBar()/statusBar()).
    def windows(self) -> list[Any]: ...


def registerPlugin(plugin: PluginInterface) -> None: ...
