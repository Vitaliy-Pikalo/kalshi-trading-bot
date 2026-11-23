"""does tennis elo beat the market price? accuracy only -- no gates, no trials.

inputs (all already on disk, no network):
  data/tennis_markets.csv     2,724 markets: kalshi `result` + trade span
  data/tennis_prices_15m.csv  122,575 vwap buckets
  data/tennis/elo_*.parquet   april-vintage elo -- PRE-DATES every market here,
                              so using it involves zero lookahead

the point:
  a market price is a forecast. so is elo. score both against the same
  outcomes with Brier / log loss. if elo does not beat the price, no gate
  threshold can create edge, and rebuilding elo from a new source is wasted
  effort. this is the cheap kill-test that runs before any trading sim.

the fix carried over from the old baseline:
  an unmatched player used to fall back to elo 1500 and emit a confident
  number. here it SKIPS the market and gets counted. accents and multi-word
  surnames are normalized rather than string-matched.

run:
    .venv\\Scripts\\python.exe -m src.phase2.tennis_score
    .venv\\Scripts\\python.exe -m src.phase2.tennis_score --price-at early
"""
from __future__ import annotations

import argparse
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from src.strategy.tennis_elo import surface_for_event
from src.utils import setup_utf8_stdout

setup_utf8_stdout()

MARKETS = "data/tennis_markets.csv"
PRICES = "data/tennis_prices_15m.csv"
ELO = {"atp": Path("data/tennis/elo_atp.parquet"),
       "wta": Path("data/tennis/elo_wta.parquet")}

_TITLE_RE = re.compile(
    r"Will\s+(?P<yes>[A-Za-z\.\-' ]+?)\s+win\s+"
    r"(?:the\s+)?(?P<p1>[A-Za-z\.\-' ]+?)\s+vs\.?\s+(?P<p2>[A-Za-z\.\-' ]+?)[:?,]",
    re.IGNORECASE)


def norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", s.lower())


def surname_variants(name) -> set[str]:
    parts = [p for p in str(name).split() if p]
    out = {norm("".join(parts[-i:])) for i in range(1, min(3, len(parts)) + 1)}
    out.add(norm(name))
    out.discard("")
    return out


def build_elo_index(df: pd.DataFrame) -> dict:
    """surface -> variant key -> (elo, matches). ambiguous keys are dropped
    rather than guessed -- a wrong player is worse than no player."""
    idx: dict = {}
    for surface, sdf in df.groupby("surface"):
        by_key = defaultdict(list)
        for r in sdf.itertuples(index=False):
            for k in surname_variants(r.player_name):
                by_key[k].append((float(r.elo), int(r.matches_played), r.player_name))
        clean = {}
        for k, vals in by_key.items():
            names = {v[2] for v in vals}
            if len(names) == 1:
                clean[k] = vals[0][:2]
            else:  # same surname, several players -- take the most active
                best = max(vals, key=lambda v: v[1])
                clean[k] = (best[0], best[1]) if best[1] >= 20 else None
        idx[surface] = {k: v for k, v in clean.items() if v}
    return idx


def lookup(idx: dict, surface: str, name: str):
    table = idx.get(surface) or idx.get("hard") or {}
    for k in surname_variants(name):
        if k in table:
            return table[k]
    return None


def brier(p, y):
    return (p - y) ** 2


