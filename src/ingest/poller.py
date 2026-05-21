"""snapshot poller — polls bid/ask/volume for tracked markets every N seconds.

filters to markets that are:
  - in our tracked series (crypto: KXBTC*, KXETH*; tennis: KXATPMATCH, KXWTAMATCH)
  - have close_ts in the future
  - close within max_close_hours (default 48h) — most active markets

uses bulk fetch (`tickers=t1,t2,...`) to fit 1000s of markets into <30s per poll.

run:
    # one poll, then exit
    .venv\\Scripts\\python.exe -m src.ingest.poller --once

    # poll every 60s for 10 min (good for testing)
    .venv\\Scripts\\python.exe -m src.ingest.poller --interval 60 --duration 600

    # poll forever
    .venv\\Scripts\\python.exe -m src.ingest.poller --interval 60
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta

from src.utils import setup_utf8_stdout, utcnow_naive

setup_utf8_stdout()

from sqlalchemy import select  # noqa: E402

from src.db import Market, Snapshot, session_scope  # noqa: E402
from src.kalshi.client import KalshiClient  # noqa: E402

# series we actively poll. add to this list to track new ones.
CRYPTO_SERIES = {
    "KXBTC", "KXBTCD", "KXBTCDB", "KXBTC15M",
    "KXETH", "KXETHD", "KXETH15M",
}
TENNIS_SERIES = {
    "KXATPMATCH", "KXWTAMATCH",
    "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH",
    "KXATPSETWINNER", "KXWTASETWINNER",
}


def chunked(seq, size):
    """yield successive chunks from seq."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def get_active_tickers(
    category: str | None = None,
    max_close_hours: int = 48,
    max_count: int = 1000,
) -> list[str]:
    """Tickers to poll: in target series + closing in (now, now+max_close_hours]."""
    if category == "crypto":
        series = CRYPTO_SERIES
    elif category == "tennis":
        series = TENNIS_SERIES
    else:
        series = CRYPTO_SERIES | TENNIS_SERIES

    now = utcnow_naive()
    cutoff = now + timedelta(hours=max_close_hours)
    with session_scope() as s:
        rows = s.execute(
            select(Market.ticker)
            .where(Market.series_ticker.in_(series))
            .where(Market.close_ts.isnot(None))
            .where(Market.close_ts > now)
            .where(Market.close_ts < cutoff)
            .order_by(Market.close_ts)
            .limit(max_count)
        ).all()
    return [r[0] for r in rows]


def _dollars_to_cents(v) -> int | None:
    """Kalshi returns prices as dollar-strings like '0.0100'. Convert to int cents."""
    if v is None or v == "":
        return None
    try:
        return int(round(float(v) * 100))
    except (ValueError, TypeError):
        return None


def _to_int(v) -> int | None:
    """Kalshi returns volume/oi as string floats like '19556.00'."""
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _extract_market_fields(m: dict) -> dict:
    """Extract bid/ask/volume from a kalshi market dict, handling both legacy
    (int cents) and current (string dollars) field naming."""
    return {
        "yes_bid": _dollars_to_cents(m.get("yes_bid_dollars"))
        if "yes_bid_dollars" in m else m.get("yes_bid"),
        "yes_ask": _dollars_to_cents(m.get("yes_ask_dollars"))
        if "yes_ask_dollars" in m else m.get("yes_ask"),
        "no_bid": _dollars_to_cents(m.get("no_bid_dollars"))
        if "no_bid_dollars" in m else m.get("no_bid"),
        "no_ask": _dollars_to_cents(m.get("no_ask_dollars"))
        if "no_ask_dollars" in m else m.get("no_ask"),
        "last_price": _dollars_to_cents(m.get("last_price_dollars"))
        if "last_price_dollars" in m else m.get("last_price"),
        "volume": _to_int(m.get("volume_fp")) if "volume_fp" in m else m.get("volume"),
        "open_interest": _to_int(m.get("open_interest_fp"))
        if "open_interest_fp" in m else m.get("open_interest"),
    }


def poll_once(
    client: KalshiClient,
    tickers: list[str],
    batch_size: int = 100,
) -> int:
    """Fetch + insert snapshots for the given tickers. Returns rows written."""
    if not tickers:
        return 0

    rows_written = 0
    now = utcnow_naive()

    for batch in chunked(tickers, batch_size):
        try:
            resp = client.list_markets(tickers=",".join(batch), limit=batch_size)
        except Exception as e:
            print(f"  ! batch failed ({len(batch)} tickers): {e}")
            continue

        markets = resp.get("markets", [])
        with session_scope() as s:
            for m in markets:
                fields = _extract_market_fields(m)
                snap = Snapshot(
                    ticker=m.get("ticker"),
                    ts=now,
                    **fields,
                )
                s.add(snap)
                rows_written += 1
    return rows_written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=60, help="seconds between polls")
    parser.add_argument(
        "--duration", type=int, default=0, help="total seconds (0=forever)"
    )
    parser.add_argument(
        "--once", action="store_true", help="single poll, then exit"
    )
    parser.add_argument(
        "--category", choices=["crypto", "tennis"], help="only poll this category"
    )
    parser.add_argument(
        "--max-close-hours",
        type=int,
        default=48,
        help="only poll markets closing within this many hours",
    )
    parser.add_argument(
        "--max-count", type=int, default=1000, help="cap polled markets"
    )
    args = parser.parse_args()

    started = time.time()
    poll_count = 0

    with KalshiClient() as client:
        while True:
            poll_count += 1
            t0 = time.time()
            tickers = get_active_tickers(
                category=args.category,
                max_close_hours=args.max_close_hours,
                max_count=args.max_count,
            )
            written = poll_once(client, tickers)
            t1 = time.time()
            elapsed = t1 - t0
            ts_str = utcnow_naive().strftime("%H:%M:%S")
            print(
                f"[{ts_str}] poll #{poll_count}: "
                f"{len(tickers)} tracked, {written} snapshots written, "
                f"{elapsed:.1f}s"
            )

            if args.once:
                break
            if args.duration > 0 and (time.time() - started) >= args.duration:
                print(f"\nstopped after {args.duration}s ({poll_count} polls)")
                break

            # sleep remainder of interval
            remaining = args.interval - elapsed
            if remaining > 0:
                time.sleep(remaining)

    return 0


if __name__ == "__main__":
    sys.exit(main())
