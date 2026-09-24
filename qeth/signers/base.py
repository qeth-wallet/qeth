"""``SignerPlugin`` — one account ``source`` (ledger / hot / watch_only / …)
behind a small, uniform interface the signing UI dispatches through.

Holds the per-source metadata the dispatch needs (display name, whether an
unlock secret must be collected first) and builds the ``Signer``. Any UI a
backend needs — progress, a secret prompt, a QR exchange — goes through the
``SignerInteraction`` passed to ``make_signer``, so no backend imports Qt.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ..chains import EVM

if TYPE_CHECKING:
    from ..signing import Signer
    from ..store import Store
    from .interaction import SignerInteraction


class SignerPlugin(ABC):
    """The metadata + factory for one account ``source``. Instances are
    stateless singletons in the ``REGISTRY`` (no per-account state — the
    account, store and interaction host are passed in), so a new backend is a
    subclass + a registry entry, not edits to the dispatch."""

    source_id: str
    display_name: str
    # Chain families this backend can sign for. A signer can hold a Tron
    # account yet not sign for it (Ledger's Ethereum app), so this is checked
    # alongside the account's own family before a Tron transaction is built.
    families: frozenset[str] = frozenset((EVM,))
    # Spinner label shown while the signature is produced. Source-specific
    # (device confirm vs. keystore decrypt); the caller shows it via the
    # interaction host's ``progress``.
    progress_text: str = ""

    def can_sign(self) -> bool:
        """False for a source that holds addresses but can't produce a
        signature (watch-only), so the dispatch warns instead of building a
        Signer."""
        return True

    @abstractmethod
    def make_signer(
        self, store: Store, account: dict[str, Any], ui: SignerInteraction,
    ) -> Signer | None:
        """Build the ``Signer`` for ``account``, driving ``ui`` for any up-front
        interaction — a hot wallet prompts for its passphrase via
        ``ui.request_secret``. Returns ``None`` if the user cancelled that
        prompt. ``ui`` is also what a worker-side backend (the QR signer)
        holds to drive its exchange from ``sign()``."""
