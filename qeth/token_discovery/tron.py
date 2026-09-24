"""``TronGridSource`` — which TRC-20s a Tron account holds, from TronGrid's
indexed ``/v1/accounts/{addr}``. Its ``trc20`` field lists every token balance
the account has (spam airdrops included; the tokens plugin's known-token gate
filters those), as ``{T… contract: raw balance string}`` with no metadata:
``lookup`` fills symbol / name / decimals from the token lists where the token
is known, and the plugin's on-chain metadata multicall covers the rest."""

from __future__ import annotations

from collections.abc import Callable

from ..address import tron_from_hex, tron_to_hex
from ..chains import TRON, Chain
from ..tron.client import TRONGRID_INSTANCES, TronClient, TronError, TronTransportError
from .sources import RateLimited, TokenBalance, TokenSource, TokenSourceError
from .tokenlists import TokenListEntry

Lookup = Callable[[int, str], "TokenListEntry | None"]


class TronGridSource(TokenSource):
    def __init__(self, lookup: Lookup | None = None) -> None:
        self._lookup = lookup

    def supports(self, chain: Chain) -> bool:
        return chain.family == TRON and chain.chain_id in TRONGRID_INSTANCES

    def list_balances(self, chain: Chain, address: str) -> list[TokenBalance]:
        try:
            resp = TronClient(chain).index_get(f"/v1/accounts/{tron_from_hex(address)}")
        except TronTransportError as e:
            # Keyless TronGrid rate-limits hard (HTTP 429 → transport error).
            raise RateLimited(str(e)) from e
        except TronError as e:
            raise TokenSourceError(str(e)) from e
        rows = resp.get("data") or []
        if not rows:
            return []          # never activated: no on-chain account yet
        out: list[TokenBalance] = []
        for entry in rows[0].get("trc20") or []:
            for contract_t, raw in entry.items():
                contract = tron_to_hex(contract_t)
                try:
                    balance = int(raw)
                except (TypeError, ValueError):
                    continue
                if contract is None or balance <= 0:
                    continue
                known = (self._lookup(chain.chain_id, contract.lower())
                         if self._lookup is not None else None)
                out.append(TokenBalance(
                    contract=contract,
                    symbol=known.symbol if known else "",
                    name=known.name if known else "",
                    decimals=known.decimals if known else 0,
                    balance_raw=balance))
        return out
