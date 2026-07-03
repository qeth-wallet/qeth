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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..signing import Signer
    from ..store import Store


class SignerPlugin(ABC):
    """The metadata + factory for one account ``source``. Instances are
    stateless singletons in the ``REGISTRY`` (no per-account state — the
    account and store are passed in), so a new backend is a subclass + a
    registry entry, not edits to the dispatch."""

    source_id: str
    display_name: str
    # Spinner label shown while the signature is produced. Source-specific
    # (device confirm vs. keystore decrypt); step 2 folds it into the
    # interaction host's ``progress``.
    progress_text: str = ""

    def can_sign(self) -> bool:
        """False for a source that holds addresses but can't produce a
        signature (watch-only), so the dispatch warns instead of building a
        Signer."""
        return True

    def secret_prompt(self, address: str) -> str | None:
        """A label to prompt for an unlock secret BEFORE signing — collected on
        the main thread so the worker has it when ``sign()`` runs — or ``None``
        when the backend needs none. Hot wallets return the passphrase prompt;
        Ledger / watch-only return ``None``."""
        return None

    @abstractmethod
    def make_signer(self, store: Store, secret: str | None) -> Signer:
        """Build the ``Signer`` for this source. ``secret`` is the
        ``secret_prompt`` result (``None`` when it returned ``None``)."""
