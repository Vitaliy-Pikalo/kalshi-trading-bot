"""deep-dive on a single market: full detail + orderbook.

run:
    .venv\\Scripts\\python.exe -m src.kalshi.probe_market
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from sqlalchemy import select  # noqa: E402

from src.db import Market, session_scope  # noqa: E402
from src.kalshi.client import KalshiClient  # noqa: E402


def main() -> int:
    # find one BTC daily market closing soon
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        rows = s.execute(
            select(Market)
            .where(Market.series_ticker.in_(["KXBTCD", "KXBTC"]))
            .where(Market.close_ts > now)
            .order_by(Market.close_ts)
            .limit(5)
        ).scalars().all()

    if not rows:
        print("no BTC markets in db")
        return 1

    with KalshiClient() as k:
        for m in rows:
            print("=" * 70)
            print(f"ticker:  {m.ticker}")
            print(f"title:   {m.title}")
            print(f"close:   {m.close_ts}")
            print()

            print("--- full get_market() response ---")
            detail = k.get_market(m.ticker)
            print(json.dumps(detail, indent=2, default=str)[:2500])

            print("\n--- orderbook ---")
            try:
                ob = k.get_orderbook(m.ticker, depth=5)
                print(json.dumps(ob, indent=2, default=str))
            except Exception as e:
                print(f"orderbook fetch failed: {e}")

            print()
            break  # just probe one

    return 0


if __name__ == "__main__":
    sys.exit(main())
