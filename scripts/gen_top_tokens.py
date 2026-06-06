#!/usr/bin/env python3
"""Regenerate the bundled top-tokens seed at ``qeth/data/top_tokens.json``.

Run occasionally — the top of the market-cap table is stable, so the
shipped snapshot stays a fine first-run baseline between regenerations.
The same ``fetch_top_tokens`` backs the runtime TTL refresh, so this only
re-seeds the cold-cache case.

    uv run python scripts/gen_top_tokens.py
"""

import json
import time
from pathlib import Path

from qeth.toptokens import COINGECKO_PLATFORMS, build_payload, fetch_top_tokens

OUT = Path(__file__).resolve().parent.parent / "qeth" / "data" / "top_tokens.json"
TOP_N = 1000


def main() -> None:
    chain_ids = list(COINGECKO_PLATFORMS)
    fetched = fetch_top_tokens(chain_ids, top_n=TOP_N)
    payload = build_payload(fetched, time.time(), TOP_N)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1))
    print(f"wrote {OUT}")
    for cid, toks in sorted(fetched.items()):
        print(f"  chain {cid:>5}: {len(toks):>4} tokens"
              + (f"  e.g. {toks[0].symbol} {toks[0].address}" if toks else ""))


if __name__ == "__main__":
    main()
