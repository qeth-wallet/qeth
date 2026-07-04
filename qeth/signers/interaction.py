"""``SignerInteraction`` — the UI a signer needs *while* producing a signature,
abstracted so a backend never imports Qt. Step 2 of ``docs/signers.md``.

One host covers every backend's UX shape: a passive "working…" spinner
(Ledger), a one-shot secret prompt (hot wallet), and — from step 3 — a
bidirectional QR exchange (air-gapped). The concrete Qt implementation
(``qeth.signer_interaction.DialogInteraction``) marshals any call made from a
signing WORKER thread onto the main loop, so a signer can drive UI from
``sign()`` without knowing about threads.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SignerInteraction(Protocol):
    def progress(self, text: str) -> None:
        """Show or update a modal 'working…' spinner labelled ``text``."""
        ...

    def request_secret(self, prompt: str, *, title: str = "") -> str | None:
        """Prompt for an unlock secret (masked input). Returns the entered
        string, or ``None`` if the user cancelled."""
        ...

    def exchange_qr(self, request_parts: list[str]) -> str | None:
        """Show ``request_parts`` (one or more ``ur:…`` strings — a single QR, or
        an animated one cycling the fragments of a large request) and, at the
        same time, run the camera to read the device's response QR; returns the
        scanned ``ur:…`` string, or ``None`` if cancelled. Protocol-agnostic —
        the signer owns the UR encode/decode."""
        ...
