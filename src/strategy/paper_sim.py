"""paper trade simulator.

for each (market, snapshot) pair:
  1. run a baseline -> Prediction
  2. write Prediction row regardless
  3. if abs(edge) > threshold AND market has liquidity, also write a paper Fill

usage:
    .venv\\Scripts\\python.exe -m src.strategy.paper_sim --once
    .venv\\Scripts\\python.exe -m src.strategy.paper_sim --interval 60 --duration 600
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import datetime, timedelta, timezone

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from sqlalchemy import desc, select  # noqa: E402

from src.config import settings  # noqa: E402
from src.db import Fill, Market, Prediction as PredRow, Snapshot, session_scope  # noqa: E402
from src.strategy.base import Baseline  # noqa: E402
from src.strategy.crypto_vol import CryptoVolBaseline  # noqa: E402
from src.strategy.tennis_elo import TennisEloBaseline  # noqa: E402

# baselines registered: paper sim runs each market through each baseline
# that returns a non-None prediction. each baseline returns None for markets
# it doesn't handle, so registering all is safe.
BASELINES: list[Baseline] = [CryptoVolBaseline(), TennisEloBaseline()]


def kelly_size(
    bankroll: float,
    edge: float,
    odds_decimal: float,
    kelly_fraction: float = 0.25,
    max_pct: float = 0.01,
) -> float:
    """Returns USD amount to risk per fractional Kelly.

    edge = your prob - market implied prob
    odds_decimal = payout multiplier (1 / market_implied)
    kelly_fraction = scale of full kelly (0.25 = quarter-kelly, poker standard)
    max_pct = hard cap, e.g. 1% of bankroll per position
    """
    if edge <= 0 or odds_decimal <= 1:
        return 0.0
    # f* = (b*p - q) / b where b = odds-1, p = your prob, q = 1-p
    b = odds_decimal - 1.0
    p = edge + (1.0 / odds_decimal)  # your prob
    q = 1.0 - p
    f_star = (b * p - q) / b
    f_used = max(0.0, min(f_star * kelly_fraction, max_pct))
    return bankroll * f_used


def paper_trade_once(
    min_edge: float | None = None,
    max_markets: int = 500,
    require_liquidity: bool = True,
    dedup_window_minutes: int = 30,
    max_credible_edge: float = 0.50,
    min_price_cents: int = 2,
    max_price_cents: int = 98,
    min_volume: int = 0,
) -> dict:
    """One pass over recent (market, latest_snapshot) pairs. Returns stats.

    dedup_window_minutes: skip ticker if we've placed a paper fill within this window
    max_credible_edge: skip if |edge| > this (almost always model bug, not real edge)
    min_price_cents / max_price_cents: avoid 1¢ and 99¢ markets (huge round-trip cost)
    """
    if min_edge is None:
        min_edge = settings.min_edge_pct / 100.0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    dedup_cutoff = now - timedelta(minutes=dedup_window_minutes)
    stats = {
        "markets_seen": 0,
        "predictions_written": 0,
        "trades_paper": 0,
        "trades_skipped_dedup": 0,
        "trades_skipped_edge_too_big": 0,
        "trades_skipped_price_range": 0,
        "edge_distribution": [],
    }

    with session_scope() as s:
        # tickers we've already paper-traded recently
        recent_fill_tickers = set(
            r[0] for r in s.execute(
                select(Fill.ticker)
                .where(Fill.is_paper == 1)
                .where(Fill.ts > dedup_cutoff)
                .distinct()
            ).all()
        )

        # markets closing in next 48h with at least one snapshot
        market_rows = s.execute(
            select(Market)
            .where(Market.close_ts > now)
            .order_by(Market.close_ts)
            .limit(max_markets)
        ).scalars().all()

        for m in market_rows:
            # latest snapshot for this ticker
            snap = s.execute(
                select(Snapshot)
                .where(Snapshot.ticker == m.ticker)
                .order_by(desc(Snapshot.ts))
                .limit(1)
            ).scalar_one_or_none()

            if snap is None:
                continue
            # volume filter — dead markets are mostly market-maker phantom orders
            if min_volume > 0 and (snap.volume or 0) < min_volume:
                continue
            stats["markets_seen"] += 1

            mdict = {
                "ticker": m.ticker,
                "close_ts": m.close_ts,
                "title": m.title,
            }
            sdict = {
                "yes_bid": snap.yes_bid,
                "yes_ask": snap.yes_ask,
                "no_bid": snap.no_bid,
                "no_ask": snap.no_ask,
            }

            for bl in BASELINES:
                pred = bl.predict(mdict, sdict)
                if pred is None:
                    continue
                # write the prediction row (always — useful for calibration later)
                s.add(
                    PredRow(
                        ticker=m.ticker,
                        ts=now,
                        model_version=bl.name,
                        predicted_prob=pred.predicted_prob,
                        market_implied_prob=pred.market_implied_prob,
                        edge=pred.edge if not math.isnan(pred.edge) else 0.0,
                    )
                )
                stats["predictions_written"] += 1

                # paper trade only if edge is real
                if math.isnan(pred.edge):
                    continue
                if require_liquidity and (snap.yes_ask is None or snap.yes_ask <= 0):
                    continue
                if abs(pred.edge) < min_edge:
                    continue

                # dedup check
                if m.ticker in recent_fill_tickers:
                    stats["trades_skipped_dedup"] += 1
                    continue

                # max-credible-edge gate: massive edges are usually model bugs
                if abs(pred.edge) > max_credible_edge:
                    stats["trades_skipped_edge_too_big"] += 1
                    continue

                stats["edge_distribution"].append(pred.edge)

                # which side? if predicted > implied, buy YES at yes_ask
                # if predicted < implied, buy NO at no_ask
                if pred.edge > 0:
                    side = "yes"
                    price_cents = snap.yes_ask or 0
                else:
                    side = "no"
                    price_cents = snap.no_ask or 0
                if price_cents <= 0 or price_cents >= 100:
                    continue
                # min/max price gate: avoid 1¢ and 99¢ markets (huge spread cost)
                if price_cents < min_price_cents or price_cents > max_price_cents:
                    stats["trades_skipped_price_range"] += 1
                    continue

                # size with fractional kelly
                implied_for_side = price_cents / 100.0
                odds = 1.0 / implied_for_side if implied_for_side > 0 else 0
                stake = kelly_size(
                    bankroll=settings.bankroll_usd,
                    edge=abs(pred.edge),
                    odds_decimal=odds,
                    kelly_fraction=settings.kelly_fraction,
                    max_pct=settings.max_risk_per_trade_pct / 100.0,
                )
                if stake <= 0:
                    continue
                contracts = max(1, int(stake / (price_cents / 100.0)))

                s.add(
                    Fill(
                        ticker=m.ticker,
                        ts=now,
                        side=side,
                        price_cents=price_cents,
                        contracts=contracts,
                        cost_usd=contracts * price_cents / 100.0,
                        is_paper=1,
                        realized_pnl=None,
                    )
                )
                stats["trades_paper"] += 1

    return stats


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--duration", type=int, default=0)
    parser.add_argument("--min-edge", type=float, default=None, help="edge threshold (0-1)")
    parser.add_argument("--max-markets", type=int, default=500)
    args = parser.parse_args()

    started = time.time()
    cycle = 0
    while True:
        cycle += 1
        t0 = time.time()
        stats = paper_trade_once(
            min_edge=args.min_edge,
            max_markets=args.max_markets,
        )
        elapsed = time.time() - t0
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        edges = stats["edge_distribution"]
        edge_summary = (
            f"min={min(edges):+.3f} max={max(edges):+.3f} n={len(edges)}"
            if edges
            else "no edges met threshold"
        )
        print(
            f"[{ts}] cycle #{cycle}: seen={stats['markets_seen']:4d} "
            f"preds={stats['predictions_written']:4d} "
            f"paper_trades={stats['trades_paper']:3d} "
            f"skipped(dedup={stats['trades_skipped_dedup']}, "
            f"edge_too_big={stats['trades_skipped_edge_too_big']}, "
            f"price={stats['trades_skipped_price_range']})  "
            f"{edge_summary}  ({elapsed:.1f}s)"
        )

        if args.once:
            break
        if args.duration > 0 and time.time() - started >= args.duration:
            break
        remaining = args.interval - elapsed
        if remaining > 0:
            time.sleep(remaining)

    return 0


if __name__ == "__main__":
    sys.exit(main())
