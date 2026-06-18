"""Token risk reports from GoPlus Security API.

Per-contract behavioral risk flags (honeypot, hidden owner, blacklist,
transfer taxes, take-back-ownership, …) cached on disk. Augments the
local text-heuristic scam check in ``TokenLists.is_likely_scam`` — see
``RiskReport.is_high_risk``.

GoPlus API: ``GET https://api.gopluslabs.io/api/v1/token_security/<chain_id>
?contract_addresses=0x...,0x...`` (batched, no API key required for
the free tier, ~30 req/min). Boolean fields are returned as the
strings "0" and "1"; taxes as decimal-string fractions.
"""

import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from . import USER_AGENT
from .fsatomic import atomic_write_text

log = logging.getLogger("qeth.risk")

CACHE_DIR = Path.home() / ".qeth" / "risk"
DEFAULT_TIMEOUT = 15.0
DEFAULT_TTL_SECONDS = 7 * 86400  # 1 week; contracts can become risky later
BATCH_SIZE = 100


# Sell-tax threshold above which we treat the contract as effectively
# a rug-pull regardless of the named flags.
HIGH_SELL_TAX = 0.50


@dataclass
class RiskReport:
    is_honeypot: bool = False
    is_blacklisted: bool = False
    hidden_owner: bool = False
    cannot_buy: bool = False
    cannot_sell_all: bool = False
    can_take_back_ownership: bool = False
    is_proxy: bool = False
    is_in_dex: bool = False
    buy_tax: float = 0.0
    sell_tax: float = 0.0
    fetched_at: int = 0       # unix seconds — drives TTL

    def is_high_risk(self) -> bool:
        """True if the contract behaves in a way that costs the user real
        money. Honeypot / hidden owner / take-back-ownership are the
        smoking guns; a sell tax above ``HIGH_SELL_TAX`` (50% by default)
        catches most rug-pull-shaped contracts that don't trip a named
        flag."""
        return bool(
            self.is_honeypot
            or self.is_blacklisted
            or self.hidden_owner
            or self.cannot_buy
            or self.cannot_sell_all
            or self.can_take_back_ownership
            or (self.sell_tax > HIGH_SELL_TAX)
        )


def _to_bool(s) -> bool:
    """GoPlus encodes booleans as strings '0'/'1'; absent / blank → False."""
    if s in (None, "", "null"):
        return False
    if isinstance(s, bool):
        return s
    return str(s).strip() in ("1", "true", "True")


def _to_float(s) -> float:
    if s in (None, "", "null"):
        return 0.0
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def _parse_report(info: dict, now: int) -> RiskReport:
    return RiskReport(
        is_honeypot=_to_bool(info.get("is_honeypot")),
        is_blacklisted=_to_bool(info.get("is_blacklisted")),
        hidden_owner=_to_bool(info.get("hidden_owner")),
        cannot_buy=_to_bool(info.get("cannot_buy")),
        cannot_sell_all=_to_bool(info.get("cannot_sell_all")),
        can_take_back_ownership=_to_bool(info.get("can_take_back_ownership")),
        is_proxy=_to_bool(info.get("is_proxy")),
        is_in_dex=_to_bool(info.get("is_in_dex")),
        buy_tax=_to_float(info.get("buy_tax")),
        sell_tax=_to_float(info.get("sell_tax")),
        fetched_at=now,
    )


class GoPlusRisk:
    """Fetch per-contract risk reports from the GoPlus Security API."""

    BASE = "https://api.gopluslabs.io/api/v1/token_security"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        self.timeout = timeout

    def fetch(self, chain_id: int, contracts: list[str]) -> dict[str, RiskReport]:
        """Returns ``{addr_lower: RiskReport}``. Tokens GoPlus didn't have
        anything to say about (notably very-well-known assets like USDC)
        are silently omitted — caller should treat absence as
        "uninteresting" not "safe"."""
        if not contracts:
            return {}
        now = int(time.time())
        out: dict[str, RiskReport] = {}
        for start in range(0, len(contracts), BATCH_SIZE):
            batch = [
                c.lower() for c in contracts[start:start + BATCH_SIZE]
                if c.lower().startswith("0x") and len(c) == 42
            ]
            if not batch:
                continue
            url = (
                f"{self.BASE}/{int(chain_id)}?contract_addresses="
                + urllib.parse.quote(",".join(batch), safe=",")
            )
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
            except Exception as e:
                log.warning("goplus batch failed (%d items): %s", len(batch), e)
                continue
            for addr, info in (data.get("result") or {}).items():
                if not isinstance(info, dict):
                    continue
                out[str(addr).lower()] = _parse_report(info, now)
        return out


