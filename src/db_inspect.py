"""quick db inspector. shows counts + sample rows.

run: .venv\\Scripts\\python.exe -m src.db_inspect
"""
from __future__ import annotations

import sys

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from sqlalchemy import desc, func, select  # noqa: E402

from src.db import (  # noqa: E402
    BankrollSnapshot,
    Fill,
    Market,
    Prediction,
    Snapshot,
    session_scope,
)


def main() -> int:
    with session_scope() as s:
        # overall counts
        print("=" * 70)
        print("db inspector")
        print("=" * 70)
        for tbl in (Market, Snapshot, Prediction, Fill, BankrollSnapshot):
            n = s.scalar(select(func.count()).select_from(tbl))
            print(f"  {tbl.__tablename__:20s} {n:6d} rows")

        # markets by series
        print("\ntop 15 series by market count:")
        rows = s.execute(
            select(Market.series_ticker, func.count())
            .group_by(Market.series_ticker)
            .order_by(desc(func.count()))
            .limit(15)
        ).all()
        for series_ticker, count in rows:
            print(f"  {(series_ticker or '?'):30s} {count:5d}")

        # sample crypto markets (closing soon)
        print("\nsample upcoming crypto markets (next 5 by close):")
        rows = s.execute(
            select(Market.ticker, Market.title, Market.close_ts)
            .where(Market.series_ticker.like("KXBTC%") | Market.series_ticker.like("KXETH%"))
            .where(Market.close_ts.isnot(None))
            .order_by(Market.close_ts)
            .limit(5)
        ).all()
        for t, title, close_ts in rows:
            print(f"  {t:40s} closes={close_ts}  {(title or '')[:45]}")

        # sample tennis markets
        print("\nsample upcoming tennis markets (next 5 by close):")
        rows = s.execute(
            select(Market.ticker, Market.title, Market.close_ts)
            .where(Market.series_ticker.like("KXATP%") | Market.series_ticker.like("KXWTA%"))
            .where(Market.close_ts.isnot(None))
            .order_by(Market.close_ts)
            .limit(5)
        ).all()
        for t, title, close_ts in rows:
            print(f"  {t:40s} closes={close_ts}  {(title or '')[:45]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
