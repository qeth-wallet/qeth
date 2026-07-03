"""Ledger hardware-wallet signer plugin. Wraps ``ledger.LedgerSigner`` — which
routes every dongle call through the single-thread ``ledger_hid`` service (see
CLAUDE.md); this plugin only supplies the dispatch metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import SignerPlugin

if TYPE_CHECKING:
    from ..signing import Signer
    from ..store import Store
    from .interaction import SignerInteraction


class LedgerSignerPlugin(SignerPlugin):
    source_id = "ledger"
    display_name = "Ledger"
    progress_text = "Confirm on your Ledger device…"

    def make_signer(
        self, store: Store, account: dict[str, Any], ui: SignerInteraction,
    ) -> Signer:
        # No up-front interaction: the device confirm happens inside sign(),
        # surfaced by the caller's spinner (progress_text).
        from ..ledger import LedgerSigner
        return LedgerSigner(store)