def logloss(p, y, eps=1e-6):
    p = min(max(p, eps), 1 - eps)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--price-at", choices=["late", "early", "both"], default="both",
                    help="which traded price represents the market's forecast")
    ap.add_argument("--min-matches", type=int, default=5,
                    help="skip players with fewer rated matches than this")
    args = ap.parse_args()

    mk = pd.read_csv(MARKETS)
    px = pd.read_csv(PRICES)
    print(f"markets {len(mk)}   price buckets {len(px)}")

    # market forecast: first and last bucket per ticker
    px = px.sort_values(["ticker", "bucket_ts"])
    first = px.groupby("ticker").first()["vwap_yes"].rename("px_early")
    last = px.groupby("ticker").last()["vwap_yes"].rename("px_late")
    mk = mk.merge(first, on="ticker", how="left").merge(last, on="ticker", how="left")

    elo_idx = {t: build_elo_index(pd.read_parquet(p)) for t, p in ELO.items()
               if p.exists()}
    for t, idx in elo_idx.items():
        print(f"  elo {t}: surfaces {sorted(idx)} "
              f"({sum(len(v) for v in idx.values())} name keys)")

    stats = Counter()
    rows = []
    for r in mk.itertuples(index=False):
        tour = "atp" if "KXATP" in str(r.ticker).upper() else "wta"
        stats[f"{tour}_total"] += 1
        if r.result not in ("yes", "no"):
            stats["no_result"] += 1
            continue
        m = _TITLE_RE.search(str(r.title))
        if not m:
            stats["unparsed"] += 1
            continue
        yes_name = m.group("yes").strip()
        p1, p2 = m.group("p1").strip(), m.group("p2").strip()
        yv = surname_variants(yes_name)
        opp = p2 if (yv & surname_variants(p1)) else p1
        if not (yv & surname_variants(p1)) and not (yv & surname_variants(p2)):
            stats["yes_side_unclear"] += 1
            continue

        surface = surface_for_event(str(r.event_ticker))
        a = lookup(elo_idx.get(tour, {}), surface, yes_name)
        b = lookup(elo_idx.get(tour, {}), surface, opp)
        if a is None or b is None:
            stats[f"{tour}_unrated"] += 1
            continue
        if a[1] < args.min_matches or b[1] < args.min_matches:
            stats[f"{tour}_too_few_matches"] += 1
            continue

        p_elo = 1.0 / (1.0 + 10 ** ((b[0] - a[0]) / 400.0))
        y = 1 if r.result == "yes" else 0
        stats[f"{tour}_scored"] += 1
        rows.append({"ticker": r.ticker, "series": r.series_ticker, "tour": tour,
                     "close_ts": r.close_ts, "surface": surface, "y": y,
                     "p_elo": p_elo, "px_early": r.px_early, "px_late": r.px_late})

    df = pd.DataFrame(rows)
    print("\n" + "=" * 76)
    print("JOIN")
    print("=" * 76)
    for tour in ("atp", "wta"):
        print(f"  {tour.upper()}  total {stats[f'{tour}_total']:>5}  "
              f"scored {stats[f'{tour}_scored']:>5}  "
              f"unrated {stats[f'{tour}_unrated']:>5}  "
              f"too_few {stats[f'{tour}_too_few_matches']:>4}")
    print(f"  dropped: no_result {stats['no_result']}  unparsed {stats['unparsed']}  "
          f"yes_side_unclear {stats['yes_side_unclear']}")
    if df.empty:
        print("\nnothing scored -- cannot compare")
        return 1

    cols = ["p_elo"] + (["px_early", "px_late"] if args.price_at == "both"
                        else [f"px_{args.price_at}"])
    print("\n" + "=" * 76)
    print("FORECAST QUALITY  (lower brier/logloss is better)")
    print("=" * 76)
    print(f"  {'forecaster':<14}{'n':>7}{'brier':>10}{'logloss':>10}{'acc':>9}{'mean p':>9}")
    for c in cols:
        d = df.dropna(subset=[c])
        if d.empty:
            continue
        b = sum(brier(p, y) for p, y in zip(d[c], d.y)) / len(d)
        ll = sum(logloss(p, y) for p, y in zip(d[c], d.y)) / len(d)
        acc = sum(int((p >= 0.5) == bool(y)) for p, y in zip(d[c], d.y)) / len(d)
        print(f"  {c:<14}{len(d):>7}{b:>10.4f}{ll:>10.4f}{acc:>9.3f}{d[c].mean():>9.3f}")
    base = df.y.mean()
    print(f"  {'always ' + f'{base:.2f}':<14}{len(df):>7}"
          f"{sum(brier(base, y) for y in df.y) / len(df):>10.4f}"
          f"{sum(logloss(base, y) for y in df.y) / len(df):>10.4f}"
          f"{max(base, 1 - base):>9.3f}{base:>9.3f}")

    ref = "px_late" if "px_late" in cols else cols[-1]
    d = df.dropna(subset=["p_elo", ref])
    if len(d):
        be = sum(brier(p, y) for p, y in zip(d.p_elo, d.y)) / len(d)
        bm = sum(brier(p, y) for p, y in zip(d[ref], d.y)) / len(d)
        print("\n" + "=" * 76)
        print("THE ANSWER")
        print("=" * 76)
        print(f"  elo brier {be:.4f}   market({ref}) brier {bm:.4f}   "
              f"diff {be - bm:+.4f}")
        print("  elo BEATS the market on accuracy" if be < bm else
              "  elo LOSES to the market -- no gate threshold can fix this")

    print("\n" + "=" * 76)
    print("ELO CALIBRATION (is it even honest about its own confidence?)")
    print("=" * 76)
    df["bin"] = (df.p_elo * 10).astype(int).clip(0, 9)
    print(f"  {'bucket':<12}{'n':>7}{'predicted':>12}{'actual':>10}{'gap':>10}")
    for b, g in df.groupby("bin"):
        print(f"  [{b / 10:.1f},{(b + 1) / 10:.1f})  {len(g):>7}"
              f"{g.p_elo.mean():>12.3f}{g.y.mean():>10.3f}"
              f"{g.y.mean() - g.p_elo.mean():>+10.3f}")

    print("\n  by segment (brier, elo vs market):")
    for key, g in df.groupby("series"):
        gg = g.dropna(subset=["p_elo", ref])
        if len(gg) < 20:
            continue
        be = sum(brier(p, y) for p, y in zip(gg.p_elo, gg.y)) / len(gg)
        bm = sum(brier(p, y) for p, y in zip(gg[ref], gg.y)) / len(gg)
        print(f"    {key:<24} n={len(gg):>5}  elo {be:.4f}  market {bm:.4f}  "
              f"{'ELO' if be < bm else 'market'}")

    df.to_csv("data/tennis_scored.csv", index=False)
    print(f"\n  -> data/tennis_scored.csv ({len(df)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
