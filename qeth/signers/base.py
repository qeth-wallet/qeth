"""``SignerPlugin`` — one account ``source`` (ledger / hot / watch_only / …)
behind a small, uniform interface the signing UI dispatches through.

Step 1 of ``docs/signers.md``: this holds the ``source`` → ``Signer`` mapping
plus the per-source metadata the dispatch needs (display name, whether an unlock
secret must be collected first). The interaction host (progress / secret / QR)
and the account-creation flows are later steps — for now the secret is a
declarative prompt the caller renders, and ``make_signer`` takes the result.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

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
        prompt. ``ui`` is also what a worker-side backend (step 3's QR signer)
        holds to drive its exchange from ``sign()``."""
