"""Tron network support: transaction encoding (``tx``), the TronGrid / full-node
HTTP client (``client``) and fee estimation (``fees``). Qt-free.

Addresses stay in qeth's internal ``0x`` + 20-byte form everywhere here; the
``41``-prefixed bytes Tron's protobuf and HTTP API want are produced at the
wire boundary (see ``qeth.address``). Design and research: docs/tron.md.
"""
