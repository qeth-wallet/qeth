"""Hot-wallet signer plugin. Wraps ``hot_wallet.HotWalletSigner``, which decrypts
a passphrase-protected keystore. The passphrase is collected up front (main
thread) via ``secret_prompt`` so the slow scrypt decrypt runs on the worker."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .base import SignerPlugin

if TYPE_CHECKING:
    from ..signing import Signer
    from ..store import Store
    from .interaction import SignerInteraction


class HotWalletSignerPlugin(SignerPlugin):
    source_id = "hot"
    display_name = "Hot wallet"
    progress_text = "Decrypting keystore and signing…"

    def make_signer(
        self, store: Store, account: dict[str, Any], ui: SignerInteraction,
    ) -> Signer | None:
        # Collect the passphrase up front (main thread) so the worker's slow
        # scrypt decrypt has it when sign() runs. A cancel returns None.
        address = account["address"]
        secret = ui.request_secret(
            prompt=f"Passphrase for {address}:", title=self.display_name)
        if secret is None:
            return None
        from ..hot_wallet import HotWalletSigner
        return HotWalletSigner(store, passphrase=secret)
