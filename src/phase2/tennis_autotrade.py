"""automated tennis trading. DRY RUN BY DEFAULT. --live spends real money.

context you should not skip:
  - KALSHI_ENV=prod. there is no sandbox here.
  - this is the first order-placing code in the project. everything before it
    was simulation.
  - the strategy behind it has ZERO settled forward bets as of 17 aug 2026.
  - this repo's automation has failed silently before: the watchdog killed a
    healthy daemon every 5 minutes for weeks; the poller starved tennis of
    every slot for the project's entire life. neither was caught until someone
    looked. an auto-trader with a bug does not announce itself either.

safety rails, all enforced every run:
  1. dry run unless --live AND data/LIVE_TRADING_ARMED exists (two switches)
  2. data/HALT stops everything, immediately, no questions
  3. hard caps: contracts/order, stake/day, open positions/day
  4. every gate re-verified AT ORDER TIME (band, lead time, spread, EV) --
     prices move between scan and fill
  5. limit orders only, priced at the ask we validated. never market
  6. auto-halt on drawdown: if settled P&L in the log is worse than
     --max-drawdown, it writes data/HALT and stops trading
  7. balance check: refuses to trade below --min-balance

run:
    .venv\\Scripts\\python.exe -m src.phase2.tennis_autotrade                 # dry run
    .venv\\Scripts\\python.exe -m src.phase2.tennis_autotrade --live          # real money
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from datetime import datetime, timedelta, timezone

from src.kalshi.client import KalshiClient
from src.phase2.tennis_live_scan import BANDS, MAX_LEAD_MIN, dollars
from src.utils import setup_utf8_stdout

setup_utf8_stdout()

LOG = "data/tennis_bet_log.csv"
HALT = "data/HALT"
ARMED = "data/LIVE_TRADING_ARMED"
ORDERS = "data/tennis_orders.csv"
SERIES = ["KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH"]
ORDER_COLS = ["placed_at", "ticker", "side", "contracts", "limit_cents", "cost",
              "ev", "band", "lead_min", "spread", "order_id", "status", "title"]


def fee(price: float, contracts: int) -> float:
    return math.ceil(0.07 * contracts * price * (1 - price) * 100) / 100.0


def load_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_order(row: dict) -> None:
    new = not os.path.exists(ORDERS)
    with open(ORDERS, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ORDER_COLS)
        if new:
            w.writeheader()
        w.writerow(row)


def drawdown_check(max_dd: float) -> tuple[bool, str]:
    rows = [r for r in load_csv(LOG) if r.get("pnl") not in (None, "")]
    if not rows:
        return True, "no settled bets yet"
    pnl = sum(float(r["pnl"]) for r in rows)
    if pnl < -abs(max_dd):
        return False, f"settled P&L ${pnl:.2f} breaches -${abs(max_dd):.2f}"
    return True, f"settled P&L ${pnl:+.2f} over {len(rows)} bets"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="place REAL orders")
    ap.add_argument("--contracts", type=int, default=3)
    ap.add_argument("--max-contracts", type=int, default=5)
    ap.add_argument("--max-daily-stake", type=float, default=15.0)
    ap.add_argument("--max-orders", type=int, default=5)
    ap.add_argument("--min-ev", type=float, default=0.10)
    ap.add_argument("--max-spread", type=float, default=0.03)
    ap.add_argument("--max-drawdown", type=float, default=25.0)
    ap.add_argument("--min-balance", type=float, default=20.0)
    ap.add_argument("--hours", type=float, default=24.0)
    args = ap.parse_args()

    if os.path.exists(HALT):
        print(f"HALT file present ({HALT}) -- refusing to trade. delete it to resume.")
        return 1

    armed = os.path.exists(ARMED)
    live = args.live and armed
    mode = "LIVE - REAL MONEY" if live else "dry run"
    print("=" * 78)
    print(f"tennis autotrade -- {mode}")
    print("=" * 78)
    if args.live and not armed:
        print(f"  --live passed but {ARMED} does not exist. staying in dry run.")
        print(f"  to arm:  echo armed > {ARMED}")
    if args.contracts > args.max_contracts:
        print(f"  contracts {args.contracts} > cap {args.max_contracts} -- refusing")
        return 1

    ok, msg = drawdown_check(args.max_drawdown)
    print(f"  drawdown check: {msg}")
    if not ok:
        with open(HALT, "w", encoding="utf-8") as f:
            f.write(f"auto-halted {datetime.now(timezone.utc).isoformat()}: {msg}\n")
        print(f"  -> wrote {HALT}. trading stopped.")
        return 1

    today = datetime.now(timezone.utc).date().isoformat()
    todays = [r for r in load_csv(ORDERS) if r["placed_at"][:10] == today
              and r["status"] == "placed"]
    spent = sum(float(r["cost"]) for r in todays)
    print(f"  today: {len(todays)} orders, ${spent:.2f} staked "
          f"(caps: {args.max_orders} orders, ${args.max_daily_stake:.2f})")
    if len(todays) >= args.max_orders or spent >= args.max_daily_stake:
        print("  daily cap reached -- nothing further today")
        return 0

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=args.hours)
    already = {r["ticker"] for r in load_csv(ORDERS)}
    already_events = {t.rsplit("-", 1)[0] for t in already}
    candidates = []

    with KalshiClient() as c:
        if live:
            bal = c.get_balance()
            usd = float(bal.get("balance", 0)) / 100.0
            print(f"  account balance: ${usd:.2f}")
            if usd < args.min_balance:
                print(f"  below --min-balance ${args.min_balance:.2f} -- refusing")
                return 1

        for s in SERIES:
            cursor = None
            while True:
                resp = c.list_markets(status="open", limit=1000,
                                      series_ticker=s, cursor=cursor)
                for mk in resp.get("markets", []) or []:
                    tk = mk.get("ticker")
                    if tk in already or str(tk).rsplit("-", 1)[0] in already_events:
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
                    if spread > args.max_spread:
                        continue
                    band = next(((lo, hi, side, wins, n) for lo, hi, side, wins, n, ok_
                                 in BANDS if ok_ and lo <= mid < hi), None)
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
                    ev = win * args.contracts - cost
                    if ev < args.min_ev:
                        continue
                    candidates.append({
                        "ticker": tk, "title": (mk.get("title") or "")[:60],
                        "side": side, "px": px, "cents": int(round(px * 100)),
                        "cost": cost, "ev": ev, "band": f"{lo:.2f}-{hi:.2f}",
                        "mins": mins, "spread": spread,
                    })
                cursor = resp.get("cursor")
                if not cursor:
                    break

        # BOTH SIDES OF ONE MATCH ARE THE SAME BET. checking only against
        # previously-placed orders missed collisions WITHIN a single run --
        # caught by the first dry run, which wanted NO Heredia + YES Djere
        # (identical direction) as two "independent" positions.
        candidates.sort(key=lambda r: -r["ev"])
        seen_events: set[str] = set()
        deduped = []
        for r in candidates:
            ev_key = str(r["ticker"]).rsplit("-", 1)[0]
            if ev_key in seen_events:
                print(f"  skipping duplicate side of the same match: "
                      f"{r['side'].upper()} {r['title'][:44]}")
                continue
            seen_events.add(ev_key)
            deduped.append(r)
        candidates = deduped
        print(f"\n  {len(candidates)} market(s) pass every gate\n")
        placed = 0
        for r in candidates:
            if len(todays) + placed >= args.max_orders:
                print("  order cap reached"); break
            if spent + r["cost"] > args.max_daily_stake:
                print(f"  ${spent:.2f} + ${r['cost']:.2f} would exceed daily stake cap")
                break
            tag = "WOULD PLACE" if not live else "PLACING"
            print(f"  {tag}: {r['side'].upper()} {args.contracts} @ {r['cents']}c  "
                  f"cost ${r['cost']:.2f}  EV ${r['ev']:+.2f}  "
                  f"T-{r['mins']/60:.1f}h  {r['title'][:44]}")
            row = {"placed_at": now.isoformat(timespec="seconds"), "ticker": r["ticker"],
                   "side": r["side"], "contracts": args.contracts,
                   "limit_cents": r["cents"], "cost": f"{r['cost']:.2f}",
                   "ev": f"{r['ev']:.2f}", "band": r["band"],
                   "lead_min": f"{r['mins']:.0f}", "spread": f"{r['spread']:.3f}",
                   "order_id": "", "status": "dry_run", "title": r["title"]}
            if live:
                coid = f"tb-{r['ticker']}-{now.strftime('%Y%m%d%H%M')}"[:64]
                try:
                    resp = c.create_order(r["ticker"], r["side"], args.contracts,
                                          r["cents"], coid)
                    row["order_id"] = str((resp.get("order") or {}).get("order_id", ""))
                    row["status"] = "placed"
                    print(f"      -> order {row['order_id']}")
                except Exception as e:  # noqa: BLE001
                    row["status"] = f"error: {type(e).__name__}"
                    print(f"      -> FAILED: {e}")
            append_order(row)
            if row["status"] in ("placed", "dry_run"):
                spent += r["cost"]
                placed += 1

    print(f"\n  {placed} order(s) {'placed' if live else 'simulated'}, "
          f"${spent:.2f} of ${args.max_daily_stake:.2f} daily cap used")
    if not live:
        print(f"\n  DRY RUN. nothing was bought. to go live you need BOTH:")
        print(f"    echo armed > {ARMED}")
        print(f"    ... --live")
        print(f"  kill switch at any time:  echo stop > {HALT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
