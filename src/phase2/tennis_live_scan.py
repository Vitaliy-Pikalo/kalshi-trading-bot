"""live tennis scanner -- shows the REAL book, and the edge after the real ask.

this is the missing measurement. every backtest ROI assumed a fill at a 15-min
VWAP of trades that already happened. this prices the same strategy against
what you can actually buy right now.

it does NOT place orders. it prints a decision table.

reference rates are the POOLED realized yes-rate per price band from 2,716
backfilled markets (jun-aug 2026). bands where the measured bias was noise are
marked so, and never produce a BUY row.

usage:
    .venv\\Scripts\\python.exe -m src.phase2.tennis_live_scan
    .venv\\Scripts\\python.exe -m src.phase2.tennis_live_scan --hours 6 --contracts 20
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timedelta, timezone

from src.kalshi.client import KalshiClient
from src.utils import setup_utf8_stdout

setup_utf8_stdout()

SERIES = ["KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH"]

# (lo, hi, side, yes_wins, n, tradeable) -- raw counts so the CI is honest
# realized rates pooled over all 2,716 markets with a pre-match price.
BANDS = [
    (0.05, 0.10, "no",    1, 133, True),
    (0.10, 0.15, "no",    2,  82, True),
    (0.15, 0.20, "no",    9, 100, True),
    (0.20, 0.25, "no",   20,  98, False),   # bias -0.017 = noise
    (0.25, 0.30, "no",   26, 100, False),   # bias -0.012 = noise
    (0.80, 0.85, "yes",  74,  92, False),   # bias -0.014, WRONG SIGN
    (0.85, 0.90, "yes",  98, 104, True),
    (0.90, 0.95, "yes",  86,  87, True),
]


def wilson(wins: int, n: int, z: float = 1.96):
    """95% CI for a rate. small-n bands need this -- 2/82 is not 0.024 +/- nothing."""
    if n == 0:
        return (0.0, 1.0)
    ph = wins / n
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def fee(price: float, contracts: int) -> float:
    return math.ceil(0.07 * contracts * price * (1 - price) * 100) / 100.0


# measured on all 2,716 markets: net ROI by lead time, contracts=10.
#   NO  0.05-0.30 :  15m-1h +3.62 [+0.61,+7.24] | 1-3h +2.16 | 3-6h -1.03 | 6-12h -0.81
#   YES 0.80-0.95 :  15m-1h +2.57 | 1-3h +4.86 [+1.60,+8.01] | 3-6h +4.94 | 6-12h +4.35
# the longshot side DIES beyond ~3h. the favourite side holds to ~12h.
MAX_LEAD_MIN = {"no": 180.0, "yes": 720.0}


def lead_ok(side: str, mins: float) -> bool:
    return mins <= MAX_LEAD_MIN[side]


def band_for(mid: float):
    for lo, hi, side, wins, n, ok in BANDS:
        if lo <= mid < hi:
            return lo, hi, side, wins, n, ok
    return None


def dollars(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f / 100.0 if f > 1.0 else f


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12.0,
                    help="only markets whose match starts within this many hours")
    ap.add_argument("--contracts", type=int, default=10)
    ap.add_argument("--min-ev", type=float, default=0.0,
                    help="only show rows with EV per contract above this (dollars)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=args.hours)
    rows = []
    spreads = []

    with KalshiClient() as c:
        for s in SERIES:
            cursor = None
            while True:
                resp = c.list_markets(status="open", limit=1000,
                                      series_ticker=s, cursor=cursor)
                for mk in resp.get("markets", []) or []:
                    occ = mk.get("occurrence_datetime") or mk.get("expected_expiration_time")
                    try:
                        t = datetime.fromisoformat(str(occ).replace("Z", "+00:00"))
                    except Exception:  # noqa: BLE001
                        continue
                    if not (now < t <= horizon):
                        continue
                    yb, ya = dollars(mk.get("yes_bid_dollars")), dollars(mk.get("yes_ask_dollars"))
                    if yb is None or ya is None or ya <= 0 or ya >= 1:
                        continue
                    spread = ya - yb
                    spreads.append(spread)
                    mid = (ya + yb) / 2.0
                    b = band_for(mid)
                    if not b:
                        continue
                    lo, hi, side, wins, n, ok = b
                    rate = wins / n
                    rlo, rhi = wilson(wins, n)
                    if side == "no":
                        px = 1.0 - yb        # lifting the no-ask
                        win, win_worst = 1.0 - rate, 1.0 - rhi
                    else:
                        px = ya              # lifting the yes-ask
                        win, win_worst = rate, rlo
                    f = fee(px, args.contracts)
                    cost = px * args.contracts + f
                    ev = win * args.contracts - cost
                    ev_worst = win_worst * args.contracts - cost
                    breakeven = cost / args.contracts   # win prob needed to break even
                    mins = (t - now).total_seconds() / 60.0
                    timing_ok = lead_ok(side, mins)
                    spread_ok = spread <= 0.03
                    rows.append({
                        "ticker": mk.get("ticker"),
                        "event": mk.get("event_ticker") or mk.get("ticker"),
                        "title": (mk.get("title") or "")[:44],
                        "mins": mins, "yb": yb, "ya": ya,
                        "spread": spread, "mid": mid, "side": side, "px": px,
                        "band": f"{lo:.2f}-{hi:.2f}", "rate": rate, "n": n,
                        "breakeven": breakeven, "cost": cost, "ev": ev,
                        "ev_worst": ev_worst,
                        "ev_pct": 100 * ev / cost if cost else 0,
                        "ok": ok and timing_ok and spread_ok,
                        "why": ("" if (ok and timing_ok and spread_ok) else
                                ("band bias was noise/wrong sign" if not ok else
                                 f"spread {spread*100:.0f}c > 3c" if not spread_ok else
                                 f"T-{mins/60:.0f}h: {side.upper()} loses money beyond "
                                 f"{MAX_LEAD_MIN[side]/60:.0f}h in our data")),
                    })
                cursor = resp.get("cursor")
                if not cursor:
                    break

    print("=" * 108)
    print(f"THE MEASUREMENT THAT WAS MISSING -- live spreads, {len(spreads)} tennis markets "
          f"starting within {args.hours}h")
    print("=" * 108)
    if spreads:
        spreads.sort()
        q = lambda p: spreads[min(int(p * len(spreads)), len(spreads) - 1)]  # noqa: E731
        print(f"  spread (yes_ask - yes_bid):  median {q(.5)*100:.1f}c   "
              f"p25 {q(.25)*100:.1f}c   p75 {q(.75)*100:.1f}c   p90 {q(.9)*100:.1f}c")
        print(f"  backtested edge is ~6.7% of stake. on an 80c stake, "
              f"a {q(.5)*100:.1f}c spread costs {q(.5)/0.80*100:.1f}% -- "
              f"{'THE EDGE SURVIVES' if q(.5)/0.80 < 0.03 else 'THIS IS THE PROBLEM'}")
    else:
        print("  no open tennis markets in that window -- try --hours 24")
        return 0

    # one match = one bet. NO on the underdog and YES on the favourite are the
    # SAME position; listing both double-sizes it. keep the higher-EV side.
    best: dict = {}
    for r in rows:
        if not r["ok"] or r["ev"] <= args.min_ev:
            continue
        cur = best.get(r["event"])
        if cur is None or r["ev"] > cur["ev"]:
            best[r["event"]] = r
    live = list(best.values())
    dropped_dupe = sum(1 for r in rows if r["ok"] and r["ev"] > args.min_ev) - len(live)
    skipped = [r for r in rows if not r["ok"]]
    print("\n" + "=" * 108)
    print(f"CANDIDATES  (contracts={args.contracts}, EV uses the pooled realized rate, "
          f"priced at the ASK you'd actually pay)")
    print("=" * 108)
    if not live:
        print("  nothing qualifies right now.")
    else:
        print(f"  {'in':>6}{'side':<6}{'pay':>7}{'sprd':>6}{'need<':>8}{'hist':>7}"
              f"{'n':>5}{'EV%':>7}{'worst':>8}  match")
        for r in sorted(live, key=lambda x: -x["ev_pct"]):
            flag = ""
            need = r["breakeven"] if r["side"] == "yes" else 1 - r["breakeven"]
            print(f"  {r['mins']:>5.0f}m{r['side'].upper():<6}{r['px']:>7.3f}"
                  f"{r['spread']*100:>5.0f}c{need:>8.3f}{r['rate']:>7.3f}{r['n']:>5}"
                  f"{r['ev_pct']:>6.1f}%{r['ev_worst']:>8.2f}  {r['title']}{flag}")
        print(f"\n  'need<' = the rate that breaks you even. 'hist' = what we measured.")
        print(f"  'worst' = EV in dollars if the true rate sits at the bad end of its 95% CI.")
        if dropped_dupe:
            print(f"  {dropped_dupe} row(s) collapsed: both sides of the same match are ONE bet.")
    if skipped:
        from collections import Counter
        why = Counter(r["why"] for r in skipped)
        print(f"\n  {len(skipped)} market(s) filtered out:")
        for reason, k in why.most_common():
            print(f"    {k:>4}x  {reason}")

    print("\n" + "-" * 108)
    print("  EV assumes the historical rate for the band holds. n per band is 82-133.")
    print("  a 90% CI on those rates is roughly +/-4-8pp, which can flip a thin EV.")
    print("  this tool does not place orders and is not advice.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
