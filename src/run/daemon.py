"""production daemon — set-and-forget runner.

what it does (in a single process, on schedules):
  poller   -> every 60s
  paper sim -> every 5min (with CONSERVATIVE gates)
  settler  -> every 30min (turn closed fills into realized P&L)
  discover -> every 6h (refresh market catalog as new ones get listed)
  status   -> every 5min print one-line summary to log

handles transient errors with continue+log (don't crash on a single bad cycle).
graceful shutdown via Ctrl+C.

config: see CONSERVATIVE_PRESET below — edit to tune.

run:
    .venv\\Scripts\\python.exe -m src.run.daemon
    .venv\\Scripts\\python.exe -m src.run.daemon --preset balanced
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime, timezone

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from sqlalchemy import func, select  # noqa: E402

from src.db import Fill, Prediction, Snapshot, session_scope  # noqa: E402
from src.ingest.discover import store as discover_store  # noqa: E402
from src.ingest.poller import get_active_tickers, poll_once  # noqa: E402
from src.ingest.settler import settle_once  # noqa: E402
from src.kalshi.client import KalshiClient  # noqa: E402
from src.strategy.paper_sim import paper_trade_once  # noqa: E402

# tuning presets — pick via --preset
PRESETS = {
    "conservative": {
        "min_edge": 0.03,               # 3% minimum edge to enter
        "max_credible_edge": 0.30,      # skip > 30% edges (model bugs)
        "min_price_cents": 5,           # avoid 1-4¢ tail markets
        "max_price_cents": 95,
        "dedup_window_minutes": 240,    # only one trade per market per 4h
        "min_volume": 1000,             # market must have real trading activity
        "max_markets": 500,
    },
    "range_v1": {
        # session 4 strategy fork: edge lives in range (between) markets.
        # v2 baseline showed between=+100% ROI / greater=+2% ROI (mostly layups).
        # also attacks the [0.20, 0.40) overconfidence leak via max_credible_edge=0.15.
        "min_edge": 0.05,               # was 0.03 — cut marginal trades
        "max_credible_edge": 0.15,      # was 0.30 — blocks model's overconfident mid bucket
        "min_price_cents": 10,          # was 5 — 0-10c price bucket was -1.4% ROI
        "max_price_cents": 95,
        "dedup_window_minutes": 240,
        "min_volume": 1000,
        "max_markets": 500,
        "allowed_strike_types": ["between"],  # range markets only
    },
    "balanced": {
        "min_edge": 0.02,
        "max_credible_edge": 0.40,
        "min_price_cents": 3,
        "max_price_cents": 97,
        "dedup_window_minutes": 60,
        "min_volume": 100,
        "max_markets": 1000,
    },
    "aggressive": {
        "min_edge": 0.01,
        "max_credible_edge": 0.50,
        "min_price_cents": 2,
        "max_price_cents": 98,
        "dedup_window_minutes": 30,
        "min_volume": 0,
        "max_markets": 2000,
    },
}

# cadences (seconds)
POLL_INTERVAL = 60
TRADE_INTERVAL = 300       # 5 min
SETTLE_INTERVAL = 1800     # 30 min
DISCOVER_INTERVAL = 21600  # 6 hours
STATUS_INTERVAL = 300      # 5 min
ERROR_BACKOFF = 30         # seconds to wait after a transient error


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_ts()}] {msg}", flush=True)


def db_summary() -> dict:
    with session_scope() as s:
        return {
            "snapshots": s.scalar(select(func.count()).select_from(Snapshot)),
            "predictions": s.scalar(select(func.count()).select_from(Prediction)),
            "fills_total": s.scalar(select(func.count()).select_from(Fill)),
            "fills_settled": s.scalar(
                select(func.count()).select_from(Fill).where(Fill.realized_pnl.isnot(None))
            ),
            "realized_pnl_total": float(
                s.scalar(select(func.sum(Fill.realized_pnl)).where(Fill.realized_pnl.isnot(None))) or 0
            ),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", choices=list(PRESETS), default="conservative")
    parser.add_argument(
        "--duration", type=int, default=0, help="seconds to run, 0=forever"
    )
    args = parser.parse_args()

    preset = PRESETS[args.preset]
    log(f"daemon starting (preset={args.preset})")
    log(f"  preset: {preset}")

    last_poll = 0.0
    last_trade = 0.0
    last_settle = 0.0
    last_discover = 0.0
    last_status = 0.0
    started = time.time()

    cycle_errors = 0
    max_errors = 20  # bail if 20 errors in a row

    client_ctx = KalshiClient()
    client = client_ctx.__enter__()
    log("kalshi client opened")

    try:
        while True:
            now = time.time()
            if args.duration > 0 and (now - started) > args.duration:
                log(f"duration {args.duration}s elapsed, stopping")
                break

            did_work = False

            # POLL
            if now - last_poll >= POLL_INTERVAL:
                try:
                    tickers = get_active_tickers(max_close_hours=48, max_count=1500)
                    written = poll_once(client, tickers)
                    log(f"POLL: {len(tickers)} tracked -> {written} snapshots")
                    last_poll = now
                    did_work = True
                    cycle_errors = 0
                except Exception as e:
                    cycle_errors += 1
                    log(f"POLL_ERROR ({cycle_errors}/{max_errors}): {e}")
                    if cycle_errors >= max_errors:
                        raise

            # TRADE
            if now - last_trade >= TRADE_INTERVAL:
                try:
                    stats = paper_trade_once(**preset)
                    log(
                        f"TRADE: seen={stats['markets_seen']} preds={stats['predictions_written']} "
                        f"fills={stats['trades_paper']} "
                        f"skip(dedup={stats['trades_skipped_dedup']}, "
                        f"edge={stats['trades_skipped_edge_too_big']}, "
                        f"price={stats['trades_skipped_price_range']}, "
                        f"strike_type={stats.get('trades_skipped_strike_type', 0)})"
                    )
                    last_trade = now
                    did_work = True
                    cycle_errors = 0
                except Exception as e:
                    cycle_errors += 1
                    log(f"TRADE_ERROR ({cycle_errors}/{max_errors}): {e}")
                    traceback.print_exc()

            # SETTLE
            if now - last_settle >= SETTLE_INTERVAL:
                try:
                    s_stats = settle_once(client)
                    log(
                        f"SETTLE: checked={s_stats['fills_checked']} "
                        f"settled={s_stats['fills_settled']} "
                        f"pending={s_stats['fills_still_pending']} "
                        f"errors={s_stats['fills_error']} "
                        f"new_pnl=${s_stats['total_pnl_dollars']:+.2f}"
                    )
                    last_settle = now
                    did_work = True
                    cycle_errors = 0
                except Exception as e:
                    cycle_errors += 1
                    log(f"SETTLE_ERROR ({cycle_errors}/{max_errors}): {e}")

            # DISCOVER (catch new markets added by kalshi)
            if now - last_discover >= DISCOVER_INTERVAL:
                try:
                    log("DISCOVER: refreshing market catalog (may take ~60s)")
                    discover_store(client)
                    last_discover = now
                    did_work = True
                except Exception as e:
                    log(f"DISCOVER_ERROR: {e}")

            # STATUS
            if now - last_status >= STATUS_INTERVAL:
                try:
                    summary = db_summary()
                    log(
                        f"STATE: snaps={summary['snapshots']} preds={summary['predictions']} "
                        f"fills={summary['fills_total']} settled={summary['fills_settled']} "
                        f"realized_pnl=${summary['realized_pnl_total']:+.2f}"
                    )
                    last_status = now
                except Exception as e:
                    log(f"STATUS_ERROR: {e}")

            # if no work happened this iteration, sleep 5s
            if not did_work:
                time.sleep(5)
            else:
                time.sleep(1)

    except KeyboardInterrupt:
        log("got Ctrl+C, shutting down")
    except Exception as e:
        log(f"FATAL: {e}")
        traceback.print_exc()
        return 1
    finally:
        client_ctx.__exit__(None, None, None)
        log("daemon stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
