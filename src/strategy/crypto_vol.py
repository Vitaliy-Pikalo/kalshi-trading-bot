"""crypto vol baseline.

prices kalshi BTC/ETH binary markets using a log-normal model + realized vol.
edge comes from the market mispricing implied vol (or stale prices vs spot).

ticker patterns we handle:
    KXBTC-{DATECODE}-T{STRIKE}      -> "yes if close >= strike"
    KXBTC-{DATECODE}-B{STRIKE}      -> "yes if close <= strike"
    KXBTCD-{DATECODE}-T{STRIKE}     -> same, daily binary
    KXBTCD-{DATECODE}-B{STRIKE}
    KXETH... same patterns
"""
from __future__ import annotations

import math
import re
import sys
from datetime import datetime, timezone
from functools import lru_cache

from src.spot.coinbase import get_realized_vol, get_spot_price
from src.strategy.base import Baseline, Prediction, implied_prob_from_snapshot

# extract strike + direction from ticker suffix
_TICKER_RE = re.compile(r"-(?P<dir>[TB])(?P<strike>\d+(?:\.\d+)?)", re.IGNORECASE)

# map ticker prefix -> coinbase pair
_TICKER_TO_PAIR = {
    "KXBTC": "BTC-USD",
    "KXBTCD": "BTC-USD",
    "KXBTC15M": "BTC-USD",
    "KXETH": "ETH-USD",
    "KXETHD": "ETH-USD",
    "KXETH15M": "ETH-USD",
}


def parse_ticker(ticker: str) -> tuple[str, str, float] | None:
    """Returns (pair, direction, strike) or None if unparseable.

    direction is 'above' (T = yes if close >= strike) or 'below' (B = yes if close <= strike).
    """
    # find which series this is
    pair = None
    for prefix, p in _TICKER_TO_PAIR.items():
        # series prefix is followed by - or another character
        if ticker.startswith(prefix + "-"):
            pair = p
            break
    if pair is None:
        return None

    m = _TICKER_RE.search(ticker)
    if not m:
        return None
    direction = "above" if m.group("dir").upper() == "T" else "below"
    strike = float(m.group("strike"))
    return pair, direction, strike


def log_normal_prob_above(spot: float, strike: float, sigma: float, T_years: float) -> float:
    """P(S_T >= K) under log-normal with σ vol, T years, drift = 0.

    Clipped to [0.005, 0.995] because real-world tail probabilities are never
    exactly zero — log-normal underestimates tails (BTC has fat tails). Clipping
    prevents the model from generating phantom 99% edges on far-OTM strikes.
    """
    if T_years <= 0:
        return 1.0 if spot >= strike else 0.0
    if sigma <= 0:
        return 1.0 if spot >= strike else 0.0
    # d2 = (ln(S/K) - 0.5σ²T) / (σ√T)
    d2 = (math.log(spot / strike) - 0.5 * sigma * sigma * T_years) / (
        sigma * math.sqrt(T_years)
    )
    raw = _normal_cdf(d2)
    # clip to acknowledge fat tails — pure log-normal way underestimates them
    return max(0.005, min(0.995, raw))


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# cache spot + vol for ~30s to avoid hammering coinbase during a poll
@lru_cache(maxsize=8)
def _spot_cached(pair: str, bucket: int) -> float:
    """bucket = int(time.time() / 30) gives 30s cache."""
    return get_spot_price(pair)


@lru_cache(maxsize=8)
def _vol_cached(pair: str, bucket: int) -> float:
    """bucket = int(time.time() / 3600) gives 1h cache for vol."""
    return get_realized_vol(pair, lookback_days=30)


