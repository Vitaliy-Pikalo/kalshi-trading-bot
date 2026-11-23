"""elo vs a genuinely PRE-MATCH market price, anchored on match time.

two failed benchmarks preceded this, both mine:
  v1 used the last traded bucket   -> brier 0.0072, acc 0.997 (post-match)
  v2 filtered to 3h before close_ts -> brier 0.0042, acc 1.000 (no-op filter)

the cause of v2: kalshi's close_ts is a SETTLEMENT deadline, not match time.
a market that traded on 17 jun has close_ts of 1 jul. so every post-match
trade sat comfortably inside "3h before close".

the fix: the market payload carries occurrence_datetime (match time), plus
open_time / expected_expiration_time / settlement_ts as fallbacks. this
script fetches them, reports how well each is populated, anchors the price
to the best one, and rescores.

a forecast benchmark whose accuracy is ~1.00 is not a forecast. the run
prints a warning if any candidate anchor still produces one.

run:
    .venv\\Scripts\\python.exe -m src.phase2.tennis_score3
    .venv\\Scripts\\python.exe -m src.phase2.tennis_score3 --lead-minutes 30
"""
from __future__ import annotations

import argparse
import math
import sys

import pandas as pd

from src.kalshi.client import KalshiClient
from src.utils import setup_utf8_stdout

setup_utf8_stdout()

SCORED = "data/tennis_scored.csv"
PRICES = "data/tennis_prices_15m.csv"
TIMES = "data/tennis_match_times.csv"
CANDIDATES = ["occurrence_datetime", "open_time", "expected_expiration_time",
              "expiration_time", "settlement_ts", "close_time"]


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


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i: i + size]


def fetch_times(tickers: list[str]) -> pd.DataFrame:
    rows = []
    with KalshiClient() as c:
        for n, chunk in enumerate(chunked(tickers, 100), 1):
            resp = c.list_markets(status=None, limit=1000, tickers=",".join(chunk))
            for mk in resp.get("markets", []) or []:
                rows.append({"ticker": mk.get("ticker"),
                             **{k: mk.get(k) for k in CANDIDATES}})
            print(f"  fetched {len(rows)}/{len(tickers)}", end="\r")
    print()
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lead-minutes", type=float, default=15.0,
                    help="price must be this many minutes before match time")
    ap.add_argument("--refetch", action="store_true")
    args = ap.parse_args()

    df = pd.read_csv(SCORED, parse_dates=["close_ts"])
    px = pd.read_csv(PRICES, parse_dates=["bucket_ts"])
    print(f"scored markets {len(df)}   price buckets {len(px)}")

    import os
    if args.refetch or not os.path.exists(TIMES):
        print("\nfetching match times from kalshi")
        times = fetch_times(sorted(df.ticker.unique()))
        times.to_csv(TIMES, index=False)
    else:
        times = pd.read_csv(TIMES)
        print(f"\nusing cached {TIMES}")

    print(f"\n{'field':<28}{'populated':>11}{'sample':>26}")
    usable = []
    for col in CANDIDATES:
        if col not in times.columns:
            continue
        s = pd.to_datetime(times[col], errors="coerce", utc=True)
        n = int(s.notna().sum())
        samp = str(s.dropna().iloc[0])[:25] if n else "-"
        print(f"  {col:<26}{n:>11}{samp:>26}")
        if n > 0.9 * len(times):
            usable.append(col)
    if not usable:
        print("\nno usable time field -- cannot build a pre-match benchmark")
        return 1

    df = df.merge(times, on="ticker", how="left")
    px = px.merge(times[["ticker"] + usable], on="ticker", how="left")

    split = df.close_ts.median()
    print(f"\ntrain/holdout split at {split}")

    for anchor in usable:
        a = pd.to_datetime(px[anchor], errors="coerce", utc=True)
        bts = pd.to_datetime(px.bucket_ts, errors="coerce", utc=True)
        pre = px[bts <= a - pd.Timedelta(minutes=args.lead_minutes)]
        if pre.empty:
            print(f"\n[{anchor}] no buckets survive the filter")
            continue
        last_pre = (pre.sort_values(["ticker", "bucket_ts"])
                    .groupby("ticker").last()["vwap_yes"].rename("px_pre"))
        d = df.merge(last_pre, on="ticker", how="inner").dropna(subset=["px_pre"])
        if len(d) < 100:
            print(f"\n[{anchor}] only {len(d)} markets survive -- too few")
            continue

        tr, ho = d[d.close_ts <= split], d[d.close_ts > split]
        if len(tr) < 50 or len(ho) < 50:
            print(f"\n[{anchor}] split too lopsided ({len(tr)}/{len(ho)})")
            continue

        best_k, best_b = 1.0, None
        for i in range(101):
            k = i / 100.0
            b = sum(brier(0.5 + k * (p - 0.5), y)
                    for p, y in zip(tr.p_elo, tr.y)) / len(tr)
            if best_b is None or b < best_b:
                best_k, best_b = k, b

        print("\n" + "=" * 74)
        print(f"ANCHOR: {anchor}   markets {len(d)}  "
              f"({100.0 * len(pre) / len(px):.0f}% of buckets kept)")
        print(f"holdout n={len(ho)}   shrinkage k={best_k:.2f} (fitted on train)")
        print("=" * 74)
        print(f"  {'forecaster':<24}{'brier':>10}{'logloss':>10}{'acc':>9}")
        out = {}
        for label, ps in [("elo raw", list(ho.p_elo)),
                          (f"elo shrunk k={best_k:.2f}",
                           [0.5 + best_k * (p - 0.5) for p in ho.p_elo]),
                          ("market pre-match", list(ho.px_pre)),
                          ("always 0.50", [0.5] * len(ho))]:
            b, ll, acc = score(ps, list(ho.y))
            out[label] = (b, acc)
            print(f"  {label:<24}{b:>10.4f}{ll:>10.4f}{acc:>9.3f}")

        mb, macc = out["market pre-match"]
        if macc > 0.95 or mb < 0.05:
            print(f"  !! market accuracy {macc:.3f} -- this anchor is still "
                  "leaking post-match prices. do not trust this row.")
            continue
        sb = out[f"elo shrunk k={best_k:.2f}"][0]
        print(f"\n  shrunk elo {sb:.4f} vs market {mb:.4f}  ->  {sb - mb:+.4f}  "
              f"{'ELO WINS' if sb < mb else 'market wins'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
