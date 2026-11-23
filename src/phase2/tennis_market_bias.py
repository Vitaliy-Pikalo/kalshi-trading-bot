"""is the tennis market's PRICE biased? no model involved.

elo v0 lost to the pre-match market by 0.083 brier, so it is dead as a
signal. but the same data answers a different question that needs no model:
are kalshi's tennis prices themselves mispriced in a stable way?

an exploratory pass on 1,138 markets showed the classic favourite-longshot
shape -- longshots overpriced, heavy favourites underpriced. that pass had
three problems, all fixed here:

  1. it used only the 1,139 elo-ratable markets. the bias question needs no
     elo, so this runs on all 2,724.
  2. it quoted the best 2 of 20 cells with no holdout. here the split is by
     date, and holdout is reported separately.
  3. no error bars. bootstrap CIs on every ROI.

the two headline strategies are specified BEFORE looking at the split
(buy NO under 0.30, buy YES over 0.80) because that is the direction the
favourite-longshot literature predicts -- not because this data picked them.

fees are the kalshi taker fee, rounded up per order, so a 1-contract trade
pays the full round-up. that is ~4% of stake on average and is the bar any
bias must clear.

run:
    .venv\\Scripts\\python.exe -m src.phase2.tennis_market_bias
    .venv\\Scripts\\python.exe -m src.phase2.tennis_market_bias --contracts 20
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys

import pandas as pd

from src.kalshi.client import KalshiClient
from src.utils import setup_utf8_stdout

setup_utf8_stdout()

MARKETS = "data/tennis_markets.csv"
PRICES = "data/tennis_prices_15m.csv"
TIMES = "data/tennis_match_times.csv"


def fee(price: float, contracts: int) -> float:
    """kalshi taker fee: roundup(0.07 * C * P * (1-P)) to the cent."""
    return math.ceil(0.07 * contracts * price * (1 - price) * 100) / 100.0


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i: i + size]


def ensure_times(tickers: list[str]) -> pd.DataFrame:
    have = pd.DataFrame()
    if os.path.exists(TIMES):
        have = pd.read_csv(TIMES)
    missing = sorted(set(tickers) - set(have.get("ticker", [])))
    print(f"match times: {len(have)} cached, {len(missing)} to fetch")
    if missing:
        rows = []
        with KalshiClient() as c:
            for chunk in chunked(missing, 100):
                resp = c.list_markets(status=None, limit=1000,
                                      tickers=",".join(chunk))
                for mk in resp.get("markets", []) or []:
                    rows.append({"ticker": mk.get("ticker"),
                                 "occurrence_datetime": mk.get("occurrence_datetime"),
                                 "expected_expiration_time": mk.get("expected_expiration_time")})
                print(f"  fetched {len(rows)}/{len(missing)}", end="\r")
        print()
        have = pd.concat([have, pd.DataFrame(rows)], ignore_index=True)
        have.to_csv(TIMES, index=False)
    return have


def roi(g: pd.DataFrame, side: str, contracts: int):
    """net ROI of taking `side` at the pre-match price, held to settlement."""
    pnl = cost = 0.0
    for p, y in zip(g.px_pre, g.y):
        f = fee(p, contracts)
        if side == "yes":
            stake = p * contracts + f
            ret = contracts if y else 0.0
        else:
            stake = (1 - p) * contracts + f
            ret = contracts if not y else 0.0
        pnl += ret - stake
        cost += stake
    return (pnl / cost * 100.0) if cost else 0.0


def boot(g: pd.DataFrame, side: str, contracts: int, n=400):
    if len(g) < 5:
        return (0.0, 0.0)
    rnd = random.Random(7)
    vals = []
    idx = list(range(len(g)))
    for _ in range(n):
        s = g.iloc[[rnd.choice(idx) for _ in idx]]
        vals.append(roi(s, side, contracts))
    vals.sort()
    return (vals[int(0.05 * n)], vals[int(0.95 * n)])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contracts", type=int, default=1,
                    help="order size -- the fee round-up hurts small orders most")
    ap.add_argument("--lead-minutes", type=float, default=15.0)
    args = ap.parse_args()

    mk = pd.read_csv(MARKETS, parse_dates=["close_ts"])
    mk = mk[mk.result.isin(["yes", "no"])].copy()
    mk["y"] = (mk.result == "yes").astype(int)
    px = pd.read_csv(PRICES, parse_dates=["bucket_ts"])
    print(f"markets {len(mk)}   buckets {len(px)}")

    times = ensure_times(sorted(mk.ticker.unique()))
    times["anchor"] = pd.to_datetime(times.occurrence_datetime, errors="coerce", utc=True)
    px = px.merge(times[["ticker", "anchor"]], on="ticker", how="left")
    bts = pd.to_datetime(px.bucket_ts, errors="coerce", utc=True)
    pre = px[bts <= px.anchor - pd.Timedelta(minutes=args.lead_minutes)]
    last = (pre.sort_values(["ticker", "bucket_ts"]).groupby("ticker")
            .last()["vwap_yes"].rename("px_pre"))
    d = mk.merge(last, on="ticker", how="inner").dropna(subset=["px_pre"])
    print(f"markets with a pre-match price: {len(d)}")

    acc = ((d.px_pre >= 0.5) == d.y.astype(bool)).mean()
    print(f"leak check: price-as-forecast accuracy {acc:.3f} "
          f"{'-- LEAKING, stop' if acc > 0.95 else '-- ok'}")
    if acc > 0.95:
        return 1

    split = d.close_ts.median()
    d["half"] = ["train" if t <= split else "holdout" for t in d.close_ts]
    print(f"split {split}   train {(d.half == 'train').sum()}   "
          f"holdout {(d.half == 'holdout').sum()}")

    edges = [0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.01]
    d["bin"] = pd.cut(d.px_pre, edges, right=False)
    print("\n" + "=" * 90)
    print(f"CALIBRATION BY PRICE  (contracts={args.contracts}, net of fees)")
    print("=" * 90)
    print(f"  {'bucket':<13}{'n tr':>6}{'n ho':>6}{'px':>8}{'act tr':>9}{'act ho':>9}"
          f"{'YES ho':>9}{'NO ho':>9}")
    for b, g in d.groupby("bin", observed=True):
        tr, ho = g[g.half == "train"], g[g.half == "holdout"]
        if len(ho) < 10:
            continue
        print(f"  {str(b):<13}{len(tr):>6}{len(ho):>6}{g.px_pre.mean():>8.3f}"
              f"{tr.y.mean() if len(tr) else float('nan'):>9.3f}{ho.y.mean():>9.3f}"
              f"{roi(ho, 'yes', args.contracts):>9.2f}{roi(ho, 'no', args.contracts):>9.2f}")

    print("\n" + "=" * 90)
    print("PRE-SPECIFIED STRATEGIES (direction from the literature, not this table)")
    print("=" * 90)
    strats = [("buy NO when price < 0.30", lambda x: x.px_pre < 0.30, "no"),
              ("buy YES when price > 0.80", lambda x: x.px_pre > 0.80, "yes"),
              ("buy YES always (fee baseline)", lambda x: x.px_pre > -1, "yes")]
    for name, f, side in strats:
        print(f"\n  {name}")
        for half in ("train", "holdout"):
            g = d[(d.half == half) & f(d)]
            if len(g) < 10:
                print(f"    {half:<8} n={len(g):>4}  too few")
                continue
            r = roi(g, side, args.contracts)
            lo, hi = boot(g, side, args.contracts)
            print(f"    {half:<8} n={len(g):>4}  net ROI {r:>+7.2f}%   "
                  f"90% CI [{lo:+.2f}, {hi:+.2f}]"
                  f"{'   <-- CI excludes zero' if lo > 0 else ''}")
    print("\n  a strategy is only interesting if the HOLDOUT CI excludes zero.")
    print("  prices are trade VWAPs -- being filled there is an assumption, not a fact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
