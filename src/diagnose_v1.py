"""v1 deep-dive diagnostic — break down the 51 fills by every dimension.

fixes the cartesian-product bug in src/analyze.py calibration by joining each
fill to its NEAREST prediction by timestamp instead of all predictions per ticker.

run: .venv\\Scripts\\python.exe -m src.diagnose_v1
     .venv\\Scripts\\python.exe -m src.diagnose_v1 --min-fill-id 135   # v3 only
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta

from src.utils import setup_utf8_stdout, utcnow_naive

setup_utf8_stdout()

from sqlalchemy import desc, func, select  # noqa: E402

from src.db import Fill, Market, Prediction, session_scope  # noqa: E402


def _bucket_days(days: float) -> str:
    if days < 0:
        return "expired"
    if days < 1:
        return "<1d"
    if days < 7:
        return "1-7d"
    if days < 30:
        return "7-30d"
    return "30d+"


def _bucket_price(c: int) -> str:
    if c < 10:
        return "0-10c"
    if c < 30:
        return "10-30c"
    if c < 70:
        return "30-70c"
    if c < 90:
        return "70-90c"
    return "90-100c"


def hr(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--min-fill-id",
        type=int,
        default=0,
        help="exclude fills with id < this value (use 135 for v3 range_v1 era only)",
    )
    args = parser.parse_args()
    min_fid = args.min_fill_id

    with session_scope() as s:
        # ------------------------------------------------------------ overview
        hr("OVERVIEW")
        if min_fid > 0:
            print(f"[FILTER ACTIVE] only counting fills with id >= {min_fid}")
        total = s.scalar(
            select(func.count()).select_from(Fill).where(Fill.id >= min_fid)
        )
        settled = s.scalar(
            select(func.count()).select_from(Fill)
            .where(Fill.id >= min_fid)
            .where(Fill.realized_pnl.isnot(None))
        )
        print(f"total fills:    {total}")
        print(f"  settled:      {settled}")
        print(f"  unsettled:    {total - settled}")

        # ------------------------------------------------- all settled fills
        hr("ALL SETTLED FILLS (chronological)")
        settled_rows = s.execute(
            select(
                Fill.id, Fill.ts, Fill.ticker, Fill.side, Fill.price_cents,
                Fill.contracts, Fill.cost_usd, Fill.realized_pnl,
                Market.series_ticker, Market.strike_type, Market.floor_strike,
                Market.cap_strike, Market.close_ts, Market.settled_outcome,
                Market.title,
            ).join(Market, Market.ticker == Fill.ticker)
            .where(Fill.id >= min_fid)
            .where(Fill.realized_pnl.isnot(None))
            .order_by(Fill.ts)
        ).all()

        print(f"{'id':>4} {'ts':16} {'series':10} {'sty':8} {'side':>4} "
              f"{'px':>4} {'qty':>4} {'cost':>6} {'pnl':>7} {'outc':>4} title")
        print("-" * 130)
        for r in settled_rows:
            ts = r.ts.strftime("%m-%d %H:%M") if r.ts else "?"
            sty = (r.strike_type or "-")[:8]
            outc = (r.settled_outcome or "?")[:4]
            print(f"{r.id:>4} {ts:16} {r.series_ticker[:10]:10} {sty:8} {r.side:>4} "
                  f"{r.price_cents:>4} {r.contracts:>4} ${r.cost_usd:>4.2f} "
                  f"${r.realized_pnl:+6.2f} {outc:>4} {(r.title or '')[:55]}")

        # ----------------------------------------- unsettled (still-open) fills
        hr("UNSETTLED FILLS — what are we waiting on?")
        unsettled = s.execute(
            select(
                Fill.id, Fill.ts, Fill.ticker, Fill.side, Fill.price_cents,
                Fill.cost_usd, Market.series_ticker, Market.close_ts, Market.title,
            ).join(Market, Market.ticker == Fill.ticker)
            .where(Fill.id >= min_fid)
            .where(Fill.realized_pnl.is_(None))
            .order_by(Market.close_ts)
        ).all()

        now = utcnow_naive()
        bucket_counts: dict[str, int] = defaultdict(int)
        bucket_cost: dict[str, float] = defaultdict(float)
        print(f"{'id':>4} {'series':10} {'side':>4} {'px':>4} {'cost':>6} "
              f"{'close_ts':16} {'dte':>6} title")
        print("-" * 130)
        for r in unsettled:
            if r.close_ts:
                dte = (r.close_ts - now).total_seconds() / 86400.0
                bucket = _bucket_days(dte)
                dte_str = f"{dte:>5.1f}d"
                close_str = r.close_ts.strftime("%m-%d %H:%M")
            else:
                dte = 999
                bucket = "no_close"
                dte_str = "  ?  "
                close_str = "?"
            bucket_counts[bucket] += 1
            bucket_cost[bucket] += r.cost_usd
            print(f"{r.id:>4} {r.series_ticker[:10]:10} {r.side:>4} {r.price_cents:>4} "
                  f"${r.cost_usd:>4.2f} {close_str:16} {dte_str:>6} {(r.title or '')[:60]}")

        print(f"\n  unsettled by days-to-expiry:")
        for b in ("expired", "<1d", "1-7d", "7-30d", "30d+", "no_close"):
            if bucket_counts.get(b):
                print(f"    {b:10} n={bucket_counts[b]:3d}  cost=${bucket_cost[b]:6.2f}")

        # ----------------------------------------- by series_ticker (settled)
        hr("SETTLED P&L BY SERIES_TICKER")
        by_series: dict[str, list[float]] = defaultdict(list)
        by_series_cost: dict[str, float] = defaultdict(float)
        for r in settled_rows:
            by_series[r.series_ticker].append(r.realized_pnl)
            by_series_cost[r.series_ticker] += r.cost_usd
        print(f"{'series':12} {'n':>3} {'wins':>4} {'win%':>6} {'cost':>7} "
              f"{'pnl':>8} {'roi%':>7}")
        print("-" * 60)
        for series, pnls in sorted(by_series.items(), key=lambda x: -sum(x[1])):
            n = len(pnls)
            w = sum(1 for p in pnls if p > 0)
            total_pnl = sum(pnls)
            cost = by_series_cost[series]
            roi = 100 * total_pnl / cost if cost else 0
            print(f"{series:12} {n:>3} {w:>4} {100*w/n:>5.1f}% ${cost:>5.2f} "
                  f"${total_pnl:+7.2f} {roi:>+6.1f}%")

        # ----------------------------------------- by strike_type (settled)
        hr("SETTLED P&L BY STRIKE_TYPE")
        by_sty: dict[str, list[float]] = defaultdict(list)
        by_sty_cost: dict[str, float] = defaultdict(float)
        for r in settled_rows:
            k = r.strike_type or "none"
            by_sty[k].append(r.realized_pnl)
            by_sty_cost[k] += r.cost_usd
        print(f"{'strike_type':14} {'n':>3} {'wins':>4} {'win%':>6} "
              f"{'cost':>7} {'pnl':>8} {'roi%':>7}")
        print("-" * 60)
        for k, pnls in sorted(by_sty.items(), key=lambda x: -sum(x[1])):
            n = len(pnls)
            w = sum(1 for p in pnls if p > 0)
            total_pnl = sum(pnls)
            cost = by_sty_cost[k]
            roi = 100 * total_pnl / cost if cost else 0
            print(f"{k:14} {n:>3} {w:>4} {100*w/n:>5.1f}% ${cost:>5.2f} "
                  f"${total_pnl:+7.2f} {roi:>+6.1f}%")

        # ----------------------------------------- by price bucket
        hr("SETTLED P&L BY ENTRY PRICE BUCKET (where the edge actually was)")
        by_px: dict[str, list[float]] = defaultdict(list)
        by_px_cost: dict[str, float] = defaultdict(float)
        for r in settled_rows:
            b = _bucket_price(r.price_cents)
            by_px[b].append(r.realized_pnl)
            by_px_cost[b] += r.cost_usd
        print(f"{'price':10} {'n':>3} {'wins':>4} {'win%':>6} {'cost':>7} "
              f"{'pnl':>8} {'roi%':>7}")
        print("-" * 60)
        order = ["0-10c", "10-30c", "30-70c", "70-90c", "90-100c"]
        for b in order:
            if b in by_px:
                pnls = by_px[b]
                n = len(pnls)
                w = sum(1 for p in pnls if p > 0)
                total_pnl = sum(pnls)
                cost = by_px_cost[b]
                roi = 100 * total_pnl / cost if cost else 0
                print(f"{b:10} {n:>3} {w:>4} {100*w/n:>5.1f}% ${cost:>5.2f} "
                      f"${total_pnl:+7.2f} {roi:>+6.1f}%")

        # ----------------------------------------- by side (settled)
        hr("SETTLED P&L BY SIDE")
        by_side: dict[str, list[float]] = defaultdict(list)
        by_side_cost: dict[str, float] = defaultdict(float)
        for r in settled_rows:
            by_side[r.side].append(r.realized_pnl)
            by_side_cost[r.side] += r.cost_usd
        for side in ("yes", "no"):
            if side in by_side:
                pnls = by_side[side]
                n = len(pnls)
                w = sum(1 for p in pnls if p > 0)
                cost = by_side_cost[side]
                total_pnl = sum(pnls)
                roi = 100 * total_pnl / cost if cost else 0
                print(f"  {side}: n={n} wins={w} win%={100*w/n:.1f}% "
                      f"cost=${cost:.2f} pnl=${total_pnl:+.2f} roi={roi:+.1f}%")

        # ------------------------------------------ FIXED calibration
        hr("CALIBRATION (FIXED — nearest prediction per fill, not cartesian)")
        # For each settled fill, pull the most recent prediction at or before fill.ts
        cal_buckets: dict[tuple[float, float], list[int]] = defaultdict(list)
        cal_details: dict[tuple[float, float], list[tuple]] = defaultdict(list)

        for r in settled_rows:
            # nearest prediction at-or-before this fill
            nearest = s.execute(
                select(Prediction.predicted_prob, Prediction.edge, Prediction.ts)
                .where(Prediction.ticker == r.ticker)
                .where(Prediction.ts <= r.ts)
                .order_by(desc(Prediction.ts))
                .limit(1)
            ).first()

            if nearest is None:
                # fallback: nearest prediction AFTER fill (shouldn't happen but safe)
                nearest = s.execute(
                    select(Prediction.predicted_prob, Prediction.edge, Prediction.ts)
                    .where(Prediction.ticker == r.ticker)
                    .order_by(Prediction.ts)
                    .limit(1)
                ).first()

            if nearest is None:
                continue

            pred_prob = nearest[0]
            edge = nearest[1]
            our_side_prob = pred_prob if r.side == "yes" else (1 - pred_prob)
            won = 1 if r.realized_pnl > 0 else 0

            for lo, hi in [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6),
                           (0.6, 0.8), (0.8, 0.95), (0.95, 1.01)]:
                if lo <= our_side_prob < hi:
                    cal_buckets[(lo, hi)].append(won)
                    cal_details[(lo, hi)].append(
                        (r.ticker, r.side, r.price_cents, pred_prob, edge,
                         r.realized_pnl, r.title)
                    )
                    break

        print(f"{'bucket':16} {'n':>3} {'wins':>4} {'actual%':>8} {'expected%':>9}")
        for (lo, hi), wins_list in sorted(cal_buckets.items()):
            n = len(wins_list)
            actual = 100 * sum(wins_list) / n if n else 0
            mid = 100 * (lo + hi) / 2
            print(f"  [{lo:.2f},{hi:.2f})    {n:>3} {sum(wins_list):>4} "
                  f"{actual:>7.1f}% {mid:>8.0f}%")

        # show every fill in the [0.40, 0.60) bucket if it had losers
        if (0.4, 0.6) in cal_details:
            hr("FILLS IN [0.40, 0.60) BUCKET (model said coinflip)")
            print(f"{'ticker':40} {'side':>4} {'px':>3} {'pred':>5} "
                  f"{'edge':>5} {'pnl':>6}  title")
            print("-" * 130)
            for ticker, side, px, pp, edge, pnl, title in cal_details[(0.4, 0.6)]:
                print(f"{ticker[:40]:40} {side:>4} {px:>3} {pp:>5.2f} "
                      f"{edge:+5.2f} ${pnl:+5.2f}  {(title or '')[:50]}")

        # ----------------------------------------- top + bottom trades
        hr("TOP 10 WINNERS")
        winners = sorted(settled_rows, key=lambda r: -r.realized_pnl)[:10]
        for r in winners:
            print(f"  ${r.realized_pnl:+6.2f}  {r.side:>3}@{r.price_cents:>3}c  "
                  f"{r.ticker[:40]:40}  {(r.title or '')[:50]}")

        hr("TOP 10 LOSERS")
        losers = sorted(settled_rows, key=lambda r: r.realized_pnl)[:10]
        for r in losers:
            print(f"  ${r.realized_pnl:+6.2f}  {r.side:>3}@{r.price_cents:>3}c  "
                  f"{r.ticker[:40]:40}  {(r.title or '')[:50]}")

        # ----------------------------------------- predictions outside fills
        hr("ALL PREDICTIONS — prob distribution (sanity check)")
        # Predicted-prob histogram across ALL predictions (not just filled)
        pred_probs = s.execute(select(Prediction.predicted_prob)).all()
        hist: dict[str, int] = defaultdict(int)
        for (p,) in pred_probs:
            for lo, hi in [(0.0, 0.05), (0.05, 0.2), (0.2, 0.4),
                           (0.4, 0.6), (0.6, 0.8), (0.8, 0.95), (0.95, 1.01)]:
                if lo <= p < hi:
                    hist[f"[{lo:.2f},{hi:.2f})"] += 1
                    break
        total_p = sum(hist.values())
        print(f"total predictions in db: {total_p}")
        for b in ["[0.00,0.05)", "[0.05,0.20)", "[0.20,0.40)",
                  "[0.40,0.60)", "[0.60,0.80)", "[0.80,0.95)", "[0.95,1.01)"]:
            n = hist.get(b, 0)
            pct = 100 * n / total_p if total_p else 0
            bar = "#" * int(pct / 2)
            print(f"  {b:14} n={n:5d}  {pct:5.1f}%  {bar}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
