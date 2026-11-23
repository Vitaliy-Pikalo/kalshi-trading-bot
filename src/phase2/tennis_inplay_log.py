"""IN-PLAY forward test: log qualifying bets while matches are being played.

why this exists: the in-play bias measured on 2,716 backfilled markets is
roughly double the pre-match one (+9.19% at 2-4h in, CI [+3.94, +13.46]),
and nothing was tracking it.

  snapshot taken   NO .05-.30            YES .80-.95
  0-30 min in       +0.09%  (flat)        +1.27%  (flat)
  30-60 min         +5.10%  [+0.37,+9.35] +3.41%
  90-120 min        +4.40%                +5.08%  [+0.64,+8.93]
  2-4 hours in      +9.19%  [+3.94,+13.46] +7.72% [+3.23,+12.14]

so: no bets before 30 minutes elapsed. that window is flat and would only add
noise to the sample.

IN-PLAY BANDS ARE NOT THE PRE-MATCH BANDS. measured separately below --
[0.20,0.30) is tradeable in-play but was noise pre-match, and [0.80,0.85)
flips from wrong-signed to positive. copying the pre-match table would have
been wrong in both directions.

caveats carried into every row this writes:
  - n per band is 38-140, smaller than pre-match. wider CIs.
  - matches still live at 2-4h are self-selected: long, grinding ones.
  - in-play spreads have NEVER been measured. this logger records them, which
    is itself a reason to run it before trusting any of the above.

run:
    .venv\\Scripts\\python.exe -m src.phase2.tennis_inplay_log --record
    .venv\\Scripts\\python.exe -m src.phase2.tennis_inplay_log --settle
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from datetime import datetime, timezone

from src.kalshi.client import KalshiClient
from src.phase2.tennis_live_scan import dollars, wilson
from src.utils import setup_utf8_stdout

setup_utf8_stdout()

LOG = "data/tennis_inplay_log.csv"
SERIES = ["KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH"]

# (lo, hi, side, yes_wins, n) -- measured on snapshots >=30 min into the match
INPLAY_BANDS = [
    (0.05, 0.10, "no",   1,  49),
    (0.10, 0.15, "no",   2,  38),
    (0.15, 0.20, "no",   5,  57),
    (0.20, 0.30, "no",  28, 140),
    (0.80, 0.85, "yes", 48,  56),
    (0.85, 0.90, "yes", 49,  52),
    (0.90, 0.95, "yes", 42,  44),
]
MIN_ELAPSED_MIN = 30.0
MAX_ELAPSED_MIN = 300.0
COLS = ["logged_at", "ticker", "title", "side", "contracts", "price", "fee", "cost",
        "spread", "band", "hist_rate", "band_n", "elapsed_min", "ev", "taken",
        "result", "payout", "pnl"]


def fee(price: float, contracts: int) -> float:
    return math.ceil(0.07 * contracts * price * (1 - price) * 100) / 100.0


def load() -> list[dict]:
    if not os.path.exists(LOG):
        return []
    with open(LOG, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save(rows: list[dict]) -> None:
    with open(LOG, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)


def record(args) -> int:
    now = datetime.now(timezone.utc)
    rows = load()
    have = {r["ticker"] for r in rows}
    have_events = {r["ticker"].rsplit("-", 1)[0] for r in rows}
    added, spreads, seen_live = [], [], 0

    with KalshiClient() as c:
        for s in SERIES:
            cursor = None
            while True:
                resp = c.list_markets(status="open", limit=1000,
                                      series_ticker=s, cursor=cursor)
                for mk in resp.get("markets", []) or []:
                    tk = mk.get("ticker")
                    ev_key = str(tk).rsplit("-", 1)[0]
                    occ = mk.get("occurrence_datetime")
                    if not occ:
                        continue
                    try:
                        t = datetime.fromisoformat(str(occ).replace("Z", "+00:00"))
                    except Exception:  # noqa: BLE001
                        continue
                    elapsed = (now - t).total_seconds() / 60.0
                    if not (MIN_ELAPSED_MIN <= elapsed <= MAX_ELAPSED_MIN):
                        continue
                    seen_live += 1
                    yb, ya = dollars(mk.get("yes_bid_dollars")), dollars(mk.get("yes_ask_dollars"))
                    if yb is None or ya is None or ya <= 0 or ya >= 1:
                        continue
                    spread = ya - yb
                    spreads.append(spread)
                    if tk in have or ev_key in have_events:
                        continue
                    if spread > args.max_spread:
                        continue
                    mid = (ya + yb) / 2
                    band = next(((lo, hi, side, w, n) for lo, hi, side, w, n
                                 in INPLAY_BANDS if lo <= mid < hi), None)
                    if not band:
                        continue
                    lo, hi, side, wins, n = band
                    rate = wins / n
                    px = (1 - yb) if side == "no" else ya
                    win = (1 - rate) if side == "no" else rate
                    f = fee(px, args.contracts)
                    cost = px * args.contracts + f
                    ev = win * args.contracts - cost
                    if ev < args.min_ev:
                        continue
                    added.append({
                        "logged_at": now.isoformat(timespec="seconds"), "ticker": tk,
                        "title": (mk.get("title") or "")[:70], "side": side,
                        "contracts": args.contracts, "price": f"{px:.3f}",
                        "fee": f"{f:.2f}", "cost": f"{cost:.2f}",
                        "spread": f"{spread:.3f}", "band": f"{lo:.2f}-{hi:.2f}",
                        "hist_rate": f"{rate:.4f}", "band_n": n,
                        "elapsed_min": f"{elapsed:.0f}", "ev": f"{ev:.2f}",
                        "taken": "yes" if args.taken else "no",
                        "result": "", "payout": "", "pnl": "",
                    })
                    have.add(tk)
                    have_events.add(ev_key)
                cursor = resp.get("cursor")
                if not cursor:
                    break

    save(rows + added)
    print(f"{seen_live} market(s) in play ({MIN_ELAPSED_MIN:.0f}-{MAX_ELAPSED_MIN:.0f} min elapsed)")
    if spreads:
        spreads.sort()
        q = lambda p: spreads[min(int(p * len(spreads)), len(spreads) - 1)]  # noqa: E731
        print(f"IN-PLAY SPREAD (never measured before): median {q(.5)*100:.1f}c  "
              f"p75 {q(.75)*100:.1f}c  p90 {q(.9)*100:.1f}c")
        print(f"  pre-match median was 1.0c. if in-play is much wider, the "
              f"{'edge survives anyway' if q(.5) <= 0.03 else 'edge may not survive'}.")
    print(f"logged {len(added)} new (total {len(rows) + len(added)})")
    for r in added:
        print(f"  {r['side'].upper():<4}{r['price']:>7}  EV {r['ev']:>6}  "
              f"{r['elapsed_min']}min in  spread {float(r['spread'])*100:.0f}c  "
              f"{r['title'][:44]}")
    return 0


def settle(args) -> int:
    rows = load()
    todo = [r for r in rows if not r["result"]]
    print(f"in-play log: {len(rows)} bets, {len(todo)} unsettled")
    if not todo:
        return 0
    with KalshiClient() as c:
        for i in range(0, len(todo), 100):
            chunk = todo[i:i + 100]
            resp = c.list_markets(status=None, limit=1000,
                                  tickers=",".join(r["ticker"] for r in chunk))
            found = {m.get("ticker"): m for m in resp.get("markets", []) or []}
            for r in chunk:
                mk = found.get(r["ticker"])
                if not mk or mk.get("result") not in ("yes", "no"):
                    continue
                r["result"] = mk["result"]
                won = r["result"] == r["side"]
                payout = float(r["contracts"]) if won else 0.0
                r["payout"] = f"{payout:.2f}"
                r["pnl"] = f"{payout - float(r['cost']):.2f}"
    save(rows)

    done = [r for r in rows if r["result"]]
    if not done:
        print("nothing settled yet")
        return 0
    cost = sum(float(r["cost"]) for r in done)
    pnl = sum(float(r["pnl"]) for r in done)
    exp = sum(float(r["ev"]) for r in done)
    wins = sum(1 for r in done if float(r["pnl"]) > 0)
    lo, hi = wilson(wins, len(done))
    print(f"\nIN-PLAY: n={len(done)}  staked ${cost:.2f}")
    print(f"  realized  ${pnl:+.2f} ({100*pnl/cost:+.2f}%)")
    print(f"  predicted ${exp:+.2f} ({100*exp/cost:+.2f}%)")
    print(f"  realized - predicted ${pnl-exp:+.2f}")
    print(f"  hit rate {wins}/{len(done)} = {wins/len(done):.3f}  95% CI [{lo:.3f}, {hi:.3f}]")
    print("\n  by elapsed-time window:")
    for a, b, lab in [(30, 60, "30-60m"), (60, 90, "60-90m"),
                      (90, 120, "90-120m"), (120, 300, "2h+")]:
        g = [r for r in done if a <= float(r["elapsed_min"]) < b]
        if not g:
            continue
        c_ = sum(float(r["cost"]) for r in g)
        p_ = sum(float(r["pnl"]) for r in g)
        print(f"    {lab:<9} n={len(g):>3}  ${p_:+7.2f}  ({100*p_/c_:+.2f}%)")
    if len(done) < 50:
        print(f"\n  {len(done)} bets. the backtest CI was [+3.94, +13.46] at 2-4h;")
        print("  you need ~150 in-play bets before realized can distinguish that from zero.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--settle", action="store_true")
    ap.add_argument("--taken", action="store_true")
    ap.add_argument("--contracts", type=int, default=3)
    ap.add_argument("--min-ev", type=float, default=0.05)
    ap.add_argument("--max-spread", type=float, default=0.04)
    args = ap.parse_args()
    if args.record:
        return record(args)
    if args.settle:
        return settle(args)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