class RiskCache:
    """Persistent ``(chain, contract) -> RiskReport`` cache. One JSON
    file per chain at ``~/.qeth/risk/<chain_id>.json``. Stale entries
    (older than ``ttl_seconds``) are treated as missing on read so
    callers re-fetch them.
    """

    def __init__(self, cache_dir: Path | None = None,
                 ttl_seconds: float = DEFAULT_TTL_SECONDS):
        # Look up CACHE_DIR at instantiation, not at function definition
        # time, so monkeypatch.setattr(qeth.risk, "CACHE_DIR", ...) in
        # tests actually takes effect. (Default-arg expressions evaluate
        # once at def-time and capture the *current* value of CACHE_DIR.)
        self.cache_dir = cache_dir if cache_dir is not None else CACHE_DIR
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._chains: dict[int, dict[str, RiskReport]] = {}

    def _path(self, chain_id: int) -> Path:
        return self.cache_dir / f"{int(chain_id)}.json"

    def _load_chain(self, chain_id: int) -> dict[str, RiskReport]:
        p = self._path(chain_id)
        if not p.exists():
            return {}
        try:
            raw = json.loads(p.read_text())
        except Exception as e:
            log.warning("risk cache parse failed %s: %s", p, e)
            return {}
        out: dict[str, RiskReport] = {}
        for addr, fields in raw.items():
            if not isinstance(fields, dict):
                continue
            try:
                out[str(addr).lower()] = RiskReport(
                    is_honeypot=bool(fields.get("is_honeypot")),
                    is_blacklisted=bool(fields.get("is_blacklisted")),
                    hidden_owner=bool(fields.get("hidden_owner")),
                    cannot_buy=bool(fields.get("cannot_buy")),
                    cannot_sell_all=bool(fields.get("cannot_sell_all")),
                    can_take_back_ownership=bool(
                        fields.get("can_take_back_ownership")
                    ),
                    is_proxy=bool(fields.get("is_proxy")),
                    is_in_dex=bool(fields.get("is_in_dex")),
                    buy_tax=float(fields.get("buy_tax") or 0.0),
                    sell_tax=float(fields.get("sell_tax") or 0.0),
                    fetched_at=int(fields.get("fetched_at") or 0),
                )
            except (TypeError, ValueError):
                continue
        return out

    def _chain_cache(self, chain_id: int) -> dict[str, RiskReport]:
        chain_id = int(chain_id)
        with self._lock:
            c = self._chains.get(chain_id)
            if c is None:
                c = self._load_chain(chain_id)
                self._chains[chain_id] = c
            return c

    def get(self, chain_id: int, contract: str) -> RiskReport | None:
        r = self._chain_cache(chain_id).get(contract.lower())
        if r is None:
            return None
        if self.ttl_seconds > 0 and (time.time() - r.fetched_at) > self.ttl_seconds:
            return None  # stale; treat as missing
        return r

    def missing(self, chain_id: int, contracts: list[str]) -> list[str]:
        """Subset of ``contracts`` whose risk we don't have (or it's stale).
        Preserves the input casing for downstream API calls."""
        cc = self._chain_cache(chain_id)
        now = time.time()
        out: list[str] = []
        for c in contracts:
            r = cc.get(c.lower())
            if r is None or (
                self.ttl_seconds > 0 and (now - r.fetched_at) > self.ttl_seconds
            ):
                out.append(c)
        return out

    def put_many(self, chain_id: int, reports: dict[str, RiskReport]) -> None:
        chain_id = int(chain_id)
        with self._lock:
            c = self._chain_cache(chain_id)
            for addr, report in reports.items():
                c[addr.lower()] = report
            p = self._path(chain_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(p, json.dumps(
                {addr: asdict(r) for addr, r in c.items()},
                indent=2, sort_keys=True,
            ))
