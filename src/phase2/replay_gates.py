"""Offline gate replay: score any preset against the full prediction history.

WHY
---
The live loop sees ~12 tradeable markets per 5-minute cycle and places ~6 fills a
day. Judging one preset takes months, which is how `range_v3` came to be selected
off n=115 fills computed gross of fees.

resolve_from_spot.py changed that: 269,806 markets now have a known outcome. Every
prediction in the db can be scored as "what would this gate have filled, and what
would it have earned NET OF FEES." That turns a two-month experiment into seconds.

TWO STAGES
----------
  --materialize   builds data/replay_dataset.csv once (prediction + price + volume
                  + outcome, one row per prediction). Slow the first time.
  (default)       sweeps a grid of gate configs over that csv.

WHAT THIS IS NOT
----------------
Replay is idealised and must not be read as a backtest:
  - it assumes you fill at the recorded ask. No queue position, no partial fills.
  - it ignores market impact. Fine at 1 contract, not fine at scale.
  - model_version changed over the period, so early and late rows are not the
    same model.
  - 269k markets are NOT 269k independent observations. Every BTC range market on
    a given day resolves off one price path. Effective n is closer to the number
    of distinct settlement events -- thousands. The script reports that number.

THE GUARDRAIL
-------------
A fast sweep is a fast way to overfit. So this ships with, and will not run
without:
  1. a TIME-BASED holdout. Configs are ranked on the training period only; the
     holdout number is computed once, for the winner, and reported separately.
  2. a trial counter, with the Bailey/Borwein/Lopez de Prado MinBTL bound printed
     next to it. If you sweep 200 configs, you are told what sample length that
     would require to believe a Sharpe-1 result.

Read the holdout column. The training column is decoration.

usage:
    .venv\\Scripts\\python.exe -m src.phase2.replay_gates --materialize
    .venv\\Scripts\\python.exe -m src.phase2.replay_gates
    .venv\\Scripts\\python.exe -m src.phase2.replay_gates --holdout-frac 0.4
"""
from __future__ import annotations

import argparse
import csv
import itertools
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from src.fees import kalshi_fee_usd

DEFAULT_DB = "data/poker_quant.db"
DATASET = Path("data/replay_dataset.csv")

MATERIALIZE_SQL = """
SELECT p.id, p.ticker, p.ts, p.predicted_prob, p.market_implied_prob, p.edge,
       p.model_version, m.strike_type, m.settled_outcome, m.series_ticker,
       s.yes_ask, s.no_ask, s.volume
FROM predictions p
JOIN markets m ON p.ticker = m.ticker
LEFT JOIN snapshots s ON s.id = (
    SELECT s2.id FROM snapshots s2
    WHERE s2.ticker = p.ticker AND s2.ts <= p.ts
    ORDER BY s2.ts DESC LIMIT 1
)
WHERE m.settled_outcome IS NOT NULL
ORDER BY p.ts
"""

COLS = ["id", "ticker", "ts", "predicted_prob", "market_implied_prob", "edge",
        "model_version", "strike_type", "settled_outcome", "series_ticker",
        "yes_ask", "no_ask", "volume"]


