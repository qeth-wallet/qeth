"""Ledger hardware-wallet signer plugin. Wraps ``ledger.LedgerSigner`` — which
routes every dongle call through the single-thread ``ledger_hid`` service (see
CLAUDE.md); this plugin only supplies the dispatch metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import SignerPlugin

if TYPE_CHECKING:
    from ..chains import Chain
    from ..signing import Signer, SigningRequest
    from ..store import Store
    from .interaction import SignerInteraction


class LedgerSignerPlugin(SignerPlugin):
    source_id = "ledger"
    display_name = "Ledger"
    progress_text = "Confirm on your Ledger device…"

    def tx_progress_text(self, req: SigningRequest, chain: Chain) -> str:
        # Local mirror lookup only — never asks a server (ledger_metadata).
        from ..ledger import device_review_text
        return device_review_text(req, chain)

    def make_signer(
        self, store: Store, account: dict[str, Any], ui: SignerInteraction,
    ) -> Signer:
        # No up-front interaction: the device confirm happens inside sign(),
        # surfaced by the caller's spinner (progress_text).
        from ..ledger import LedgerSigner
        return LedgerSigner(store)
