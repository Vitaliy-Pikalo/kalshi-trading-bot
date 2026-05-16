"""diagnose paper trade results — show edge distribution + best opportunities."""
from __future__ import annotations

import sys

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from sqlalchemy import desc, func, select  # noqa: E402

from src.db import Market, Prediction, Snapshot, session_scope  # noqa: E402


def main() -> int:
    with session_scope() as s:
        # 1. predicted prob distribution (where does our model land?)
        print("=" * 70)
        print("PREDICTION DIAGNOSTICS")
        print("=" * 70)

        n = s.scalar(select(func.count()).select_from(Prediction))
        print(f"total predictions: {n}\n")

        # bucketed predicted prob distribution
        print("predicted_prob histogram (model's view):")
        for lo, hi in [(0.0, 0.01), (0.01, 0.1), (0.1, 0.3), (0.3, 0.7), (0.7, 0.9), (0.9, 0.99), (0.99, 1.01)]:
            c = s.scalar(
                select(func.count()).select_from(Prediction)
                .where(Prediction.predicted_prob >= lo)
                .where(Prediction.predicted_prob < hi)
            )
            bar = "#" * min(60, c // 2)
            print(f"  [{lo:.2f}, {hi:.2f})  {c:4d}  {bar}")

        # 2. implied prob distribution (where does the market land?)
        print("\nmarket_implied_prob histogram (where market is pricing):")
        for lo, hi in [(0.0, 0.01), (0.01, 0.1), (0.1, 0.3), (0.3, 0.7), (0.7, 0.9), (0.9, 0.99), (0.99, 1.01)]:
            c = s.scalar(
                select(func.count()).select_from(Prediction)
                .where(Prediction.market_implied_prob >= lo)
                .where(Prediction.market_implied_prob < hi)
            )
            bar = "#" * min(60, c // 2)
            print(f"  [{lo:.2f}, {hi:.2f})  {c:4d}  {bar}")

        # how many predictions have implied = 0 (no liquidity case)?
        zero_impl = s.scalar(
            select(func.count()).select_from(Prediction)
            .where(Prediction.market_implied_prob == 0.0)
        )
        nonzero_impl = n - zero_impl
        print(f"\npredictions with implied=0 (no liquidity): {zero_impl}/{n}")
        print(f"predictions with real implied:              {nonzero_impl}/{n}")

        # 3. edge distribution (only for non-zero implied)
        print("\nedge histogram (predicted - implied, only liquid markets):")
        for lo, hi in [(-1.0, -0.1), (-0.1, -0.03), (-0.03, -0.01), (-0.01, 0.01), (0.01, 0.03), (0.03, 0.1), (0.1, 1.0)]:
            c = s.scalar(
                select(func.count()).select_from(Prediction)
                .where(Prediction.market_implied_prob > 0)
                .where(Prediction.edge >= lo)
                .where(Prediction.edge < hi)
            )
            bar = "#" * min(60, c // 2)
            print(f"  [{lo:+.2f}, {hi:+.2f})  {c:4d}  {bar}")

        # 4. top 15 by |edge| where market has real liquidity
        print("\ntop 15 by |edge| (with real liquidity):")
        rows = s.execute(
            select(
                Prediction.ticker,
                Prediction.predicted_prob,
                Prediction.market_implied_prob,
                Prediction.edge,
            )
            .where(Prediction.market_implied_prob > 0)
            .order_by(desc(func.abs(Prediction.edge)))
            .limit(15)
        ).all()
        for t, p, i, e in rows:
            print(f"  {t:45s}  pred={p:.3f}  implied={i:.3f}  edge={e:+.3f}")

        # 5. sample markets with actual bid/ask > 0
        print("\nsample LIQUID markets (yes_ask > 0):")
        rows = s.execute(
            select(Snapshot.ticker, Snapshot.yes_bid, Snapshot.yes_ask, Snapshot.volume, Market.title)
            .join(Market, Market.ticker == Snapshot.ticker)
            .where(Snapshot.yes_ask > 0)
            .order_by(desc(Snapshot.volume))
            .limit(15)
        ).all()
        if not rows:
            print("  ⚠ no markets with yes_ask > 0 in current snapshot batch")
        for t, yb, ya, vol, title in rows:
            print(f"  {t:40s} bid={yb:3} ask={ya:3} vol={vol:5}  {(title or '')[:40]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
