"""phase 1 analyzer — once fills are settled, what does our edge look like?

metrics:
  - total fills / settled / open
  - win rate (settled only)
  - realized P&L
  - average edge (predicted) vs average realized P&L per $ at risk
  - calibration: of fills where model said pred=0.7, what % actually hit?
  - sharpe-ish: mean P&L / std P&L

run: .venv\\Scripts\\python.exe -m src.analyze
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from sqlalchemy import desc, func, select  # noqa: E402

from src.db import Fill, Prediction, session_scope  # noqa: E402


def main() -> int:
    with session_scope() as s:
        # overall fill counts
        total = s.scalar(select(func.count()).select_from(Fill))
        settled = s.scalar(
            select(func.count()).select_from(Fill).where(Fill.realized_pnl.isnot(None))
        )
        open_n = total - settled

        print("=" * 70)
        print("PHASE 1 ANALYSIS")
        print("=" * 70)
        print(f"total paper fills:        {total}")
        print(f"  settled:                {settled}")
        print(f"  still open:             {open_n}")

        if settled == 0:
            print("\nno settled fills yet — run settler after markets close")
            return 0

        # P&L stats
        pnl_rows = s.execute(
            select(Fill.realized_pnl, Fill.cost_usd, Fill.side, Fill.ticker)
            .where(Fill.realized_pnl.isnot(None))
        ).all()

        pnls = [r[0] for r in pnl_rows]
        costs = [r[1] for r in pnl_rows]
        sides = [r[2] for r in pnl_rows]

        total_pnl = sum(pnls)
        total_risked = sum(costs)
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        win_rate = wins / settled if settled else 0
        avg_pnl = total_pnl / settled
        mean = avg_pnl
        std = math.sqrt(sum((p - mean) ** 2 for p in pnls) / settled) if settled > 1 else 0
        sharpe = (mean / std) * math.sqrt(252) if std > 0 else 0  # daily sharpe annualized

        print(f"\nP&L:")
        print(f"  total realized:         ${total_pnl:+.2f}")
        print(f"  total $ risked:         ${total_risked:.2f}")
        print(f"  roi:                    {100*total_pnl/total_risked:+.1f}%" if total_risked else "  roi: n/a")
        print(f"  wins / losses:          {wins} / {losses}")
        print(f"  win rate:               {win_rate*100:.1f}%")
        print(f"  avg P&L per trade:      ${avg_pnl:+.4f}")
        print(f"  std P&L:                ${std:.4f}")
        print(f"  pseudo-sharpe (daily):  {sharpe:.2f}")

        # by side
        print(f"\nby side:")
        for side in ("yes", "no"):
            side_pnls = [p for p, s_ in zip(pnls, sides) if s_ == side]
            if side_pnls:
                print(
                    f"  {side}:  n={len(side_pnls)}  pnl=${sum(side_pnls):+.2f}  "
                    f"win_rate={100*sum(1 for p in side_pnls if p>0)/len(side_pnls):.1f}%"
                )

        # calibration: pred bucket -> actual hit rate
        # FIX (bug B): nearest prediction-at-or-before fill.ts, not cartesian join.
        # the old naive `Fill.ticker == Prediction.ticker` inflated bucket counts
        # by ~N predictions per ticker (e.g. n=31 reported when only 1 fill actually
        # lived in that bucket).
        print(f"\nmodel calibration (predicted prob bucket -> actual win rate):")
        fill_rows = s.execute(
            select(Fill.ticker, Fill.ts, Fill.side, Fill.realized_pnl)
            .where(Fill.realized_pnl.isnot(None))
        ).all()

        buckets: dict[tuple[float, float], list[int]] = defaultdict(list)
        for ticker, fill_ts, side, pnl in fill_rows:
            nearest = s.execute(
                select(Prediction.predicted_prob)
                .where(Prediction.ticker == ticker)
                .where(Prediction.ts <= fill_ts)
                .order_by(desc(Prediction.ts))
                .limit(1)
            ).first()
            if nearest is None:
                # fallback: first prediction after fill (shouldn't happen)
                nearest = s.execute(
                    select(Prediction.predicted_prob)
                    .where(Prediction.ticker == ticker)
                    .order_by(Prediction.ts)
                    .limit(1)
                ).first()
            if nearest is None:
                continue
            pred_prob = nearest[0]
            # what prob did model assign to the SIDE we took?
            our_side_prob = pred_prob if side == "yes" else (1 - pred_prob)
            won = 1 if pnl > 0 else 0
            for lo, hi in [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 0.95), (0.95, 1.01)]:
                if lo <= our_side_prob < hi:
                    buckets[(lo, hi)].append(won)
                    break

        for (lo, hi), wins_list in sorted(buckets.items()):
            n = len(wins_list)
            actual = sum(wins_list) / n if n else 0
            mid = (lo + hi) / 2
            print(f"  predicted [{lo:.2f}, {hi:.2f}) n={n:3d}  actual_win_rate={actual*100:.1f}%  (model midpoint={mid*100:.0f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
