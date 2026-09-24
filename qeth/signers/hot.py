"""Hot-wallet signer plugin. Wraps ``hot_wallet.HotWalletSigner``, which decrypts
a passphrase-protected keystore. The passphrase is collected up front (main
thread) via ``secret_prompt`` so the slow scrypt decrypt runs on the worker.
An account used within the last ``hot_wallet.UNLOCK_TTL_S`` signs without a
prompt."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..chains import FAMILIES
from .base import SignerPlugin

if TYPE_CHECKING:
    from ..signing import Signer
    from ..store import Store
    from .interaction import SignerInteraction


class HotWalletSignerPlugin(SignerPlugin):
    source_id = "hot"
    display_name = "Hot wallet"
    progress_text = "Decrypting keystore and signing…"
    # One secp256k1 key signs an EVM tx and a Tron txid alike.
    families = frozenset(FAMILIES)

    def make_signer(
        self, store: Store, account: dict[str, Any], ui: SignerInteraction,
    ) -> Signer | None:
        from ..hot_wallet import UNLOCKED, HotWalletSigner
        address = account["address"]
        priv = UNLOCKED.get(address)
        if priv is not None:
            return HotWalletSigner(
                store, unlocked=(address, priv), cache=UNLOCKED)
        # Collect the passphrase up front (main thread) so the worker's slow
        # scrypt decrypt has it when sign() runs. A cancel returns None.
        secret = ui.request_secret(
            prompt=f"Passphrase for {address}:", title=self.display_name)
        if secret is None:
            return None
        return HotWalletSigner(store, passphrase=secret, cache=UNLOCKED)
