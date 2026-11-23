"""Populate markets.settled_outcome from fills.realized_pnl. ZERO API CALLS.

Why this exists: `backfill_market_outcomes.py` queries every closed-or-unsettled
row in `markets` -- 277,760 of them, because the discover job ingests every
strike of every BTC/ETH event, and the bot only ever traded a few hundred --
then fetches each one individually. At ~8 req/sec that is ~9.6 hours, and for
markets that settled months ago Kalshi's /markets/{ticker} endpoint returns 404
anyway. The job cannot succeed as written.

It also isn't needed for any market we actually traded. `fills.realized_pnl` is
already the resolved outcome, written by the settler at settlement time when
the data WAS available. The mapping is exact and lossless:

    side='yes', pnl > 0  ->  outcome = 'yes'
    side='yes', pnl < 0  ->  outcome = 'no'
    side='no',  pnl > 0  ->  outcome = 'no'
    side='no',  pnl < 0  ->  outcome = 'yes'

i.e. outcome == side if the fill won, and the opposite side if it lost.

That covers every market with a settled fill -- which is exactly the population
the calibration model is applied to, and the one `calibration_audit.py`'s
scheme 4 needs. Markets we predicted on but never traded still need the API,
but that is a separate and much smaller job once the candidate query is fixed.

Defaults to a DRY RUN. Pass --apply to write.

usage:
    .venv\\Scripts\\python.exe -m src.phase2.derive_outcomes_from_fills
    .venv\\Scripts\\python.exe -m src.phase2.derive_outcomes_from_fills --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter

DEFAULT_DB = "data/poker_quant.db"

# One row per traded market, with the derived outcome and a consistency check:
# if two fills on the same market imply different outcomes, something is wrong
# and we must not write it.
SQL = """
SELECT f.ticker,
       CASE WHEN (f.side = 'yes') = (f.realized_pnl > 0) THEN 'yes' ELSE 'no' END AS derived,
       COUNT(*) AS n,
       m.settled_outcome AS existing
FROM fills f
LEFT JOIN markets m ON f.ticker = m.ticker
WHERE f.realized_pnl IS NOT NULL AND f.realized_pnl != 0
GROUP BY f.ticker, derived
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Derive markets.settled_outcome from fills. No API.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="actually write (default is dry run)")
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=15)
    con.execute("PRAGMA busy_timeout=15000")
    rows = con.execute(SQL).fetchall()

    by_ticker: dict[str, dict] = {}
    for ticker, derived, n, existing in rows:
        d = by_ticker.setdefault(ticker, {"outcomes": {}, "existing": existing})
        d["outcomes"][derived] = n

    conflicts, agree, disagree, to_write, already = [], 0, [], [], 0
    for ticker, d in by_ticker.items():
        if len(d["outcomes"]) > 1:
            conflicts.append((ticker, d["outcomes"]))
            continue
        derived = next(iter(d["outcomes"]))
        if d["existing"] is None:
            to_write.append((derived, ticker))
        elif d["existing"] == derived:
            agree += 1
            already += 1
        else:
            disagree.append((ticker, d["existing"], derived))

    print("=" * 70)
    print("DERIVE settled_outcome FROM fills  (no API calls)")
    print("=" * 70)
    print(f"  traded markets with a settled fill : {len(by_ticker)}")
    print(f"  already populated, and AGREE       : {agree}")
    print(f"  already populated, but DISAGREE    : {len(disagree)}")
    print(f"  missing -> would write             : {len(to_write)}")
    print(f"  conflicting fills, SKIPPED         : {len(conflicts)}")
    print()
    print(f"  outcome split of new writes: {dict(Counter(o for o, _ in to_write))}")

    if disagree:
        print("\n  !! DISAGREEMENTS -- investigate before trusting either source:")
        for t, e, d in disagree[:10]:
            print(f"     {t:42s} db={e:4s} derived={d}")
        if len(disagree) > 10:
            print(f"     ... and {len(disagree) - 10} more")
    if conflicts:
        print("\n  !! CONFLICTING FILLS on one market (both outcomes implied):")
        for t, o in conflicts[:10]:
            print(f"     {t:42s} {o}")
        if len(conflicts) > 10:
            print(f"     ... and {len(conflicts) - 10} more")

    if not args.apply:
        print("\n  DRY RUN -- nothing written. Re-run with --apply to commit.")
        con.close()
        return 0

    if disagree:
        print("\n  REFUSING TO WRITE: disagreements present. Resolve them first.")
        con.close()
        return 1

    con.executemany("UPDATE markets SET settled_outcome = ? WHERE ticker = ?", to_write)
    con.commit()
    print(f"\n  WROTE {len(to_write)} rows. ({already} already correct, left alone.)")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
