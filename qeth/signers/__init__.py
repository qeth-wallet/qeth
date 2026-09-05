"""Signer backends as plugins — one ``SignerPlugin`` per account ``source``, in
a ``REGISTRY`` the signing dispatch consults instead of ``if source == …``.

Per ``docs/signers.md``: the ``source`` → ``Signer`` mapping and the per-source
metadata live here; ``ui.py`` looks a plugin up by ``source`` and drives it
through a ``SignerInteraction`` (see ``interaction.py``). Adding a backend
(Keystone, Keycard Shell, …) is a new module + one registry entry. The
account-creation flows are still per-source in the wallets plugin.
"""

from __future__ import annotations

from .base import SignerPlugin
from .hot import HotWalletSignerPlugin
from .ledger import LedgerSignerPlugin
from .qr import QRSignerPlugin
from .watch_only import WatchOnlySignerPlugin

# source_id → the singleton plugin. Stateless, so one instance each.
REGISTRY: dict[str, SignerPlugin] = {
    p.source_id: p
    for p in (
        LedgerSignerPlugin(),
        HotWalletSignerPlugin(),
        QRSignerPlugin(),
        WatchOnlySignerPlugin(),
    )
}


def signer_for_source(source: str | None) -> SignerPlugin | None:
    """The plugin for an account ``source``, or ``None`` for an unknown /
    missing source (the caller then reports 'no known signer')."""
    if source is None:
        return None
    return REGISTRY.get(source)


__all__ = [
    "REGISTRY",
    "SignerPlugin",
    "signer_for_source",
]