def materialize(db: str) -> int:
    con = sqlite3.connect(db, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    print("ensuring composite index on snapshots(ticker, ts) -- one-time, may take a few minutes")
    con.execute("CREATE INDEX IF NOT EXISTS ix_snap_ticker_ts ON snapshots(ticker, ts)")
    con.commit()
    print("running the join (this is the slow part) ...")
    cur = con.execute(MATERIALIZE_SQL)
    DATASET.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with DATASET.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        while True:
            batch = cur.fetchmany(20000)
            if not batch:
                break
            w.writerows(batch)
            n += len(batch)
            print(f"  {n} rows", end="\r", flush=True)
    con.close()
    print(f"\nwrote {n} rows -> {DATASET}")
    return 0


def load() -> list[dict]:
    rows = []
    with DATASET.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                edge = float(r["edge"])
                ya = int(float(r["yes_ask"])) if r["yes_ask"] else 0
                na = int(float(r["no_ask"])) if r["no_ask"] else 0
            except (TypeError, ValueError):
                continue
            rows.append({
                "ticker": r["ticker"], "ts": r["ts"], "edge": edge,
                "strike_type": r["strike_type"], "outcome": r["settled_outcome"],
                "series": r["series_ticker"], "yes_ask": ya, "no_ask": na,
                "volume": int(float(r["volume"])) if r["volume"] else 0,
            })
    return rows


def simulate(rows, cfg) -> dict:
    """Equal-weight, 1 contract per fill. Returns net-of-fee stats."""
    last_fill_ts: dict[str, str] = {}
    n = wins = 0
    cost = gross = fee_tot = 0.0
    for r in rows:
        if cfg["allowed_strike_types"] and r["strike_type"] not in cfg["allowed_strike_types"]:
            continue
        if r["volume"] < cfg["min_volume"]:
            continue
        e = r["edge"]
        if math.isnan(e) or abs(e) < cfg["min_edge"] or abs(e) > cfg["max_credible_edge"]:
            continue
        side = "yes" if e > 0 else "no"
        px = r["yes_ask"] if side == "yes" else r["no_ask"]
        if px <= 0 or px >= 100:
            continue
        if px < cfg["min_price_cents"] or px > cfg["max_price_cents"]:
            continue
        # dedup: one fill per ticker per window (ts strings sort chronologically)
        prev = last_fill_ts.get(r["ticker"])
        if prev is not None and r["ts"] < prev:
            continue
        fee = kalshi_fee_usd(px, 1)
        if cfg["fee_aware"]:
            if abs(e) < fee / (px / 100.0) + cfg["fee_edge_margin"]:
                continue
        won = (r["outcome"] == side)
        n += 1
        wins += won
        cost += px / 100.0
        gross += (1.0 - px / 100.0) if won else -(px / 100.0)
        fee_tot += fee
        last_fill_ts[r["ticker"]] = _plus_window(r["ts"], cfg["dedup_window_minutes"])
    net = gross - fee_tot
    return {"n": n, "win_rate": wins / n if n else 0.0, "cost": cost,
            "gross": gross, "fee": fee_tot, "net": net,
            "net_roi": net / cost if cost else 0.0,
            "gross_roi": gross / cost if cost else 0.0}


def _plus_window(ts: str, minutes: int) -> str:
    from datetime import datetime, timedelta
    for n, fmt in ((26, "%Y-%m-%d %H:%M:%S.%f"), (19, "%Y-%m-%d %H:%M:%S")):
        try:
            return (datetime.strptime(str(ts).replace("T", " ")[:n], fmt)
                    + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            continue
    return ts


def minbtl_years(n_trials: int, sr: float = 1.0) -> float:
    return 2.0 * math.log(max(n_trials, 2)) / (sr ** 2)


def main() -> int:
    ap = argparse.ArgumentParser(description="Sweep gate configs over the full prediction history.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--materialize", action="store_true")
    ap.add_argument("--holdout-frac", type=float, default=0.30,
                    help="final fraction of the period held out. ranked on the rest.")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    if args.materialize:
        return materialize(args.db)
    if not DATASET.exists():
        print(f"{DATASET} not found -- run with --materialize first", file=sys.stderr)
        return 1

    rows = load()
    if not rows:
        print("no usable rows", file=sys.stderr)
        return 1
    rows.sort(key=lambda r: r["ts"])
    cut = int(len(rows) * (1 - args.holdout_frac))
    train, holdout = rows[:cut], rows[cut:]

    days = len({r["ts"][:10] for r in rows})
    eff_n = len({(r["ticker"][:12], r["ts"][:13]) for r in rows})
    print("=" * 92)
    print("GATE REPLAY")
    print("=" * 92)
    print(f"  rows                 {len(rows)}")
    print(f"  distinct days        {days}")
    print(f"  effective n (approx) {eff_n}   <-- markets on one day share one price path")
    print(f"  train / holdout      {len(train)} / {len(holdout)}   (split at {rows[cut]['ts'][:16]})")

    grid = {
        "min_edge": [0.02, 0.03, 0.05, 0.08],
        "max_credible_edge": [0.15, 0.25, 0.40],
        "min_price_cents": [5, 10, 40, 70],
        "max_price_cents": [80, 95],
        "min_volume": [0, 1000],
        "fee_aware": [False, True],
        "fee_edge_margin": [0.02],
        "dedup_window_minutes": [240],
    }
    keys = list(grid)
    base_combos = [dict(zip(keys, v)) for v in itertools.product(*(grid[k] for k in keys))]
    per_seg = len(base_combos)

    # Each segment gets its OWN 384-config sweep and its OWN holdout, rather than
    # one 1152-trial sweep across all three. Segments answer different questions
    # ("does daily-greater have edge?" is not "which of between/greater/both is
    # best?"), and pooling them would inflate the trial count on every answer.
    # NOTE: greater (KXBTCD/KXETHD) resolves off the SAME BTC/ETH price path as
    # between (KXBTC/KXETH). It is a different slice of the same 42 days, not an
    # independent sample. The trial budget already spent applies here too.
    SEGMENTS = [
        ("between (range)", ["between"]),
        ("greater (daily)", ["greater"]),
        ("both", ["between", "greater"]),
    ]

    avail = {}
    for r in rows:
        avail[r["strike_type"]] = avail.get(r["strike_type"], 0) + 1
    print(f"\n  rows by strike_type  {dict(sorted(avail.items(), key=lambda kv: -kv[1]))}")
    print(f"  configs per segment  {per_seg}")
    print(f"  MinBTL at Sharpe 1   {minbtl_years(per_seg):.1f} years needed to believe "
          f"the best of {per_seg} trials")
    print(f"  you have             {days / 365.0:.2f} years")
    print("  -> the TRAIN column is not evidence. Read HOLDOUT.")

    summary = []
    for seg_name, seg_types in SEGMENTS:
        combos = [dict(c, allowed_strike_types=list(seg_types)) for c in base_combos]
        results = []
        for c in combos:
            tr = simulate(train, c)
            if tr["n"] < 30:
                continue
            results.append((c, tr))
        results.sort(key=lambda x: -x[1]["net_roi"])

        print(f"\n{'=' * 92}")
        print(f"SEGMENT: {seg_name}   ({len(results)}/{per_seg} configs reached n>=30 on train)")
        print("=" * 92)
        if not results:
            print("  no config produced enough training fills -- nothing to rank.")
            summary.append((seg_name, None, None, None, None))
            continue
        print(f"  {'edge':>5} {'maxE':>5} {'px':>8} {'vol':>5} {'fee':>4} | "
              f"{'tr n':>5} {'tr netROI':>10} | {'ho n':>5} {'ho netROI':>10}  {'degrade':>9}")
        print("  " + "-" * 88)
        for c, tr in results[:args.top]:
            ho = simulate(holdout, c)
            print(f"  {c['min_edge']:>5.2f} {c['max_credible_edge']:>5.2f} "
                  f"{str(c['min_price_cents']) + '-' + str(c['max_price_cents']):>8} "
                  f"{c['min_volume']:>5} {'Y' if c['fee_aware'] else 'n':>4} | "
                  f"{tr['n']:>5} {tr['net_roi']:>9.2%} | "
                  f"{ho['n']:>5} {ho['net_roi']:>9.2%}  {ho['net_roi'] - tr['net_roi']:>+8.2%}")
        best, btr = results[0]
        bho = simulate(holdout, best)
        summary.append((seg_name, btr, bho, best, len(results)))
        # best config's holdout, split by series -- KXBTCD and KXETHD behave very
        # differently (+6.74% vs -26.68% net all-time) and pooling hides that
        by_series: dict[str, list] = defaultdict(list)
        for r in holdout:
            by_series[r["series"]].append(r)
        parts = []
        for series, srows in sorted(by_series.items(), key=lambda kv: -len(kv[1])):
            st = simulate(srows, best)
            if st["n"]:
                parts.append(f"{series} n={st['n']} {st['net_roi']:+.2%}")
        if parts:
            print("  holdout by series: " + " | ".join(parts))

    print(f"\n{'=' * 92}")
    print("THE HONEST NUMBERS  (holdout only)")
    print("=" * 92)
    print(f"  {'segment':<18} {'tr n':>6} {'tr netROI':>11} | {'ho n':>6} {'ho netROI':>11}  {'degrade':>9}")
    print("  " + "-" * 74)
    for seg_name, btr, bho, best, _ in summary:
        if btr is None:
            print(f"  {seg_name:<18} {'--':>6} {'--':>11} | {'--':>6} {'--':>11}")
            continue
        print(f"  {seg_name:<18} {btr['n']:>6} {btr['net_roi']:>10.2%} | "
              f"{bho['n']:>6} {bho['net_roi']:>10.2%}  {bho['net_roi'] - btr['net_roi']:>+8.2%}")
    print()
    print(f"  Each row is the best of {per_seg} trials on {days / 365.0:.2f} years of data;")
    print(f"  MinBTL says {minbtl_years(per_seg):.1f} years would be needed to believe it.")
    print("  A large train->holdout gap IS the overfitting, measured.")
    print("  greater and between share one BTC/ETH price path -- these are slices of")
    print("  the same 42 days, NOT independent samples.")
    print("  Treat a positive holdout number as a hypothesis to forward-test, not a result.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
