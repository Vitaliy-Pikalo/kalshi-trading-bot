"""settlement worker — turns paper fills into realized P&L.

logic:
  1. find fills where market.close_ts < now AND realized_pnl is NULL
  2. for each, fetch market detail to get `result` field
  3. compute P&L based on (side, result):
       side=yes, result=yes  -> won:  +(100-price) * contracts cents
       side=yes, result=no   -> lost: -(price)     * contracts cents
       side=no,  result=no   -> won:  +(100-price) * contracts cents
       side=no,  result=yes  -> lost: -(price)     * contracts cents
       result='' or unknown  -> skip (not yet settled or weird)
  4. update Fill.realized_pnl (stored in dollars, signed)

run:
    .venv\\Scripts\\python.exe -m src.ingest.settler
    .venv\\Scripts\\python.exe -m src.ingest.settler --loop --interval 600

note: this is read-only against kalshi — only updates our local db.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from src.utils import setup_utf8_stdout, utcnow_naive

setup_utf8_stdout()

from sqlalchemy import select  # noqa: E402

from src.db import Fill, Market, session_scope  # noqa: E402
from src.kalshi.client import KalshiClient  # noqa: E402


def compute_pnl_cents(side: str, result: str, price_cents: int, contracts: int) -> int | None:
    """Return realized P&L in cents (signed). None if can't compute."""
    if result not in ("yes", "no"):
        return None
    won = (side == result)
    if won:
        return (100 - price_cents) * contracts
    else:
        return -(price_cents * contracts)


def settle_once(client: KalshiClient) -> dict:
    """One settlement pass. Returns stats."""
    now = utcnow_naive()
    stats = {
        "fills_checked": 0,
        "fills_settled": 0,
        "fills_still_pending": 0,
        "fills_error": 0,
        "total_pnl_dollars": 0.0,
    }

    with session_scope() as s:
        # join fills + markets, get fills awaiting settlement
        rows = s.execute(
            select(Fill, Market)
            .join(Market, Market.ticker == Fill.ticker)
            .where(Fill.realized_pnl.is_(None))
            .where(Market.close_ts < now)
        ).all()

        if not rows:
            return stats

        # group by ticker to avoid duplicate api calls
        unsettled_by_ticker: dict[str, list[Fill]] = {}
        for fill, market in rows:
            unsettled_by_ticker.setdefault(fill.ticker, []).append(fill)

        for ticker, fills in unsettled_by_ticker.items():
            stats["fills_checked"] += len(fills)
            try:
                detail = client.get_market(ticker)
                market_obj = detail.get("market", {})
                result = market_obj.get("result", "").lower().strip()
            except Exception as e:
                print(f"  ! error fetching {ticker}: {e}")
                stats["fills_error"] += len(fills)
                continue

            if result not in ("yes", "no"):
                stats["fills_still_pending"] += len(fills)
                continue

            for fill in fills:
                pnl_cents = compute_pnl_cents(
                    side=fill.side,
                    result=result,
                    price_cents=fill.price_cents,
                    contracts=fill.contracts,
                )
                if pnl_cents is None:
                    stats["fills_error"] += 1
                    continue
                pnl_dollars = pnl_cents / 100.0
                fill.realized_pnl = pnl_dollars
                stats["fills_settled"] += 1
                stats["total_pnl_dollars"] += pnl_dollars

    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="run continuously")
    parser.add_argument(
        "--interval", type=int, default=600, help="seconds between passes (--loop only)"
    )
    args = parser.parse_args()

    with KalshiClient() as client:
        cycle = 0
        while True:
            cycle += 1
            stats = settle_once(client)
            ts = utcnow_naive().strftime("%H:%M:%S")
            print(
                f"[{ts}] cycle #{cycle}: "
                f"checked={stats['fills_checked']:4d} "
                f"settled={stats['fills_settled']:4d} "
                f"pending={stats['fills_still_pending']:4d} "
                f"errors={stats['fills_error']:3d} "
                f"pnl=${stats['total_pnl_dollars']:+.2f}"
            )
            if not args.loop:
                break
            time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
