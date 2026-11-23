"""Resolve every BTC/ETH market from free Coinbase spot history. No Kalshi API.

WHY THIS EXISTS
---------------
`markets.settled_outcome` is empty for almost everything. `backfill_market_outcomes.py`
tries to fill it by hitting Kalshi's /markets/{ticker} endpoint once per market --
277,760 calls, ~9.6 hours -- and every market older than roughly a month returns 404,
so it cannot succeed. `derive_outcomes_from_fills.py` fixed the ~658 markets we
actually traded, but that leaves 277k predictions with no ground truth attached.

Those predictions are the entire offline-testing capacity of this project. Without
outcomes you learn at ~6 fills/day and wait two months for n=355. With them you can
score any gate configuration against three months of history in seconds.

KXBTC/KXETH markets resolve on the underlying price at a stated time. That is a
public fact. `src/spot/coinbase.py` already fetches it, free and unauthenticated.

    between : yes  iff  floor_strike <= price <= cap_strike
    greater : yes  iff  price >  floor_strike
    less    : yes  iff  price <  cap_strike

THE CAVEAT, STATED UP FRONT
---------------------------
Kalshi settles on its own index, not on Coinbase spot. Away from a strike boundary
the two cannot disagree about the outcome. Near a boundary they can. This script
therefore refuses to resolve a market when the settlement minute's [low, high]
straddles a strike, or when the close sits within `--tolerance` of one, and it
COUNTS those rather than guessing. A resolved-but-wrong outcome is worse than an
unresolved one -- it would silently corrupt every downstream calibration number.

ACCEPTANCE TEST, BUILT IN
-------------------------
We already know the true outcome for the ~658 markets we traded (from fills). The
script scores itself against those before writing anything. If agreement is below
--min-agreement it refuses to write. That is the whole basis for trusting it.

usage:
    .venv\\Scripts\\python.exe -m src.phase2.resolve_from_spot                 # validate only
    .venv\\Scripts\\python.exe -m src.phase2.resolve_from_spot --apply         # write
    .venv\\Scripts\\python.exe -m src.phase2.resolve_from_spot --refresh-cache
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import time
from bisect import bisect_left
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

DEFAULT_DB = "data/poker_quant.db"
CACHE_DIR = Path("data/spot_cache")
EXCHANGE = "https://api.exchange.coinbase.com"
GRANULARITY = 60          # 1-minute candles
MAX_CANDLES = 300         # coinbase hard limit per request
REQ_SLEEP = 0.34          # ~3 req/sec, well under the public limit

PAIR_FOR_SERIES = {
    "KXBTC": "BTC-USD", "KXBTCD": "BTC-USD", "KXBTC15M": "BTC-USD",
    "KXETH": "ETH-USD", "KXETHD": "ETH-USD", "KXETH15M": "ETH-USD",
}


def parse_ts(s):
    if s is None:
        return None
    s = str(s).replace("T", " ")
    for n, fmt in ((26, "%Y-%m-%d %H:%M:%S.%f"), (19, "%Y-%m-%d %H:%M:%S"), (16, "%Y-%m-%d %H:%M")):
        try:
            return datetime.strptime(s[:n], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- candle cache

def fetch_candles(pair: str, start: datetime, end: datetime) -> list[tuple]:
    """[(epoch, low, high, close)] for [start, end), chunked at 300 candles."""
    out, cur = [], start
    span = timedelta(seconds=GRANULARITY * MAX_CANDLES)
    n_chunks = max(1, int((end - start) / span) + 1)
    with httpx.Client(timeout=30.0) as c:
        i = 0
        while cur < end:
            chunk_end = min(cur + span, end)
            for attempt in range(5):
                try:
                    r = c.get(f"{EXCHANGE}/products/{pair}/candles",
                              params={"granularity": GRANULARITY,
                                      "start": cur.isoformat(),
                                      "end": chunk_end.isoformat()})
                    if r.status_code == 429:
                        time.sleep(2 ** attempt)
                        continue
                    r.raise_for_status()
                    for row in r.json():
                        out.append((int(row[0]), float(row[1]), float(row[2]), float(row[4])))
                    break
                except httpx.HTTPError as exc:
                    if attempt == 4:
                        print(f"    !! {pair} {cur:%Y-%m-%d %H:%M}: {exc}", file=sys.stderr)
                    time.sleep(2 ** attempt)
            i += 1
            if i % 25 == 0:
                print(f"    {pair}: chunk {i}/{n_chunks} ({cur:%Y-%m-%d})", flush=True)
            cur = chunk_end
            time.sleep(REQ_SLEEP)
    out.sort()
    return out


def load_or_fetch(pair: str, start: datetime, end: datetime, refresh: bool) -> list[tuple]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{pair}_{GRANULARITY}s.csv"
    cached: dict[int, tuple] = {}
    if path.exists() and not refresh:
        with path.open() as f:
            for row in csv.reader(f):
                if row and row[0] != "ts":
                    cached[int(row[0])] = (int(row[0]), float(row[1]), float(row[2]), float(row[3]))
        have = sorted(cached)
        if have and have[0] <= int(start.timestamp()) and have[-1] >= int(end.timestamp()) - GRANULARITY:
            print(f"  {pair}: cache hit ({len(cached)} candles)")
            return [cached[k] for k in have]
        print(f"  {pair}: cache incomplete ({len(cached)} candles) -- refetching")
    print(f"  {pair}: fetching {start:%Y-%m-%d} -> {end:%Y-%m-%d} at 1m ...")
    for row in fetch_candles(pair, start, end):
        cached[row[0]] = row
    have = sorted(cached)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "low", "high", "close"])
        for k in have:
            w.writerow(cached[k])
    print(f"  {pair}: cached {len(cached)} candles -> {path}")
    return [cached[k] for k in have]


# ---------------------------------------------------------------- resolution

def resolve(candles, times, close_ts, strike_type, floor_s, cap_s, tol_frac):
    """-> (outcome, price, reason). outcome is 'yes' | 'no' | None (unresolvable)."""
    target = int(close_ts.timestamp())
    i = bisect_left(times, target)
    # the candle whose window contains close_ts is the one starting at or just before it
    if i >= len(times) or times[i] > target:
        i -= 1
    if i < 0 or i >= len(times):
        return None, None, "no_candle"
    if abs(times[i] - target) > 15 * 60:
        return None, None, "candle_gap>15m"
    _ts, low, high, close = candles[i]

    strikes = [s for s in (floor_s, cap_s) if s is not None]
    if not strikes:
        return None, close, "no_strikes"
    tol = close * tol_frac
    for s in strikes:
        if low - tol <= s <= high + tol:
            return None, close, "straddles_strike"
        if abs(close - s) <= tol:
            return None, close, "within_tolerance"

    if strike_type == "between":
        if floor_s is None or cap_s is None:
            return None, close, "between_missing_strike"
        ok = floor_s <= close <= cap_s
    elif strike_type == "greater":
        ok = close > floor_s
    elif strike_type == "greater_or_equal":
        # Kalshi uses a distinct strike_type for >=. The tolerance check above
        # already refused anything close enough for > vs >= to matter, so this
        # differs from "greater" only in the exactly-equal case, which cannot
        # reach here. Handled separately anyway so the semantics stay honest.
        ok = close >= floor_s
    elif strike_type == "less":
        ok = close < cap_s
    elif strike_type == "less_or_equal":
        ok = close <= cap_s
    else:
        return None, close, f"unsupported:{strike_type}"
    return ("yes" if ok else "no"), close, "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve markets from Coinbase spot. No Kalshi API.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="write settled_outcome (default: validate only)")
    ap.add_argument("--refresh-cache", action="store_true")
    ap.add_argument("--tolerance", type=float, default=0.0005,
                    help="fraction of price treated as too close to a strike to call. "
                         "default 0.0005 = 5bp, covering Kalshi-index vs Coinbase basis.")
    ap.add_argument("--min-agreement", type=float, default=0.99,
                    help="refuse to write if agreement with known outcomes is below this")
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    rows = con.execute("""
        SELECT ticker, series_ticker, close_ts, strike_type, floor_strike, cap_strike, settled_outcome
        FROM markets
        WHERE close_ts IS NOT NULL AND series_ticker IS NOT NULL
    """).fetchall()
    print(f"markets with a close_ts: {len(rows)}")

    work, skipped_series = [], {}
    for tk, series, cts, st, fs, cs, existing in rows:
        pair = PAIR_FOR_SERIES.get(series)
        if pair is None:
            skipped_series[series] = skipped_series.get(series, 0) + 1
            continue
        t = parse_ts(cts)
        if t is None:
            continue
        work.append((tk, pair, t, st, fs, cs, existing))
    print(f"  resolvable series (BTC/ETH): {len(work)}")
    if skipped_series:
        print(f"  skipped, no spot pair mapped: {skipped_series}")
    if not work:
        return 1

    by_pair: dict[str, list] = {}
    for w in work:
        by_pair.setdefault(w[1], []).append(w)

    print("\nfetching spot history (cached to data/spot_cache/)")
    candles_for, times_for = {}, {}
    for pair, items in by_pair.items():
        lo = min(x[2] for x in items) - timedelta(hours=2)
        hi = max(x[2] for x in items) + timedelta(hours=2)
        hi = min(hi, datetime.now(timezone.utc))
        c = load_or_fetch(pair, lo, hi, args.refresh_cache)
        candles_for[pair] = c
        times_for[pair] = [x[0] for x in c]
        if not c:
            print(f"  !! {pair}: no candles, cannot resolve its markets")

    print("\nresolving ...")
    resolved, reasons, to_write = {}, {}, []
    for tk, pair, t, st, fs, cs, existing in work:
        if not candles_for.get(pair):
            reasons["no_spot_data"] = reasons.get("no_spot_data", 0) + 1
            continue
        outcome, price, why = resolve(candles_for[pair], times_for[pair], t, st, fs, cs, args.tolerance)
        reasons[why] = reasons.get(why, 0) + 1
        if outcome is None:
            continue
        resolved[tk] = (outcome, existing)
        if existing is None:
            to_write.append((outcome, tk))

    print(f"\n{'=' * 70}")
    print("RESOLUTION SUMMARY")
    print("=" * 70)
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {k:24s} {v:>8}")
    print(f"  {'RESOLVED':24s} {len(resolved):>8}")
    print(f"  {'of which new':24s} {len(to_write):>8}")

    # ---- acceptance test: agree with the outcomes we already know from fills ----
    known = [(tk, o, e) for tk, (o, e) in resolved.items() if e is not None]
    agree = sum(1 for _, o, e in known if o == e)
    print(f"\n{'=' * 70}")
    print("ACCEPTANCE TEST vs outcomes derived from fills")
    print("=" * 70)
    if not known:
        print("  no overlap with known outcomes -- run derive_outcomes_from_fills.py first.")
        print("  REFUSING TO WRITE without validation.")
        con.close()
        return 1
    rate = agree / len(known)
    print(f"  overlap        : {len(known)}")
    print(f"  agree          : {agree}")
    print(f"  disagree       : {len(known) - agree}")
    print(f"  agreement rate : {rate:.4%}   (threshold {args.min_agreement:.2%})")
    bad = [(tk, o, e) for tk, o, e in known if o != e]
    for tk, o, e in bad[:15]:
        print(f"    MISMATCH {tk:42s} spot={o:4s} fills={e}")
    if len(bad) > 15:
        print(f"    ... and {len(bad) - 15} more")

    if not args.apply:
        print("\n  VALIDATE ONLY -- nothing written. Re-run with --apply.")
        con.close()
        return 0
    if rate < args.min_agreement:
        print(f"\n  REFUSING TO WRITE: {rate:.2%} < {args.min_agreement:.2%}.")
        print("  Raise --tolerance, or investigate the mismatches above.")
        con.close()
        return 1

    con.executemany("UPDATE markets SET settled_outcome = ? WHERE ticker = ?", to_write)
    con.commit()
    print(f"\n  WROTE {len(to_write)} outcomes. Known outcomes left untouched.")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