class CryptoVolBaseline(Baseline):
    name = "crypto_vol_v1"  # v1: uses kalshi strike_type (was v0: ticker parsing — BUGGY)

    def predict(self, market: dict, snapshot: dict | None = None) -> Prediction | None:
        import time as _time

        ticker = market.get("ticker", "")
        # determine underlying pair from ticker prefix
        pair = None
        for prefix, p in _TICKER_TO_PAIR.items():
            if ticker.startswith(prefix + "-"):
                pair = p
                break
        if pair is None:
            return None

        # use kalshi's strike fields (source of truth, not ticker regex)
        strike_type = market.get("strike_type")
        floor_strike = market.get("floor_strike")
        cap_strike = market.get("cap_strike")
        if strike_type is None or (floor_strike is None and cap_strike is None):
            return None

        # time to expiry
        close_ts = market.get("close_ts")
        if close_ts is None:
            return None
        if isinstance(close_ts, str):
            close_ts = datetime.fromisoformat(close_ts.replace("Z", "+00:00"))
        if close_ts.tzinfo is None:
            close_ts = close_ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        seconds_to_expiry = (close_ts - now).total_seconds()
        if seconds_to_expiry <= 0:
            return None
        T_years = seconds_to_expiry / (365 * 86400)

        # spot + vol (cached)
        spot = _spot_cached(pair, int(_time.time() / 30))
        sigma = _vol_cached(pair, int(_time.time() / 3600))

        # compute predicted prob based on strike_type
        if strike_type == "greater":
            # yes if S_T >= floor_strike
            if floor_strike is None:
                return None
            predicted = log_normal_prob_above(spot, floor_strike, sigma, T_years)
            strike_desc = f">={floor_strike:.0f}"
        elif strike_type == "less":
            # yes if S_T <= cap_strike
            if cap_strike is None:
                return None
            predicted = 1.0 - log_normal_prob_above(spot, cap_strike, sigma, T_years)
            strike_desc = f"<={cap_strike:.0f}"
        elif strike_type == "between":
            # yes if floor_strike <= S_T <= cap_strike
            if floor_strike is None or cap_strike is None:
                return None
            p_above_floor = log_normal_prob_above(spot, floor_strike, sigma, T_years)
            p_above_cap = log_normal_prob_above(spot, cap_strike, sigma, T_years)
            predicted = max(0.005, min(0.995, p_above_floor - p_above_cap))
            strike_desc = f"in[{floor_strike:.0f},{cap_strike:.0f}]"
        else:
            # 'structured' or unknown — skip
            return None

        # market implied — may be None if no liquidity
        implied = None
        if snapshot:
            implied = implied_prob_from_snapshot(snapshot, side="yes")

        if implied is None:
            implied_for_dataclass = 0.0  # sentinel; downstream filters on edge=NaN
            edge = float("nan")
            implied_str = "n/a"
            edge_str = "n/a"
        else:
            implied_for_dataclass = implied
            edge = predicted - implied
            implied_str = f"{implied:.3f}"
            edge_str = f"{edge:+.3f}"

        rationale = (
            f"{pair} spot=${spot:.2f} σ={sigma*100:.1f}% T={T_years*365:.2f}d "
            f"{strike_desc} ({strike_type}) "
            f"predicted={predicted:.3f} implied={implied_str} edge={edge_str}"
        )
        return Prediction(
            ticker=ticker,
            predicted_prob=predicted,
            market_implied_prob=implied_for_dataclass,
            edge=edge,
            rationale=rationale,
        )


def main() -> int:
    """Smoke test against a few stored markets + their latest snapshot."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from sqlalchemy import desc, select

    from src.db import Market, Snapshot, session_scope

    print("crypto vol baseline smoke test")
    print("=" * 70)

    bl = CryptoVolBaseline()

    now = _dt.now(_tz.utc).replace(tzinfo=None)
    with session_scope() as s:
        # get a few BTC/ETH markets with future close + latest snapshot
        rows = s.execute(
            select(Market)
            .where(Market.series_ticker.in_(["KXBTC", "KXBTCD", "KXETH", "KXETHD"]))
            .where(Market.close_ts > now)
            .order_by(Market.close_ts)
            .limit(20)
        ).scalars().all()

        if not rows:
            print("no future markets in db — run poller first")
            return 1

        for m in rows[:10]:
            # latest snapshot for this market
            snap = s.execute(
                select(Snapshot)
                .where(Snapshot.ticker == m.ticker)
                .order_by(desc(Snapshot.ts))
                .limit(1)
            ).scalar_one_or_none()

            mdict = {
                "ticker": m.ticker,
                "close_ts": m.close_ts,
                "title": m.title,
                "strike_type": m.strike_type,
                "floor_strike": m.floor_strike,
                "cap_strike": m.cap_strike,
            }
            sdict = (
                {
                    "yes_bid": snap.yes_bid,
                    "yes_ask": snap.yes_ask,
                    "no_bid": snap.no_bid,
                    "no_ask": snap.no_ask,
                }
                if snap
                else None
            )
            pred = bl.predict(mdict, sdict)
            if pred is None:
                print(f"  {m.ticker:40s} (no prediction)")
            else:
                print(f"  {m.ticker:40s} {pred.rationale}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
