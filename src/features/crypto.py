"""crypto feature extraction.

input: (market_dict, snapshot_dict, spot_price, vol_30d, baseline_predicted_prob)
output: dict of features for a single row

design notes:
- all features are scalars (numeric)
- missing features are filled with sentinel values (-1 for prices, 0 for counts)
- baseline_predicted_prob is used as a feature — the ML model learns to
  IMPROVE on the baseline, not replace it from scratch (residual learning)
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from src.strategy.crypto_vol import parse_ticker


def extract_crypto_features(
    market: dict[str, Any],
    snapshot: dict[str, Any],
    spot: float,
    vol_30d: float,
    baseline_prob: float | None,
) -> dict[str, float] | None:
    """Extract a feature dict for one crypto market snapshot. Returns None if
    ticker unparseable.
    """
    ticker = market.get("ticker", "")
    parsed = parse_ticker(ticker)
    if not parsed:
        return None
    pair, direction, strike = parsed

    # time to expiry
    close_ts = market.get("close_ts")
    if close_ts is None:
        return None
    if isinstance(close_ts, str):
        close_ts = datetime.fromisoformat(close_ts.replace("Z", "+00:00"))
    if close_ts.tzinfo is None:
        close_ts = close_ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    seconds_to_expiry = max(1, (close_ts - now).total_seconds())
    hours_to_expiry = seconds_to_expiry / 3600.0

    # microstructure
    yes_bid = snapshot.get("yes_bid") or 0
    yes_ask = snapshot.get("yes_ask") or 0
    no_bid = snapshot.get("no_bid") or 0
    no_ask = snapshot.get("no_ask") or 0
    last_price = snapshot.get("last_price") or 0
    volume = snapshot.get("volume") or 0
    open_interest = snapshot.get("open_interest") or 0

    yes_mid = (yes_bid + yes_ask) / 2.0 if (yes_bid > 0 and yes_ask > 0) else (yes_ask or 0)
    spread = max(0, yes_ask - yes_bid)
    spread_pct = spread / yes_mid if yes_mid > 0 else 0.0

    # moneyness
    log_moneyness = math.log(spot / strike) if strike > 0 and spot > 0 else 0.0
    moneyness = (spot - strike) / strike if strike > 0 else 0.0
    is_above_direction = 1 if direction == "above" else 0

    # vol-adjusted distance to strike (in "sigma units")
    T_years = seconds_to_expiry / (365 * 86400)
    sigma_t = vol_30d * math.sqrt(T_years) if T_years > 0 else 0.0001
    z_score = log_moneyness / sigma_t if sigma_t > 0 else 0.0

    return {
        # market structure
        "ticker_above": is_above_direction,
        "strike_usd": float(strike),
        # time
        "hours_to_expiry": hours_to_expiry,
        "log_hours_to_expiry": math.log(max(hours_to_expiry, 0.01)),
        # spot + moneyness
        "spot_usd": spot,
        "log_moneyness": log_moneyness,
        "moneyness_pct": moneyness,
        "z_score": z_score,             # most predictive single feature
        "abs_z_score": abs(z_score),
        # vol
        "realized_vol_30d": vol_30d,
        "sigma_t": sigma_t,
        # microstructure
        "yes_bid_cents": float(yes_bid),
        "yes_ask_cents": float(yes_ask),
        "yes_mid_cents": yes_mid,
        "spread_cents": float(spread),
        "spread_pct": spread_pct,
        "last_price_cents": float(last_price),
        "volume": float(volume),
        "open_interest": float(open_interest),
        # baseline residual learning
        "baseline_prob": baseline_prob if baseline_prob is not None else 0.5,
    }


# canonical feature order for training (xgboost cares about column order)
CRYPTO_FEATURE_COLUMNS = [
    "ticker_above",
    "strike_usd",
    "hours_to_expiry",
    "log_hours_to_expiry",
    "spot_usd",
    "log_moneyness",
    "moneyness_pct",
    "z_score",
    "abs_z_score",
    "realized_vol_30d",
    "sigma_t",
    "yes_bid_cents",
    "yes_ask_cents",
    "yes_mid_cents",
    "spread_cents",
    "spread_pct",
    "last_price_cents",
    "volume",
    "open_interest",
    "baseline_prob",
]
