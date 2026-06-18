"""Disk cache for built :class:`~qeth.tx_activity.Activity` objects, keyed
by (chain_id, address).

A confirmed tx's activity never changes (its function call and the
transfers it emitted are fixed), so the verb + coins we resolved last
session — one tokentx/internal batch plus a Blockscout ABI per distinct
callee — can be reloaded straight from JSON instead of refetching. That
turns a chain's *second* visit from "blank for a minute" into instant,
and makes the receipt/simulation-derived coins survive a restart.

Layout mirrors qeth.abi_cache: ``<config>/activities/<chain_id>/<addr>.json``.
Kept in memory per key so repeated reads/writes don't hit disk; the whole
key is rewritten on update (small — one page of rows).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .fsatomic import atomic_write_text
from .store import CONFIG_DIR
from .tx_activity import Activity, AssetLeg

log = logging.getLogger("qeth.activity_cache")

ACTIVITIES_DIR = CONFIG_DIR / "activities"

# Bump when the activity-build logic changes what a tx resolves to (e.g.
# widening the transfer fetch so older rows gain coins). A cache written by
# an older build is then ignored and those rows are rebuilt, rather than
# pinning the stale result forever.
_BUILD_VERSION = 2


def _leg_to_json(leg: AssetLeg) -> list:
    return [leg.symbol, leg.contract]


def _leg_from_json(raw: list) -> AssetLeg:
    return AssetLeg(str(raw[0]), raw[1])


def _to_json(a: Activity) -> dict:
    return {
        "v": a.verb,
        "o": [_leg_to_json(x) for x in a.out],
        "i": [_leg_to_json(x) for x in a.inn],
        "a": a.show_arrow,
        "m": a.muted,
    }


def _from_json(d: dict) -> Activity:
    return Activity(
        str(d["v"]),
        tuple(_leg_from_json(x) for x in d.get("o", [])),
        tuple(_leg_from_json(x) for x in d.get("i", [])),
        bool(d.get("a", True)),
        bool(d.get("m", False)),
    )


class ActivityCache:
    """Per-(chain, address) ``{tx_hash: Activity}`` store, JSON on disk,
    cached in memory."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or ACTIVITIES_DIR
        self._mem: dict[tuple[int, str], dict[str, Activity]] = {}

    def _file(self, chain_id: int, address: str) -> Path:
        return self._root / str(chain_id) / f"{address.lower()}.json"

    def _key(self, chain_id: int, address: str) -> tuple[int, str]:
        return (chain_id, address.lower())

    def load(self, chain_id: int, address: str) -> dict[str, Activity]:
        """Resolved activities for this view (memory, then disk). Returns a
        copy so callers can't mutate the cache in place."""
        key = self._key(chain_id, address)
        cached = self._mem.get(key)
        if cached is None:
            cached = {}
            try:
                raw = json.loads(self._file(chain_id, address).read_text())
            except (OSError, ValueError):
                raw = {}
            # Skip an older build's file (legacy flat format, or a build
            # before a logic change) — those rows get rebuilt.
            if isinstance(raw, dict) and raw.get("_v") == _BUILD_VERSION:
                for h, d in (raw.get("acts") or {}).items():
                    try:
                        cached[h] = _from_json(d)
                    except Exception:   # tolerate a partial schema
                        continue
            self._mem[key] = cached
        return dict(cached)

    def update(self, chain_id: int, address: str,
               acts: dict[str, Activity]) -> None:
        """Merge freshly-resolved activities in and persist the key."""
        if not acts:
            return
        key = self._key(chain_id, address)
        if key not in self._mem:
            self.load(chain_id, address)   # hydrate from disk first
        self._mem[key].update(acts)
        f = self._file(chain_id, address)
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(f, json.dumps({
                "_v": _BUILD_VERSION,
                "acts": {h: _to_json(a) for h, a in self._mem[key].items()},
            }))
        except OSError as e:
            log.debug("activity cache write failed for %s: %s", key, e)
