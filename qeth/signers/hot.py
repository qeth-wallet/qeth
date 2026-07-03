"""Hot-wallet signer plugin. Wraps ``hot_wallet.HotWalletSigner``, which decrypts
a passphrase-protected keystore. The passphrase is collected up front (main
thread) via ``secret_prompt`` so the slow scrypt decrypt runs on the worker."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import SignerPlugin

if TYPE_CHECKING:
    from ..signing import Signer
    from ..store import Store


class HotWalletSignerPlugin(SignerPlugin):
    source_id = "hot"
    display_name = "Hot wallet"
    progress_text = "Decrypting keystore and signing…"

    def secret_prompt(self, address: str) -> str:
        return f"Passphrase for {address}:"

    def make_signer(self, store: Store, secret: str | None) -> Signer:
        from ..hot_wallet import HotWalletSigner
        return HotWalletSigner(store, secret or "")
