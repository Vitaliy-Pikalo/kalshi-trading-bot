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
import pickle
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from sqlalchemy import desc, select  # noqa: E402

from src.config import settings  # noqa: E402
from src.db import Fill, Market, Prediction as PredRow, Snapshot, session_scope  # noqa: E402
from src.fees import kalshi_fee_usd  # noqa: E402
from src.strategy.base import Baseline  # noqa: E402
from src.strategy.crypto_vol import CryptoVolBaseline  # noqa: E402
from src.strategy.tennis_elo import TennisEloBaseline  # noqa: E402

# baselines registered: paper sim runs each market through each baseline
# that returns a non-None prediction. each baseline returns None for markets
# it doesn't handle, so registering all is safe.
BASELINES: list[Baseline] = [CryptoVolBaseline(), TennisEloBaseline()]

# calibration models — loaded lazily on first use
_CALIBRATION_MODELS: dict[str, object] | None = None
_CALIBRATION_DIR = Path("data/calibration")


def _load_calibration_models() -> dict[str, object]:
    """load isotonic models per strike_type from data/calibration/iso_v1_*.pkl.

    called once on first paper_trade_once() with use_calibration=True.
    returns dict: {"between": IsotonicRegression, "greater": IsotonicRegression, ...}
    raises FileNotFoundError if a model file is missing.
    """
    models: dict[str, object] = {}
    for strike_type in ("between", "greater"):
        path = _CALIBRATION_DIR / f"iso_v1_{strike_type}.pkl"
        if not path.exists():
            raise FileNotFoundError(
                f"calibration model not found: {path}. "
                f"run _phase2_fit_isotonic.bat first."
            )
        with path.open("rb") as f:
            models[strike_type] = pickle.load(f)
    return models


