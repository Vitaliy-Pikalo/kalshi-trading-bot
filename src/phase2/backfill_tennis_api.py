"""backfill tennis outcomes + executed-trade prices from the kalshi API.

probe findings (probe_tennis_history.py, then a 200-market dry run):
  - metadata retention boundary: markets closing <= ~15 jun 2026 are GONE (404);
    markets closing >= ~22 jun 2026 return 200 with `result`
  - candlesticks return HTTP 200 with an empty array -> useless
  - /markets/trades returns real executed prices, ~2,500 trades per market,
    and reaches back BEFORE the metadata boundary
  - the window MOVES: markets age out ~8 weeks after close. rerun early.

outputs (raw trades are aggregated on the fly -- 6M rows is not worth storing):
  data/tennis_markets.csv     one row per market: result, volume, trade span
  data/tennis_prices_15m.csv  15-min VWAP buckets: the replay's price path
  data/tennis_trades.csv.gz   only with --raw

read-only by default. `--write` also sets markets.settled_outcome.

run:
    # full backfill, writes settled_outcome, resumable
    .venv\\Scripts\\python.exe -m src.phase2.backfill_tennis_api --since 2026-06-16 --write

    # resume after an interruption
    .venv\\Scripts\\python.exe -m src.phase2.backfill_tennis_api --since 2026-06-16 --write --resume
"""
from __future__ import annotations

import argparse
import csv
import gzip
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import httpx

from src.kalshi.client import KalshiClient
from src.utils import setup_utf8_stdout

setup_utf8_stdout()

DB = "data/poker_quant.db"
SERIES = ("KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH")
MARKETS_CSV = "data/tennis_markets.csv"
PRICES_CSV = "data/tennis_prices_15m.csv"
RAW_CSV = "data/tennis_trades.csv.gz"

MARKET_COLS = ["ticker", "series_ticker", "event_ticker", "close_ts", "result",
               "status", "volume_fp", "open_interest_fp", "n_trades",
               "first_trade_ts", "last_trade_ts", "total_contracts",
               "vwap_all", "last_yes_price", "title"]
PRICE_COLS = ["ticker", "series_ticker", "close_ts", "result", "bucket_ts",
              "n_trades", "contracts", "vwap_yes", "min_yes", "max_yes", "last_yes"]


def load_targets(since: str, max_markets: int | None) -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    rows = con.execute(
        f"""select ticker, series_ticker, title, event_ticker, close_ts
            from markets
            where series_ticker in ({','.join('?' * len(SERIES))})
              and close_ts >= ? and close_ts < ?
            order by close_ts""",
        (*SERIES, since, now),
    ).fetchall()
    con.close()
    out = [dict(r) for r in rows]
    return out[:max_markets] if max_markets else out


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i: i + size]


