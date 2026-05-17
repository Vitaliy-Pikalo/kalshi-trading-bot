"""show markets with moderate edges — where real signal might live.

filters to predictions with |edge| in [0.03, 0.10] (not phantom, not zero).

run: .venv\\Scripts\\python.exe -m src.show_edges
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from sqlalchemy import desc, func, select  # noqa: E402

from src.db import Market, Prediction, Snapshot, session_scope  # noqa: E402


def main() -> int:
    with session_scope() as s:
        # pull markets with moderate edge — exclude phantoms (|edge|>0.5) and noise (|edge|<0.02)
        rows = s.execute(
            select(
                Prediction.ticker,
                Prediction.predicted_prob,
                Prediction.market_implied_prob,
                Prediction.edge,
                Market.title,
                Market.close_ts,
            )
            .join(Market, Market.ticker == Prediction.ticker)
            .where(func.abs(Prediction.edge) >= 0.02)
            .where(func.abs(Prediction.edge) <= 0.15)
            .where(Prediction.market_implied_prob > 0)
            .order_by(desc(func.abs(Prediction.edge)))
            .limit(50)
        ).all()

        if not rows:
            print("no moderate-edge markets found")
            return 0

        print("=" * 110)
        print("MODERATE EDGE MARKETS — |edge| in [0.02, 0.15], real liquidity")
        print("=" * 110)
        print(f"{'ticker':40s} {'pred':>6s} {'impl':>6s} {'edge':>7s} {'closes':>20s}  title")
        print("-" * 110)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for ticker, pred, impl, edge, title, close_ts in rows:
            close_str = close_ts.strftime("%m-%d %H:%M") if close_ts else "?"
            hours_left = (close_ts - now).total_seconds() / 3600 if close_ts else 0
            side = "YES" if edge > 0 else "NO "
            title_short = (title or "")[:50]
            print(
                f"{ticker:40s} {pred:.3f} {impl:.3f} {edge:+.3f} "
                f"{close_str:>16s}({hours_left:+5.1f}h)  [{side}]  {title_short}"
            )

        # also show the 11 "fair" near-50/50 markets — most informative
        print()
        print("=" * 110)
        print("NEAR-FAIR MARKETS — implied in [0.30, 0.70] (most uncertain)")
        print("=" * 110)
        print(f"{'ticker':40s} {'pred':>6s} {'impl':>6s} {'edge':>7s} {'closes':>20s}  title")
        print("-" * 110)
        rows2 = s.execute(
            select(
                Prediction.ticker,
                Prediction.predicted_prob,
                Prediction.market_implied_prob,
                Prediction.edge,
                Market.title,
                Market.close_ts,
            )
            .join(Market, Market.ticker == Prediction.ticker)
            .where(Prediction.market_implied_prob >= 0.30)
            .where(Prediction.market_implied_prob <= 0.70)
            .order_by(desc(func.abs(Prediction.edge)))
            .limit(30)
        ).all()
        for ticker, pred, impl, edge, title, close_ts in rows2:
            close_str = close_ts.strftime("%m-%d %H:%M") if close_ts else "?"
            hours_left = (close_ts - now).total_seconds() / 3600 if close_ts else 0
            side = "YES" if edge > 0 else "NO "
            title_short = (title or "")[:50]
            print(
                f"{ticker:40s} {pred:.3f} {impl:.3f} {edge:+.3f} "
                f"{close_str:>16s}({hours_left:+5.1f}h)  [{side}]  {title_short}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