def _apply_calibration(
    raw_prob: float,
    strike_type: str | None,
    models: dict[str, object],
) -> float:
    """apply isotonic calibration to a raw predicted prob.

    if no model exists for this strike_type, returns raw_prob unchanged.
    """
    model = models.get(strike_type or "")
    if model is None:
        return raw_prob
    try:
        import numpy as np
        calibrated = float(model.predict(np.array([raw_prob]))[0])
        return max(0.0, min(1.0, calibrated))
    except Exception:
        return raw_prob  # fall back to raw on any error


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
    allowed_strike_types: list[str] | None = None,
    use_calibration: bool = False,
    fee_aware: bool = False,
    fee_edge_margin: float = 0.0,
) -> dict:
    """One pass over recent (market, latest_snapshot) pairs. Returns stats.

    dedup_window_minutes: skip ticker if we've placed a paper fill within this window
    max_credible_edge: skip if |edge| > this (almost always model bug, not real edge)
    min_price_cents / max_price_cents: avoid 1¢ and 99¢ markets (huge round-trip cost)
    allowed_strike_types: if set (e.g. ["between"]), only place fills for those strike types.
        predictions are still written for ALL markets so calibration analysis stays intact.
    use_calibration: if True, apply isotonic calibration (loaded from data/calibration/)
        to predicted_prob before edge calc. model_version gets '+iso_v1' suffix so
        diagnose can A/B compare calibrated vs uncalibrated fills.
    fee_aware: if True, require the edge to exceed the Kalshi fee expressed as a
        fraction of the stake, plus fee_edge_margin. See the gate below for why.
    fee_edge_margin: extra edge required on top of the fee, e.g. 0.02 for 2pp.
    """
    global _CALIBRATION_MODELS
    if use_calibration and _CALIBRATION_MODELS is None:
        _CALIBRATION_MODELS = _load_calibration_models()

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
        "trades_skipped_strike_type": 0,
        "trades_skipped_fee": 0,
        # These three paths used to `continue` silently, so a TRADE line reading
        # "seen=12 preds=12 fills=0 skip(dedup=1, edge=0, price=2, strike_type=1)"
        # left 8 markets unaccounted for and gave no way to tell whether a preset
        # produced zero fills because of min_edge, liquidity, or a NaN prediction.
        # Counting them is the difference between diagnosing a preset in one log
        # line and guessing at it for days.
        "trades_skipped_min_edge": 0,
        "trades_skipped_nan_edge": 0,
        "trades_skipped_no_liquidity": 0,
        "trades_skipped_zero_stake": 0,
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
                "event_ticker": m.event_ticker,
                "strike_type": m.strike_type,
                "floor_strike": m.floor_strike,
                "cap_strike": m.cap_strike,
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

                # apply isotonic calibration if enabled
                # calibration maps raw predicted_prob → calibrated_prob per strike_type,
                # then recalculates edge from the calibrated prob.
                # model_version gets '+iso_v1' suffix so diagnose can A/B compare.
                if use_calibration and _CALIBRATION_MODELS is not None:
                    cal_prob = _apply_calibration(
                        pred.predicted_prob, m.strike_type, _CALIBRATION_MODELS
                    )
                    cal_edge = cal_prob - pred.market_implied_prob
                    effective_prob = cal_prob
                    effective_edge = cal_edge
                    model_version = f"{bl.name}+iso_v1"
                else:
                    effective_prob = pred.predicted_prob
                    effective_edge = pred.edge
                    model_version = bl.name

                # write the prediction row (always — useful for calibration later)
                s.add(
                    PredRow(
                        ticker=m.ticker,
                        ts=now,
                        model_version=model_version,
                        predicted_prob=effective_prob,
                        market_implied_prob=pred.market_implied_prob,
                        edge=effective_edge if not math.isnan(effective_edge) else 0.0,
                    )
                )
                stats["predictions_written"] += 1

                # paper trade only if edge is real
                if math.isnan(effective_edge):
                    stats["trades_skipped_nan_edge"] += 1
                    continue
                if require_liquidity and (snap.yes_ask is None or snap.yes_ask <= 0):
                    stats["trades_skipped_no_liquidity"] += 1
                    continue
                if abs(effective_edge) < min_edge:
                    stats["trades_skipped_min_edge"] += 1
                    continue

                # strike_type allowlist (e.g. only "between" range markets in range_v1 preset)
                # gate is here (not at market_rows level) so predictions are still written
                # for diagnostic / calibration purposes on excluded markets.
                if allowed_strike_types is not None and m.strike_type not in allowed_strike_types:
                    stats["trades_skipped_strike_type"] += 1
                    continue

                # dedup check
                if m.ticker in recent_fill_tickers:
                    stats["trades_skipped_dedup"] += 1
                    continue

                # max-credible-edge gate: massive edges are usually model bugs
                if abs(effective_edge) > max_credible_edge:
                    stats["trades_skipped_edge_too_big"] += 1
                    continue

                stats["edge_distribution"].append(effective_edge)

                # which side? if predicted > implied, buy YES at yes_ask
                # if predicted < implied, buy NO at no_ask
                if effective_edge > 0:
                    side = "yes"
                    price_cents = snap.yes_ask or 0
                else:
                    side = "no"
                    price_cents = snap.no_ask or 0
                if price_cents <= 0 or price_cents >= 100:
                    stats["trades_skipped_price_range"] += 1
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
                    # Kelly returned nothing. With a $100 bankroll and a 1% cap
                    # the risk budget is $1.00, so this fires whenever the edge
                    # is too thin for fractional Kelly to justify a single
                    # contract. Worth counting separately from a gate rejection:
                    # it means the SIZER declined, not the strategy.
                    stats["trades_skipped_zero_stake"] += 1
                    continue
                contracts = max(1, int(stake / (price_cents / 100.0)))

                # Fee-aware edge gate.
                #
                # Kalshi charges roundup(0.07 * C * P * (1-P)) on the opening
                # order. As a fraction of the stake that is fee / (C * P), which
                # at one contract is ~2.7% at 75c and ~10.5% at 19c -- the
                # round-up dominates on single-contract orders, and it is worst
                # at the cheap end where the stake is small.
                #
                # An edge smaller than that fraction is a guaranteed loss no
                # matter how well calibrated the model is. Measured on 355 v4
                # fills: the 70-90c bucket earned +2.70% gross and +0.01% NET.
                # The book had converged on a trade that is arithmetically the
                # fee, because nothing in this decision path had ever seen one.
                #
                # This gate is derived from the fee formula, not fitted to a
                # bucket table -- which is deliberately unlike the 70-80c price
                # window in range_v3, chosen off n=115 gross-of-fees fills.
                if fee_aware:
                    stake_usd = contracts * price_cents / 100.0
                    fee_frac = kalshi_fee_usd(price_cents, contracts) / stake_usd
                    if abs(effective_edge) < fee_frac + fee_edge_margin:
                        stats["trades_skipped_fee"] += 1
                        continue

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
    parser.add_argument("--use-calibration", action="store_true",
                        help="apply isotonic calibration from data/calibration/iso_v1_*.pkl")
    args = parser.parse_args()

    started = time.time()
    cycle = 0
    while True:
        cycle += 1
        t0 = time.time()
        stats = paper_trade_once(
            min_edge=args.min_edge,
            max_markets=args.max_markets,
            use_calibration=args.use_calibration,
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
            f"price={stats['trades_skipped_price_range']}, "
            f"strike_type={stats['trades_skipped_strike_type']})  "
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
