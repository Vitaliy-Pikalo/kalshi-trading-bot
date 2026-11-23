"""phase 2.0.1b — backfill Market.settled_outcome from kalshi API.

the settler only updates Fill.realized_pnl. it never writes back to
Market.settled_outcome. that means calibration analysis can't use the
~13k predictions in the db (the JOIN to markets returns 0 rows).

this script fixes that. for every market in our db where close_ts < now
AND settled_outcome IS NULL, it calls kalshi's get_market endpoint and
populates settled_outcome based on the `result` field.

run:
    .venv\\Scripts\\python.exe -m src.phase2.backfill_market_outcomes
    .venv\\Scripts\\python.exe -m src.phase2.backfill_market_outcomes --dry-run

safe to run while daemon is up (WAL handles concurrent writes; this
script only updates Market rows, daemon writes Fill/Snapshot/Prediction).

design notes:
- batches commits every 50 rows for crash safety
- self-throttled via kalshi client (~8 req/sec)
- handles 3 result states from kalshi:
    - 'yes' / 'no' -> populate settled_outcome
    - '' or other  -> still pending, leave NULL
- prints progress every 50 markets so user can ctrl-c safely
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from src.utils import setup_utf8_stdout, utcnow_naive

setup_utf8_stdout()

from sqlalchemy import select  # noqa: E402

from src.db import Market, session_scope  # noqa: E402
from src.kalshi.client import KalshiClient  # noqa: E402


COMMIT_EVERY = 50
PROGRESS_EVERY = 1  # print on every fetch so we can see exactly where it hangs


def backfill(dry_run: bool = False, verbose: bool = True) -> dict:
    print("step 1: getting current time...", flush=True)
    now = utcnow_naive()
    stats = {
        "candidates": 0,
        "fetched": 0,
        "set_yes": 0,
        "set_no": 0,
        "still_pending": 0,
        "errors": 0,
    }

    print("step 2: initializing kalshi client (loads RSA key, sets up auth)...", flush=True)
    with KalshiClient() as client:
        print("step 3: kalshi client ready", flush=True)

        print("step 4: opening db session + querying candidates...", flush=True)
        with session_scope() as s:
            candidates = s.execute(
                select(Market)
                .where(Market.close_ts < now)
                .where(Market.settled_outcome.is_(None))
                .order_by(Market.close_ts.asc())
            ).scalars().all()

            stats["candidates"] = len(candidates)
            print(f"step 5: found {len(candidates)} candidates (closed + unsettled in db)", flush=True)

            if not candidates:
                return stats

            print(f"step 6: starting fetch loop ({len(candidates)} api calls @ ~8 req/sec = ~{len(candidates) / 8:.0f}s)...", flush=True)

            for i, m in enumerate(candidates, 1):
                try:
                    detail = client.get_market(m.ticker)
                    result = (detail.get("market", {}).get("result") or "").lower().strip()
                    stats["fetched"] += 1
                except Exception as e:
                    print(f"  ! [{i}/{len(candidates)}] {m.ticker}: error fetching: {e}", flush=True)
                    stats["errors"] += 1
                    continue

                if result == "yes":
                    if not dry_run:
                        m.settled_outcome = "yes"
                    stats["set_yes"] += 1
                elif result == "no":
                    if not dry_run:
                        m.settled_outcome = "no"
                    stats["set_no"] += 1
                else:
                    stats["still_pending"] += 1

                if verbose and i % PROGRESS_EVERY == 0:
                    res_display = result if result else "(pending)"
                    print(f"  [{i:4d}/{len(candidates)}] {m.ticker:40s} -> {res_display}", flush=True)

                if i % COMMIT_EVERY == 0:
                    if not dry_run:
                        s.commit()
                    print(
                        f"  --- progress: yes={stats['set_yes']} no={stats['set_no']} "
                        f"pending={stats['still_pending']} err={stats['errors']} ---",
                        flush=True,
                    )

            if not dry_run:
                s.commit()

    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="don't write")
    args = parser.parse_args()

    print("=" * 60)
    print("PHASE 2.0.1b - BACKFILL MARKET OUTCOMES")
    if args.dry_run:
        print("(DRY RUN - no db writes)")
    print("=" * 60)
    print()

    stats = backfill(dry_run=args.dry_run)

    print()
    print("=" * 60)
    print("RESULTS")
    print("=" * 60)
    for k, v in stats.items():
        print(f"  {k:20s} {v}")
    print()
    if stats["set_yes"] + stats["set_no"] > 0:
        print(f"  -> set settled_outcome on {stats['set_yes'] + stats['set_no']} markets")
    if stats["still_pending"] > 0:
        print(f"  -> {stats['still_pending']} still pending (no result yet on kalshi)")
    print()
    print("next: re-run _phase2_data_extract.bat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
