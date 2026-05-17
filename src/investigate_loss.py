"""investigate WHY the model lost — pick a few specific bad fills, show:
  - the kalshi market detail (strike_type, floor_strike, cap_strike, etc.)
  - the actual settlement result
  - the fill we made
  - what we BELIEVED vs what actually happened

run: .venv\\Scripts\\python.exe -m src.investigate_loss
"""
from __future__ import annotations

import json
import sys

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from sqlalchemy import desc, select  # noqa: E402

from src.db import Fill, session_scope  # noqa: E402
from src.kalshi.client import KalshiClient  # noqa: E402


def main() -> int:
    with session_scope() as s:
        # pick 5 worst losers + 5 winners
        losers = s.execute(
            select(Fill).where(Fill.realized_pnl.isnot(None)).order_by(Fill.realized_pnl).limit(5)
        ).scalars().all()
        winners = s.execute(
            select(Fill).where(Fill.realized_pnl.isnot(None)).order_by(desc(Fill.realized_pnl)).limit(5)
        ).scalars().all()

    samples = list(losers) + list(winners)

    with KalshiClient() as k:
        for f in samples:
            print("=" * 80)
            label = "WINNER" if f.realized_pnl > 0 else "LOSER"
            print(f"[{label}] {f.ticker}")
            print(f"  fill: side={f.side} price={f.price_cents}c x{f.contracts} cost=${f.cost_usd:.2f} pnl=${f.realized_pnl:+.2f}")
            try:
                detail = k.get_market(f.ticker)
                m = detail.get("market", {})
                # show the fields that determine semantics
                print(f"  strike_type:   {m.get('strike_type')}")
                print(f"  floor_strike:  {m.get('floor_strike')}")
                print(f"  cap_strike:    {m.get('cap_strike')}")
                print(f"  yes_sub_title: {m.get('yes_sub_title')}")
                print(f"  no_sub_title:  {m.get('no_sub_title')}")
                print(f"  subtitle:      {m.get('subtitle')}")
                print(f"  title:         {(m.get('title') or '')[:60]}")
                print(f"  result:        {m.get('result')!r}")
                print(f"  rules:         {(m.get('rules_primary') or '')[:120]}")
            except Exception as e:
                print(f"  ERROR fetching detail: {e}")
            print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
