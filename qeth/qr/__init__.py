"""Air-gapped QR signer codec (BC-UR + EIP-4527) — headless, no Qt.

Step 3a of docs/signers-qr.md. Turns an ``eth-sign-request`` into the ``ur:…``
text a QR carries, and parses an ``eth-signature`` back out. Layers:

- ``bytewords`` — Blockchain Commons Bytewords (BCR-2020-012): bytes ⇄ QR-safe
  text, with a CRC32 tail.
- ``ur`` — single-part Uniform Resources (BCR-2020-005): ``ur:<type>/<bytewords>``.
- ``multipart`` / ``fountain`` — the rateless multi-part codec (BCR-2024-001)
  behind the animated QR a big sign-request needs; a 65-byte signature back
  still fits one part.
- ``eth`` — the EIP-4527 registry (eth-sign-request / eth-signature / crypto-keypath).
"""
