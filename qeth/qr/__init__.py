"""Air-gapped QR signer codec (BC-UR + EIP-4527) — headless, no Qt.

Step 3a of docs/signers-qr.md. Turns an ``eth-sign-request`` into the ``ur:…``
text a QR carries, and parses an ``eth-signature`` back out. Layers:

- ``bytewords`` — Blockchain Commons Bytewords (BCR-2020-012): bytes ⇄ QR-safe
  text, with a CRC32 tail.
- ``ur`` — single-part Uniform Resources (BCR-2020-005): ``ur:<type>/<bytewords>``.
- ``eth`` — the EIP-4527 registry (eth-sign-request / eth-signature / crypto-keypath).

The fountain multi-part codec (animated QR for large payloads) is deferred — a
single EVM sign-request and a 65-byte signature each fit one part.
"""
