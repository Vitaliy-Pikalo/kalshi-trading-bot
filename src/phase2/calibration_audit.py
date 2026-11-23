"""WORKSTREAM B -- is the calibration dataset selection-biased? READ-ONLY.

`extract_calibration_data.py` pulls EVERY prediction for every settled market:

    FROM predictions p JOIN markets m ON p.ticker = m.ticker
    WHERE m.settled_outcome IS NOT NULL

with the stated rationale "more is better". The poller writes a prediction
roughly every 60s across a market's entire life, so a BTC daily range market
contributes hundreds of rows -- and most of them are taken late, when spot is
far from the boundary and the outcome is already effectively determined.

That matters because `iso_v1_*.pkl` is fitted on this pooled population and
then applied at the moments the bot actually TRADES, which are by construction
the uncertain ones. If calibration quality varies with time-to-close, the fit
is dominated by the easy end of the distribution and is wrong where it counts.

Two pieces of circumstantial evidence say it does:
  - 48.7% of all predictions sit in [0.00,0.05) and 33.1% in [0.95,1.01).
    82% at the extremes is not a model with opinions, it is a model mostly
    describing already-resolved states.
  - the session-7 pooled fit measured [0.60,0.80) as 70% -> 98.3% actual
    (massive UNDERconfidence), but v4's 158 live fills at [0.95,1.01) won
    82.3% against 98% expected (-15.7pp OVERconfidence). Opposite signs.

This script does not refit anything. It measures calibration under several
sampling schemes so we can see whether the pooled fit is measuring the same
thing the bot faces. Safe to run while the daemon is up (read-only URI, WAL).

usage:
    .venv\\Scripts\\python.exe -m src.phase2.calibration_audit
    .venv\\Scripts\\python.exe -m src.phase2.calibration_audit --strike-type between
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict

DEFAULT_DB = "data/poker_quant.db"

BUCKETS = [(0.00, 0.05), (0.05, 0.20), (0.20, 0.40), (0.40, 0.60),
           (0.60, 0.80), (0.80, 0.95), (0.95, 1.01)]

# hours before close. ordered widest lead -> tightest.
LEAD_STRATA = [("> 12h", 12.0, 1e9), ("6-12h", 6.0, 12.0), ("2-6h", 2.0, 6.0),
               ("0.5-2h", 0.5, 2.0), ("< 0.5h", -1e9, 0.5)]

SQL = """
SELECT p.ticker, p.ts, p.predicted_prob, m.close_ts,
       CASE WHEN m.settled_outcome='yes' THEN 1 ELSE 0 END AS y,
       m.strike_type
FROM predictions p
JOIN markets m ON p.ticker = m.ticker
WHERE m.settled_outcome IS NOT NULL
  AND m.close_ts IS NOT NULL
  AND (? = '' OR m.strike_type = ?)
