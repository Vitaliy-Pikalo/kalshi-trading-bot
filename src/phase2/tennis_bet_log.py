"""forward-test log: record what you bet, settle it later, compare to prediction.

the backtest says ~+5%. the only way to know if that is real is to log every
qualifying bet BEFORE the result and score it after. this is the difference
between a strategy and a story.

--record   append currently-qualifying markets (from the same filters as
           tennis_live_scan) with the price and EV predicted AT THAT MOMENT.
           add --taken to mark which ones you actually placed.
--settle   fetch results for logged bets, compute realized P&L, and compare
           realized hit rate to the historical rate the EV assumed.

log lives at data/tennis_bet_log.csv and is append-only.

run:
    .venv\\Scripts\\python.exe -m src.phase2.tennis_bet_log --record --contracts 3
    .venv\\Scripts\\python.exe -m src.phase2.tennis_bet_log --settle
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from datetime import datetime, timedelta, timezone

from src.kalshi.client import KalshiClient
from src.phase2.tennis_live_scan import BANDS, MAX_LEAD_MIN, dollars, wilson
from src.utils import setup_utf8_stdout

setup_utf8_stdout()

LOG = "data/tennis_bet_log.csv"
SERIES = ["KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH"]
COLS = ["logged_at", "ticker", "title", "side", "contracts", "price", "fee",
        "cost", "spread", "band", "hist_rate", "lead_min", "ev", "taken",
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
    horizon = now + timedelta(hours=args.hours)
    existing = load()
    have = {r["ticker"] for r in existing}
    # NO on the underdog and YES on the favourite are the SAME bet on the same
    # match. logging both inflates the sample and hides correlated risk, so one
    # event = one entry, ever.
    have_events = {r["ticker"].rsplit("-", 1)[0] for r in existing}
    added = []

    with KalshiClient() as c:
        for s in SERIES:
            cursor = None
            while True:
                resp = c.list_markets(status="open", limit=1000,
                                      series_ticker=s, cursor=cursor)
                for mk in resp.get("markets", []) or []:
                    tk = mk.get("ticker")
                    ev_key = str(tk).rsplit("-", 1)[0]
                    if tk in have or ev_key in have_events:
                        continue
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
                    spread, mid = ya - yb, (ya + yb) / 2
                    if spread > 0.03:
                        continue
                    band = None
                    for lo, hi, side, wins, n, ok in BANDS:
                        if ok and lo <= mid < hi:
                            band = (lo, hi, side, wins, n)
                    if not band:
                        continue
                    lo, hi, side, wins, n = band
                    mins = (t - now).total_seconds() / 60
                    if mins > MAX_LEAD_MIN[side]:
                        continue
                    rate = wins / n
                    px = (1 - yb) if side == "no" else ya
                    win = (1 - rate) if side == "no" else rate
                    f = fee(px, args.contracts)
                    cost = px * args.contracts + f
                    added.append({
                        "logged_at": now.isoformat(timespec="seconds"),
                        "ticker": tk, "title": (mk.get("title") or "")[:70],
                        "side": side, "contracts": args.contracts,
                        "price": f"{px:.3f}", "fee": f"{f:.2f}", "cost": f"{cost:.2f}",
                        "spread": f"{spread:.3f}", "band": f"{lo:.2f}-{hi:.2f}",
                        "hist_rate": f"{rate:.4f}", "lead_min": f"{mins:.0f}",
                        "ev": f"{win * args.contracts - cost:.2f}",
                        "taken": "yes" if args.taken else "no",
                        "result": "", "payout": "", "pnl": "",
                    })
                    have.add(tk)
                    have_events.add(ev_key)
                cursor = resp.get("cursor")
                if not cursor:
                    break

    save(existing + added)
    print(f"logged {len(added)} new qualifying bets (total log: {len(existing) + len(added)})")
    for r in added:
        print(f"  {r['side'].upper():<4}{r['price']:>7}  EV {r['ev']:>6}  "
              f"T-{int(r['lead_min']) // 60}h  {r['title'][:52]}")
    if not args.taken and added:
        print("\n  all marked taken=no. rerun with --taken to log them as real positions,")
        print("  or edit the 'taken' column by hand. unsettled either way -- both get scored.")
    return 0


def settle(args) -> int:
    rows = load()
    todo = [r for r in rows if not r["result"]]
    print(f"log: {len(rows)} bets, {len(todo)} unsettled")
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
                won = (r["result"] == r["side"])
                payout = float(r["contracts"]) if won else 0.0
                r["payout"] = f"{payout:.2f}"
                r["pnl"] = f"{payout - float(r['cost']):.2f}"
    save(rows)

    done = [r for r in rows if r["result"]]
    if not done:
        print("nothing settled yet")
        return 0
    for label, sel in [("ALL logged", done),
                       ("TAKEN only", [r for r in done if r["taken"] == "yes"])]:
        if not sel:
            continue
        cost = sum(float(r["cost"]) for r in sel)
        pnl = sum(float(r["pnl"]) for r in sel)
        wins = sum(1 for r in sel if float(r["pnl"]) > 0)
        exp = sum(float(r["ev"]) for r in sel)
        print(f"\n{label}: n={len(sel)}  staked ${cost:.2f}  P&L ${pnl:+.2f}  "
              f"({100 * pnl / cost:+.2f}%)")
        print(f"  hit rate {wins}/{len(sel)} = {wins / len(sel):.3f}   "
              f"predicted P&L ${exp:+.2f}   realized minus predicted ${pnl - exp:+.2f}")
        lo, hi = wilson(wins, len(sel))
        print(f"  95% CI on realized hit rate: [{lo:.3f}, {hi:.3f}]")
        if len(sel) < 50:
            print(f"  {len(sel)} bets is far too few to conclude anything. target ~200.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--settle", action="store_true")
    ap.add_argument("--taken", action="store_true", help="mark as real positions")
    ap.add_argument("--contracts", type=int, default=3)
    ap.add_argument("--hours", type=float, default=24.0)
    args = ap.parse_args()
    if args.record:
        return record(args)
    if args.settle:
        return settle(args)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
