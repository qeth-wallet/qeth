"""Air-gapped QR signer plugin (Keystone / Keycard Shell). No up-front secret
and no generic spinner — the signer drives its own QR-exchange window via
``ui.exchange_qr`` (so ``progress_text`` is empty and ui.py skips the spinner)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import SignerPlugin

if TYPE_CHECKING:
    from ..signing import Signer
    from ..store import Store
    from .interaction import SignerInteraction


class QRSignerPlugin(SignerPlugin):
    source_id = "qr"
    display_name = "Air-gapped (QR)"
    progress_text = ""   # the exchange dialog IS the UI; no Ledger-style spinner

    def make_signer(
        self, store: Store, account: dict[str, Any], ui: SignerInteraction,
    ) -> Signer:
        from ..qr_signer import QRSigner
        return QRSigner(account, ui)