ORDER BY p.ticker, p.ts
"""

FILL_SQL = """
SELECT f.ticker, f.ts FROM fills f
WHERE f.realized_pnl IS NOT NULL AND f.id >= ?
"""


def parse_ts(s):
    if s is None:
        return None
    s = str(s).replace("T", " ")
    for fmt_len in (26, 19, 16):
        try:
            from datetime import datetime
            return datetime.strptime(s[:fmt_len],
                                     "%Y-%m-%d %H:%M:%S.%f" if fmt_len == 26
                                     else ("%Y-%m-%d %H:%M:%S" if fmt_len == 19
                                           else "%Y-%m-%d %H:%M"))
        except ValueError:
            continue
    return None


def bucket_idx(p: float) -> int:
    for i, (lo, hi) in enumerate(BUCKETS):
        if lo <= p < hi:
            return i
    return len(BUCKETS) - 1


def report(name: str, rows: list[tuple[float, int]], note: str = "") -> None:
    """rows = [(predicted_prob, actual_outcome), ...]"""
    if not rows:
        print(f"\n--- {name}: no rows ---")
        return
    agg = defaultdict(lambda: [0, 0, 0.0])  # n, wins, sum_pred
    brier = 0.0
    for p, y in rows:
        b = agg[bucket_idx(p)]
        b[0] += 1
        b[1] += y
        b[2] += p
        brier += (p - y) ** 2
    n_tot = len(rows)
    ece = 0.0
    print(f"\n{'=' * 78}")
    print(f"{name}   n={n_tot}   {note}")
    print(f"{'=' * 78}")
    print(f"  {'bucket':<14} {'n':>7} {'share':>7} {'predicted':>10} {'actual':>8} {'gap':>8}")
    print("  " + "-" * 60)
    for i, (lo, hi) in enumerate(BUCKETS):
        if i not in agg:
            continue
        n, wins, sp = agg[i]
        pred = sp / n
        act = wins / n
        ece += (n / n_tot) * abs(pred - act)
        flag = "  <<" if abs(pred - act) > 0.10 else ""
        print(f"  [{lo:.2f},{hi:.2f})  {n:>7} {n / n_tot * 100:>6.1f}% "
              f"{pred:>9.1%} {act:>7.1%} {act - pred:>+7.1%}{flag}")
    print("  " + "-" * 60)
    print(f"  ECE = {ece:.4f}     Brier = {brier / n_tot:.4f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit the calibration dataset for selection bias.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--strike-type", default="", help="e.g. between. empty = all")
    ap.add_argument("--min-fill-id", type=int, default=188, help="v4 cutoff for the fill-matched view")
    args = ap.parse_args()

    try:
        con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    except sqlite3.OperationalError as exc:
        print(f"ERROR: cannot open {args.db}: {exc}", file=sys.stderr)
        return 1
    raw = con.execute(SQL, (args.strike_type, args.strike_type)).fetchall()
    fills = con.execute(FILL_SQL, (args.min_fill_id,)).fetchall()
    con.close()

    if not raw:
        print("no rows -- check --strike-type / db path")
        return 1

    # group by market, compute lead time
    per_market: dict[str, list[tuple]] = defaultdict(list)
    all_rows, by_lead = [], defaultdict(list)
    for ticker, ts, p, close_ts, y, _st in raw:
        t, c = parse_ts(ts), parse_ts(close_ts)
        lead = (c - t).total_seconds() / 3600.0 if (t and c) else None
        per_market[ticker].append((t, p, y, lead))
        all_rows.append((p, y))
        if lead is not None:
            for label, lo, hi in LEAD_STRATA:
                if lo <= lead < hi:
                    by_lead[label].append((p, y))
                    break

    firsts, lasts = [], []
    for _tk, recs in per_market.items():
        recs.sort(key=lambda r: (r[0] is None, r[0]))
        firsts.append((recs[0][1], recs[0][2]))
        lasts.append((recs[-1][1], recs[-1][2]))

    # fill-matched: prediction nearest in time to each actual fill
    fill_rows = []
    fills_by_ticker = defaultdict(list)
    for tk, fts in fills:
        fills_by_ticker[tk].append(parse_ts(fts))
    for tk, ftss in fills_by_ticker.items():
        recs = [r for r in per_market.get(tk, []) if r[0] is not None]
        if not recs:
            continue
        for fts in ftss:
            if fts is None:
                continue
            best = min(recs, key=lambda r: abs((r[0] - fts).total_seconds()))
            fill_rows.append((best[1], best[2]))

    print("#" * 78)
    print("# CALIBRATION AUDIT -- does the fitting population match the trading population?")
    print(f"# db={args.db}  strike_type={args.strike_type or 'ALL'}")
    print("#" * 78)
    print(f"\n  markets with a settled outcome : {len(per_market)}")
    print(f"  prediction rows (what the fit sees) : {len(all_rows)}")
    print(f"  rows per market : {len(all_rows) / max(len(per_market), 1):.1f}"
          "   <-- effective n is MARKETS, not rows")

    report("SCHEME 1 -- ALL PREDICTIONS (what extract_calibration_data.py does today)",
           all_rows, "this is the population iso_v1_*.pkl was fitted on")
    report("SCHEME 2 -- FIRST prediction per market (closest to a real decision point)",
           firsts, "one row per market, no autocorrelation")
    report("SCHEME 3 -- LAST prediction per market (outcome nearly determined)",
           lasts, "if this looks great and scheme 2 looks bad, the pooled fit is contaminated")
    report(f"SCHEME 4 -- prediction nearest each actual FILL (id >= {args.min_fill_id})",
           fill_rows, "the population the model is ACTUALLY applied to")

    print(f"\n{'#' * 78}")
    print("# CALIBRATION BY TIME-TO-CLOSE  (the mechanism)")
    print(f"{'#' * 78}")
    for label, _lo, _hi in LEAD_STRATA:
        if by_lead.get(label):
            report(f"LEAD {label}", by_lead[label],
                   f"{len(by_lead[label]) / len(all_rows) * 100:.1f}% of the pooled fitting set")

    print(f"\n{'#' * 78}")
    print("# HOW TO READ THIS")
    print("#" * 78)
    print("# If ECE falls monotonically as lead time shrinks, the model looks well")
    print("# calibrated mostly because most rows are taken when the answer is already")
    print("# known. Whichever lead stratum holds the largest row share is what the")
    print("# pooled fit is really measuring -- and if that is not the stratum the bot")
    print("# trades in, iso_v1_*.pkl is fitted on the wrong population.")
    print("#")
    print("# Scheme 2 vs Scheme 3 is the decisive comparison. Scheme 4 is the ground")
    print("# truth: it is the only view that matches where money actually changes hands.")
    print("#" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
