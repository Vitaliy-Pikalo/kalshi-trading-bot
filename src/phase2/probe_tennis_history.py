"""probe: is retrospective tennis market data retrievable from kalshi?

decides whether the tennis elo baseline can be evaluated on HISTORY (may-aug 2026)
or only by collecting prices FORWARD.

for a sample of already-closed tennis match markets, spread across months, tries:
  1. GET /markets/{ticker}            -> status, result (the outcome)
  2. GET /series/{s}/markets/{t}/candlesticks -> price history
  3. GET /markets/trades?ticker=      -> executed trades (fallback price source)

read-only. writes nothing to the db. writes one json report to data/.

run:
    .venv\\Scripts\\python.exe -m src.phase2.probe_tennis_history
    .venv\\Scripts\\python.exe -m src.phase2.probe_tennis_history --per-month 5
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

import httpx

from src.kalshi.client import KalshiClient
from src.utils import setup_utf8_stdout

setup_utf8_stdout()

DB = "data/poker_quant.db"
SERIES = ("KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH")
MONTHS = ("2026-05", "2026-06", "2026-07", "2026-08")


def sample_markets(per_month: int) -> list[dict]:
    """closed tennis match markets, per_month per (month, series)."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    out: list[dict] = []
    for month in MONTHS:
        for series in SERIES:
            rows = con.execute(
                """select ticker, series_ticker, title, close_ts, settled_outcome
                   from markets
                   where series_ticker = ?
                     and close_ts like ?
                     and close_ts < ?
                   order by close_ts limit ?""",
                (series, f"{month}%", now, per_month),
            ).fetchall()
            for r in rows:
                d = dict(r)
                d["month"] = month
                out.append(d)
    con.close()
    return out


def try_call(fn, *a, **kw):
    """returns (ok, payload_or_error_string, http_status)."""
    try:
        return True, fn(*a, **kw), 200
    except httpx.HTTPStatusError as e:
        return False, f"HTTP {e.response.status_code}", e.response.status_code
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", None


def probe_one(client: KalshiClient, m: dict) -> dict:
    res: dict = {
        "ticker": m["ticker"],
        "series": m["series_ticker"],
        "month": m["month"],
        "close_ts": m["close_ts"],
    }

    # 1. market metadata + result
    ok, payload, code = try_call(client.get_market, m["ticker"])
    res["market_http"] = code
    if ok:
        mk = payload.get("market", payload)
        res["status"] = mk.get("status")
        res["result"] = mk.get("result")
        res["volume"] = mk.get("volume")
        res["last_price"] = mk.get("last_price")
    else:
        res["market_err"] = payload

    # 2. candlesticks (hourly) over the market's life
    close_dt = datetime.fromisoformat(str(m["close_ts"]).replace(" ", "T"))
    end_ts = int(close_dt.replace(tzinfo=timezone.utc).timestamp()) + 3600
    start_ts = end_ts - 7 * 24 * 3600
    ok, payload, code = try_call(
        client.get,
        f"/series/{m['series_ticker']}/markets/{m['ticker']}/candlesticks",
        params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": 60},
    )
    res["candles_http"] = code
    if ok:
        candles = payload.get("candlesticks", []) or []
        res["n_candles"] = len(candles)
        res["candle_sample"] = candles[:2]
    else:
        res["candles_err"] = payload
        res["n_candles"] = 0

    # 3. executed trades
    ok, payload, code = try_call(
        client.get, "/markets/trades", params={"ticker": m["ticker"], "limit": 100}
    )
    res["trades_http"] = code
    if ok:
        trades = payload.get("trades", []) or []
        res["n_trades"] = len(trades)
        res["trade_sample"] = trades[:2]
    else:
        res["trades_err"] = payload
        res["n_trades"] = 0

    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-month", type=int, default=3,
                    help="markets per (month, series) to probe")
    ap.add_argument("--out", default="data/tennis_history_probe.json")
    args = ap.parse_args()

    markets = sample_markets(args.per_month)
    print(f"sampled {len(markets)} closed tennis markets across {len(MONTHS)} months")
    if not markets:
        print("NO CLOSED TENNIS MARKETS FOUND — check db path / close_ts values")
        return 1

    results: list[dict] = []
    with KalshiClient() as client:
        for i, m in enumerate(markets, 1):
            r = probe_one(client, m)
            results.append(r)
            print(
                f"  [{i:>3}/{len(markets)}] {r['month']} {r['series']:<22} "
                f"market={r.get('market_http')} result={r.get('result')!r} "
                f"candles={r.get('n_candles')}({r.get('candles_http')}) "
                f"trades={r.get('n_trades')}({r.get('trades_http')})"
            )

    # rollup
    print("\n" + "=" * 78)
    print("ROLLUP: retrievability by month")
    print("=" * 78)
    agg = defaultdict(lambda: {"n": 0, "result_ok": 0, "candles_ok": 0, "trades_ok": 0})
    for r in results:
        a = agg[r["month"]]
        a["n"] += 1
        a["result_ok"] += 1 if r.get("result") not in (None, "", "unknown") else 0
        a["candles_ok"] += 1 if r.get("n_candles", 0) > 0 else 0
        a["trades_ok"] += 1 if r.get("n_trades", 0) > 0 else 0
    print(f"{'month':<10}{'n':>5}{'result':>10}{'candles':>10}{'trades':>10}")
    for month in MONTHS:
        a = agg.get(month)
        if not a:
            print(f"{month:<10}{'-':>5}{'-':>10}{'-':>10}{'-':>10}")
            continue
        print(f"{month:<10}{a['n']:>5}{a['result_ok']:>10}{a['candles_ok']:>10}{a['trades_ok']:>10}")

    codes = defaultdict(int)
    for r in results:
        codes[("market", r.get("market_http"))] += 1
        codes[("candles", r.get("candles_http"))] += 1
        codes[("trades", r.get("trades_http"))] += 1
    print("\nhttp status counts:")
    for (kind, code), n in sorted(codes.items(), key=lambda x: (x[0][0], str(x[0][1]))):
        print(f"  {kind:<9} {str(code):<6} {n}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nfull report -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
