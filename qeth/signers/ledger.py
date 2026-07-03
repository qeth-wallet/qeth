"""Ledger hardware-wallet signer plugin. Wraps ``ledger.LedgerSigner`` — which
routes every dongle call through the single-thread ``ledger_hid`` service (see
CLAUDE.md); this plugin only supplies the dispatch metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import SignerPlugin

if TYPE_CHECKING:
    from ..signing import Signer
    from ..store import Store


class LedgerSignerPlugin(SignerPlugin):
    source_id = "ledger"
    display_name = "Ledger"
    progress_text = "Confirm on your Ledger device…"

    def make_signer(self, store: Store, secret: str | None = None) -> Signer:
        from ..ledger import LedgerSigner
        return LedgerSigner(store)
