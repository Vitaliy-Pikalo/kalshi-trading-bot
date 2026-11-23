"""WORKSTREAM A -- re-score every settled fill net of Kalshi fees. READ-ONLY.

`paper_sim.py` models no fee. This script does not change that; it re-scores
what is already in the db so we can see the size of the hole before touching
the daemon's code path. Safe to run while the daemon is up (opens sqlite in
read-only URI mode; WAL handles the concurrency).

usage:
    .venv\\Scripts\\python.exe -m src.rescore_fees
    .venv\\Scripts\\python.exe -m src.rescore_fees --min-fill-id 188      # v4 only
    .venv\\Scripts\\python.exe -m src.rescore_fees --min-fill-id 188 --maker
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict

from src.fees import kalshi_fee_usd

DEFAULT_DB = "data/poker_quant.db"

SQL = """
SELECT f.id, f.ticker, f.ts, f.side, f.price_cents, f.contracts,
       f.cost_usd, f.realized_pnl, m.series_ticker, m.strike_type
FROM fills f
LEFT JOIN markets m ON f.ticker = m.ticker
WHERE f.realized_pnl IS NOT NULL AND f.id >= ?
ORDER BY f.id
"""


def bucket_of(px: int) -> str:
    if px < 10:
        return "0-10c"
    if px < 30:
        return "10-30c"
    if px < 70:
        return "30-70c"
    if px < 90:
        return "70-90c"
    return "90-100c"


def pct(num: float, den: float) -> str:
    return f"{num / den * 100:+7.2f}%" if den else "    n/a"


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-score settled fills net of Kalshi fees.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--min-fill-id", type=int, default=0)
    ap.add_argument("--maker", action="store_true",
                    help="assume every order was a resting maker order (25%% of taker fee). "
                         "This is the OPTIMISTIC bound -- the daemon crosses the spread, so "
                         "taker is the realistic assumption.")
    args = ap.parse_args()

    try:
        con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    except sqlite3.OperationalError as exc:
        print(f"ERROR: cannot open {args.db}: {exc}", file=sys.stderr)
        return 1
    rows = con.execute(SQL, (args.min_fill_id,)).fetchall()
    con.close()

    if not rows:
        print(f"no settled fills with id >= {args.min_fill_id}")
        return 1

    tot_cost = tot_gross = tot_fee_t = tot_fee_m = 0.0
    n_contracts = 0
    by_bucket: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])  # n, cost, gross, fee
    by_series: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])

    for (_id, _tk, _ts, _side, px, qty, cost, pnl, series, _st) in rows:
        fee_t = kalshi_fee_usd(px, qty, maker=False)
        fee_m = kalshi_fee_usd(px, qty, maker=True)
        fee = fee_m if args.maker else fee_t
        tot_cost += cost
        tot_gross += pnl
        tot_fee_t += fee_t
        tot_fee_m += fee_m
        n_contracts += qty
        for d, key in ((by_bucket, bucket_of(px)), (by_series, series or "unknown")):
            d[key][0] += 1
            d[key][1] += cost
            d[key][2] += pnl
            d[key][3] += fee

    fee_used = tot_fee_m if args.maker else tot_fee_t
    net = tot_gross - fee_used

    print("=" * 78)
    print(f"FEE RE-SCORE  |  fills id >= {args.min_fill_id}  |  "
          f"assumption: {'MAKER (optimistic)' if args.maker else 'TAKER (realistic)'}")
    print("=" * 78)
    print(f"  settled fills      {len(rows):>10}")
    print(f"  contracts          {n_contracts:>10}")
    print(f"  capital deployed   ${tot_cost:>9.2f}")
    print()
    print(f"  gross pnl          ${tot_gross:>9.2f}   ROI {pct(tot_gross, tot_cost)}   <-- what the bot reports today")
    print(f"  fees (taker)       ${tot_fee_t:>9.2f}")
    print(f"  fees (maker)       ${tot_fee_m:>9.2f}")
    print(f"  NET pnl            ${net:>9.2f}   ROI {pct(net, tot_cost)}   <-- reality")
    print()
    print(f"  fee as % of capital deployed  {tot_fee_t / tot_cost * 100:5.2f}% taker / "
          f"{tot_fee_m / tot_cost * 100:5.2f}% maker")
    print(f"  avg fee per order             ${tot_fee_t / len(rows):.4f} taker")
    if tot_gross > 0:
        print(f"  fees / gross pnl              {tot_fee_t / tot_gross:.1f}x  "
              f"<-- >1x means fees eat the entire edge")
    print()

    for title, d in (("BY ENTRY PRICE BUCKET", by_bucket), ("BY SERIES", by_series)):
        print("=" * 78)
        print(title + ("  (fee-adjusted)" if True else ""))
        print("=" * 78)
        print(f"  {'key':<10} {'n':>5} {'cost':>9} {'gross':>9} {'fee':>8} "
              f"{'net':>9} {'gross ROI':>10} {'net ROI':>10}")
        print("  " + "-" * 74)
        order = sorted(d.items(), key=lambda kv: -kv[1][1])
        for key, (n, cost, gross, fee) in order:
            print(f"  {key:<10} {int(n):>5} ${cost:>8.2f} ${gross:>8.2f} ${fee:>7.2f} "
                  f"${gross - fee:>8.2f} {pct(gross, cost):>10} {pct(gross - fee, cost):>10}")
        print()

    print("=" * 78)
    print("NOTE: Kalshi charges no fee at settlement, only on the opening order.")
    print("      This book holds every position to settlement, so the entry fee")
    print("      above is the full transaction cost. If the bot ever starts")
    print("      closing early, exit fees must be added.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
