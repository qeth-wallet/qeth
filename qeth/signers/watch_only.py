"""Watch-only source plugin: holds addresses but can't sign. Registered so
``can_sign() is False`` is a first-class property of the source (the dispatch
warns 'no signer') rather than an ``else`` branch."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import SignerPlugin

if TYPE_CHECKING:
    from ..signing import Signer
    from ..store import Store


class WatchOnlySignerPlugin(SignerPlugin):
    source_id = "watch_only"
    display_name = "Watch-only"

    def can_sign(self) -> bool:
        return False

    def make_signer(self, store: Store, secret: str | None = None) -> Signer:
        from ..signing import SignerError
        raise SignerError("watch-only accounts cannot sign")
