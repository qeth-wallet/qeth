"""Trezor hardware-wallet signer plugin. Wraps ``trezor.TrezorSigner`` — which
runs every trezorlib call on the single Trezor device thread; this plugin only
supplies the dispatch metadata. The signer holds ``ui`` so the device's prompts
(PIN / passphrase / "confirm") can update the spinner or ask for input."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import SignerPlugin

if TYPE_CHECKING:
    from ..signing import Signer
    from ..store import Store
    from .interaction import SignerInteraction


class TrezorSignerPlugin(SignerPlugin):
    source_id = "trezor"
    display_name = "Trezor"
    progress_text = "Confirm on your Trezor…"

    def make_signer(
        self, store: Store, account: dict[str, Any], ui: SignerInteraction,
    ) -> Signer:
        from ..trezor import TrezorSigner
        return TrezorSigner(account, ui)
