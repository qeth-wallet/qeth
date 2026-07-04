"""Signer backends as plugins — one ``SignerPlugin`` per account ``source``, in
a ``REGISTRY`` the signing dispatch consults instead of ``if source == …``.

Step 1 of ``docs/signers.md``: the ``source`` → ``Signer`` mapping and the
per-source metadata live here; ``ui.py`` looks a plugin up by ``source`` and
drives it (prompting for an unlock secret when the plugin asks). Adding a
backend (Keystone / Keycard Shell QR, …) becomes a new module + one registry
entry. The interaction host and account-creation flows are later steps.
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