def fetch_metadata(client: KalshiClient, targets: list[dict]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    tickers = [t["ticker"] for t in targets]
    for n, chunk in enumerate(chunked(tickers, 100), 1):
        try:
            resp = client.list_markets(status=None, limit=1000, tickers=",".join(chunk))
        except httpx.HTTPStatusError as e:
            print(f"\n  chunk {n} failed: HTTP {e.response.status_code}")
            continue
        for mk in resp.get("markets", []) or []:
            found[mk.get("ticker")] = mk
        print(f"  metadata: {len(found)} resolved of {len(tickers)} asked", end="\r")
    print()
    return found


def fetch_trades(client: KalshiClient, ticker: str, max_pages: int = 40) -> list[dict]:
    trades: list[dict] = []
    cursor = None
    for _ in range(max_pages):
        params = {"ticker": ticker, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = client.get("/markets/trades", params=params)
        except httpx.HTTPStatusError:
            break
        batch = resp.get("trades", []) or []
        trades.extend(batch)
        cursor = resp.get("cursor")
        if not cursor or not batch:
            break
    return trades


def fnum(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def yes_prob(t: dict):
    for k in ("yes_price_dollars", "yes_price"):
        f = fnum(t.get(k))
        if f is None:
            continue
        return f / 100.0 if f > 1.0 else f
    return None


def bucket_key(iso_ts: str, minutes: int) -> str:
    """floor an ISO8601 timestamp to a bucket."""
    s = str(iso_ts).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return ""
    dt = dt.astimezone(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)
    dt = dt.replace(minute=(dt.minute // minutes) * minutes)
    return dt.isoformat(sep=" ")


def done_tickers(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {r["ticker"] for r in csv.DictReader(f) if r.get("ticker")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-16")
    ap.add_argument("--max-markets", type=int, default=None)
    ap.add_argument("--bucket-minutes", type=int, default=15)
    ap.add_argument("--write", action="store_true", help="persist settled_outcome")
    ap.add_argument("--resume", action="store_true", help="skip markets already in the csv")
    ap.add_argument("--raw", action="store_true", help="also dump every trade, gzipped")
    ap.add_argument("--series", nargs="+", default=None,
                    help="override which series to backfill, e.g. KXATPEXACTMATCH")
    ap.add_argument("--tag", default="",
                    help="suffix for output files, so a new series does not "
                         "overwrite the match-market dataset")
    args = ap.parse_args()

    global SERIES, MARKETS_CSV, PRICES_CSV, RAW_CSV
    if args.series:
        SERIES = tuple(args.series)
        print(f"series override: {SERIES}")
    if args.tag:
        MARKETS_CSV = f"data/tennis_markets_{args.tag}.csv"
        PRICES_CSV = f"data/tennis_prices_15m_{args.tag}.csv"
        RAW_CSV = f"data/tennis_trades_{args.tag}.csv.gz"
        print(f"writing to {MARKETS_CSV} / {PRICES_CSV}")

    targets = load_targets(args.since, args.max_markets)
    print(f"targets: {len(targets)} match markets closing >= {args.since}")
    if not targets:
        return 1

    already = done_tickers(MARKETS_CSV) if args.resume else set()
    if already:
        print(f"resume: {len(already)} markets already done, skipping them")

    with KalshiClient() as client:
        print("\nphase 1: bulk metadata")
        meta = fetch_metadata(client, targets)

        weeks: dict[str, Counter] = defaultdict(Counter)
        for t in targets:
            d = str(t["close_ts"])[:10]
            wk = d[:8] + f"{(int(d[8:10]) - 1) // 7 * 7 + 1:02d}"
            c = weeks[wk]
            c["n"] += 1
            mk = meta.get(t["ticker"])
            if mk:
                c["found"] += 1
                if mk.get("result") in ("yes", "no"):
                    c["result"] += 1
        print(f"\n{'week of':<12}{'n':>7}{'found':>8}{'result':>8}")
        for wk in sorted(weeks):
            c = weeks[wk]
            print(f"{wk:<12}{c['n']:>7}{c['found']:>8}{c['result']:>8}")

        resolvable = [t for t in targets
                      if meta.get(t["ticker"], {}).get("result") in ("yes", "no")]
        print(f"\nresolvable now: {len(resolvable)}/{len(targets)}")

        if args.write and resolvable:
            con = sqlite3.connect(DB, timeout=60)
            con.executemany("update markets set settled_outcome=? where ticker=?",
                            [(meta[t["ticker"]]["result"], t["ticker"]) for t in resolvable])
            con.commit()
            con.close()
            print(f"wrote settled_outcome for {len(resolvable)} markets")
        elif resolvable:
            print("(dry run -- pass --write to persist settled_outcome)")

        print(f"\nphase 2: trades -> {args.bucket_minutes}-min buckets")
        todo = [t for t in resolvable if t["ticker"] not in already]
        print(f"  {len(todo)} markets to fetch")

        append = args.resume and os.path.exists(MARKETS_CSV)
        mode = "a" if append else "w"
        mf = open(MARKETS_CSV, mode, newline="", encoding="utf-8")
        pf = open(PRICES_CSV, mode, newline="", encoding="utf-8")
        rf = gzip.open(RAW_CSV, mode + "t", newline="", encoding="utf-8") if args.raw else None
        mw, pw = csv.writer(mf), csv.writer(pf)
        rw = csv.writer(rf) if rf else None
        if not append:
            mw.writerow(MARKET_COLS)
            pw.writerow(PRICE_COLS)
            if rw:
                rw.writerow(["ticker", "created_time", "yes_prob", "contracts", "taker_side"])

        n_with = n_buckets = n_trades_total = 0
        try:
            for i, t in enumerate(todo, 1):
                mk = meta[t["ticker"]]
                trades = fetch_trades(client, t["ticker"])
                n_trades_total += len(trades)
                if trades:
                    n_with += 1

                buckets: dict[str, dict] = {}
                tot_c = tot_pc = 0.0
                first_ts = last_ts = None
                last_p = None
                for tr in trades:
                    p = yes_prob(tr)
                    c = fnum(tr.get("count_fp"), fnum(tr.get("count"), 0.0)) or 0.0
                    ts = tr.get("created_time")
                    if p is None or not ts:
                        continue
                    if rw:
                        rw.writerow([t["ticker"], ts, f"{p:.4f}", c, tr.get("taker_side")])
                    tot_c += c
                    tot_pc += p * c
                    if first_ts is None or ts < first_ts:
                        first_ts = ts
                    if last_ts is None or ts > last_ts:
                        last_ts, last_p = ts, p
                    b = buckets.setdefault(bucket_key(ts, args.bucket_minutes),
                                           {"n": 0, "c": 0.0, "pc": 0.0,
                                            "mn": p, "mx": p, "lts": ts, "last": p})
                    b["n"] += 1
                    b["c"] += c
                    b["pc"] += p * c
                    b["mn"] = min(b["mn"], p)
                    b["mx"] = max(b["mx"], p)
                    if ts > b["lts"]:
                        b["lts"], b["last"] = ts, p

                for bts in sorted(buckets):
                    b = buckets[bts]
                    vwap = b["pc"] / b["c"] if b["c"] else b["last"]
                    pw.writerow([t["ticker"], t["series_ticker"], t["close_ts"],
                                 mk.get("result"), bts, b["n"], f"{b['c']:.2f}",
                                 f"{vwap:.4f}", f"{b['mn']:.4f}", f"{b['mx']:.4f}",
                                 f"{b['last']:.4f}"])
                n_buckets += len(buckets)

                mw.writerow([t["ticker"], t["series_ticker"], t["event_ticker"],
                             t["close_ts"], mk.get("result"), mk.get("status"),
                             mk.get("volume_fp"), mk.get("open_interest_fp"),
                             len(trades), first_ts, last_ts, f"{tot_c:.2f}",
                             f"{tot_pc / tot_c:.4f}" if tot_c else "",
                             f"{last_p:.4f}" if last_p is not None else "",
                             t["title"]])

                if i % 25 == 0:
                    mf.flush(); pf.flush()
                    print(f"  {i}/{len(todo)} markets | {n_trades_total} trades "
                          f"| {n_buckets} buckets", end="\r")
        finally:
            mf.close(); pf.close()
            if rf:
                rf.close()

        print(f"\n  done: {len(todo)} markets, {n_with} with trades, "
              f"{n_trades_total} trades -> {n_buckets} buckets")
        if todo:
            print(f"  mean trades/market: {n_trades_total / len(todo):.0f}")
        print(f"  -> {MARKETS_CSV}, {PRICES_CSV}" + (f", {RAW_CSV}" if rf else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
