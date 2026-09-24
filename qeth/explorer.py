"""Block-explorer links, per chain family: Etherscan-family paths on EVM
chains (``/tx/``, ``/address/``, ``/token/``), Tronscan's hash routes on Tron
(``/#/transaction/``, ``/#/address/``, ``/#/token20/``). Values are qeth's
internal forms — ``0x`` tx hashes and ``0x`` addresses — converted here."""

from __future__ import annotations

from .address import codec_for
from .chains import TRON, Chain


def explorer_url(chain: Chain | None, kind: str, value: str | None, *,
                 ref_addr: str | None = None) -> str | None:
    """The explorer page for a ``tx`` hash, an ``address`` or a ``token``
    contract — None when the chain has no explorer or the kind is unknown.
    ``token`` with ``ref_addr`` narrows Etherscan's token page to one holder
    (``?a=``); Tronscan has no such filter, so it's dropped there."""
    if chain is None or not chain.explorer or not value:
        return None
    base = chain.explorer.rstrip("/")
    if chain.family == TRON:
        codec = codec_for(TRON)
        if kind == "tx":
            return f"{base}/#/transaction/{value.removeprefix('0x')}"
        if kind == "address":
            return f"{base}/#/address/{codec.display(value)}"
        if kind == "token":
            return f"{base}/#/token20/{codec.display(value)}"
        return None
    if kind == "tx":
        return f"{base}/tx/{value}"
    if kind == "address":
        return f"{base}/address/{value}"
    if kind == "token":
        return f"{base}/token/{value}" + (f"?a={ref_addr}" if ref_addr else "")
    return None
