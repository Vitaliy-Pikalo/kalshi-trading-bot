"""fixed comparison: elo vs a PRE-MATCH market price, plus a shrinkage fix.

corrects two errors in tennis_score.py:

  1. `px_late` was the last traded bucket -- which is AFTER the match is
     decided. brier 0.0072, accuracy 99.7%. that is the answer key, not a
     forecast. every price used here is now taken at least --hours-before
     hours ahead of close_ts, and markets without such a bucket are dropped.

  2. elo's probabilities are too extreme in both tails (+13pp on underdogs,
     -12pp on favourites). that is a calibration defect, not missing
     knowledge. p' = 0.5 + k*(p - 0.5) with a single k fitted on train.

trial discipline: ONE parameter, fitted on the train half, reported on the
holdout half. k is also reported per-tour but the headline number is the
single pooled k -- two extra fits would be two extra trials.

run:
    .venv\\Scripts\\python.exe -m src.phase2.tennis_score2
    .venv\\Scripts\\python.exe -m src.phase2.tennis_score2 --hours-before 6
"""
from __future__ import annotations

import argparse
import math
import sys

import pandas as pd

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

SCORED = "data/tennis_scored.csv"     # ticker, tour, series, close_ts, y, p_elo
PRICES = "data/tennis_prices_15m.csv"


def brier(p, y):
    return (p - y) ** 2


def logloss(p, y, eps=1e-6):
    p = min(max(p, eps), 1 - eps)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def score(ps, ys):
    n = len(ys)
    return (sum(brier(p, y) for p, y in zip(ps, ys)) / n,
            sum(logloss(p, y) for p, y in zip(ps, ys)) / n,
            sum(int((p >= 0.5) == bool(y)) for p, y in zip(ps, ys)) / n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours-before", type=float, default=3.0,
                    help="price must be at least this many hours before close")
    ap.add_argument("--split", default=None,
                    help="train/holdout cut date; default = median close_ts")
    args = ap.parse_args()

    df = pd.read_csv(SCORED, parse_dates=["close_ts"])
    px = pd.read_csv(PRICES, parse_dates=["close_ts", "bucket_ts"])
    print(f"scored markets {len(df)}   price buckets {len(px)}")

    cutoff = px.close_ts - pd.Timedelta(hours=args.hours_before)
    pre = px[px.bucket_ts <= cutoff]
    print(f"buckets at least {args.hours_before}h before close: {len(pre)} "
          f"({100.0 * len(pre) / len(px):.1f}%)")

    pre = pre.sort_values(["ticker", "bucket_ts"])
    last_pre = pre.groupby("ticker").last()["vwap_yes"].rename("px_pre")
    n_before = len(df)
    df = df.merge(last_pre, on="ticker", how="inner").dropna(subset=["px_pre"])
    print(f"markets with a usable pre-match price: {len(df)} "
          f"(dropped {n_before - len(df)})")
    if len(df) < 100:
        print("too few markets to say anything")
        return 1

    split = pd.Timestamp(args.split) if args.split else df.close_ts.median()
    tr = df[df.close_ts <= split]
    ho = df[df.close_ts > split]
    print(f"split at {split}   train {len(tr)}   holdout {len(ho)}")

    # fit shrinkage k on TRAIN only
    best_k, best_b = 1.0, None
    for i in range(0, 101):
        k = i / 100.0
        b = sum(brier(0.5 + k * (p - 0.5), y) for p, y in zip(tr.p_elo, tr.y)) / len(tr)
        if best_b is None or b < best_b:
            best_k, best_b = k, b
    print(f"\nshrinkage k fitted on train: {best_k:.2f}  (train brier {best_b:.4f})")
    print("  k=1 means no change; k<1 means elo was too confident")

    def shrunk(p):
        return 0.5 + best_k * (p - 0.5)

    print("\n" + "=" * 76)
    print(f"HOLDOUT ONLY  (n={len(ho)}, price {args.hours_before}h+ before close)")
    print("=" * 76)
    print(f"  {'forecaster':<22}{'brier':>10}{'logloss':>10}{'acc':>9}")
    rows = [("elo raw", list(ho.p_elo)),
            (f"elo shrunk k={best_k:.2f}", [shrunk(p) for p in ho.p_elo]),
            ("market pre-match", list(ho.px_pre)),
            ("always 0.50", [0.5] * len(ho))]
    res = {}
    for label, ps in rows:
        b, ll, acc = score(ps, list(ho.y))
        res[label] = b
        print(f"  {label:<22}{b:>10.4f}{ll:>10.4f}{acc:>9.3f}")

    mb = res["market pre-match"]
    sb = res[f"elo shrunk k={best_k:.2f}"]
    print("\n" + "=" * 76)
    print("THE ANSWER  (holdout)")
    print("=" * 76)
    print(f"  shrunk elo {sb:.4f}  vs  market {mb:.4f}   diff {sb - mb:+.4f}")
    if sb < mb:
        print("  -> elo beats the pre-match market. a trading sim is now justified.")
    else:
        print("  -> market still wins. elo v0 has no edge even after recalibration.")
        print(f"     the gap to close is {sb - mb:.4f} brier.")

    print("\n  per-tour (holdout, using the pooled k -- not refitted):")
    for tour, g in ho.groupby("tour"):
        if len(g) < 50:
            continue
        be, _, _ = score([shrunk(p) for p in g.p_elo], list(g.y))
        bm, _, _ = score(list(g.px_pre), list(g.y))
        print(f"    {tour.upper()}  n={len(g):>4}  elo {be:.4f}  market {bm:.4f}  "
              f"{'ELO' if be < bm else 'market'}")

    print("\n  calibration after shrinkage (holdout):")
    ho = ho.copy()
    ho["ps"] = [shrunk(p) for p in ho.p_elo]
    ho["bin"] = (ho.ps * 10).astype(int).clip(0, 9)
    print(f"    {'bucket':<12}{'n':>6}{'predicted':>11}{'actual':>9}{'gap':>9}")
    for b, g in ho.groupby("bin"):
        print(f"    [{b / 10:.1f},{(b + 1) / 10:.1f})  {len(g):>6}"
              f"{g.ps.mean():>11.3f}{g.y.mean():>9.3f}"
              f"{g.y.mean() - g.ps.mean():>+9.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
