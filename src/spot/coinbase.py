"""coinbase spot price client.

uses the public exchange API — no auth, no key needed.
endpoints:
  GET /v2/prices/BTC-USD/spot       — current spot
  GET /products/BTC-USD/candles     — historical OHLCV candles

run:
    .venv\\Scripts\\python.exe -m src.spot.coinbase
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
import pandas as pd

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

# coinbase has two APIs:
#   api.coinbase.com — the consumer/retail API (simpler, less granular)
#   api.exchange.coinbase.com — the institutional API (better candles, free)
_COINBASE_RETAIL = "https://api.coinbase.com/v2"
_COINBASE_EXCHANGE = "https://api.exchange.coinbase.com"

Granularity = Literal[60, 300, 900, 3600, 21600, 86400]  # 1m, 5m, 15m, 1h, 6h, 1d


def get_spot_price(pair: str = "BTC-USD", timeout: float = 10.0) -> float:
    """Current spot mid for a pair, e.g. BTC-USD, ETH-USD."""
    url = f"{_COINBASE_RETAIL}/prices/{pair}/spot"
    with httpx.Client(timeout=timeout) as c:
        r = c.get(url)
        r.raise_for_status()
        return float(r.json()["data"]["amount"])


def get_candles(
    pair: str = "BTC-USD",
    granularity: Granularity = 3600,
    start: datetime | None = None,
    end: datetime | None = None,
    timeout: float = 10.0,
) -> pd.DataFrame:
    """Historical candles. Returns DataFrame with columns:
    [ts (utc), low, high, open, close, volume].

    Coinbase returns max 300 candles per call. Default fetches last 24h hourly.
    """
    if end is None:
        end = datetime.now(timezone.utc)
    if start is None:
        # 300 candles back at requested granularity
        start = end - timedelta(seconds=granularity * 299)

    url = f"{_COINBASE_EXCHANGE}/products/{pair}/candles"
    params = {
        "granularity": granularity,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    with httpx.Client(timeout=timeout) as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        raw = r.json()
    # coinbase returns [time, low, high, open, close, volume] sorted desc by time
    df = pd.DataFrame(raw, columns=["ts", "low", "high", "open", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    return df


def get_realized_vol(
    pair: str = "BTC-USD",
    lookback_days: int = 30,
    granularity: Granularity = 86400,
) -> float:
    """Annualized realized volatility from past `lookback_days` daily closes."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days + 1)
    df = get_candles(pair, granularity=granularity, start=start, end=end)
    if len(df) < 5:
        raise ValueError(f"not enough candles ({len(df)}) for vol calc")
    log_ret = (df["close"].pct_change().dropna()).apply(lambda x: pd.np.log(1 + x) if False else 0)
    # use ln returns properly
    import numpy as np

    log_ret = np.log(df["close"] / df["close"].shift(1)).dropna()
    daily_vol = log_ret.std()
    # annualize (crypto = 365 days, no weekend)
    annual_vol = daily_vol * (365 ** 0.5)
    return float(annual_vol)


def main() -> int:
    print("=" * 60)
    print("coinbase spot client smoke test")
    print("=" * 60)

    for pair in ("BTC-USD", "ETH-USD"):
        print(f"\n[{pair}]")
        spot = get_spot_price(pair)
        print(f"  spot: ${spot:,.2f}")

        candles = get_candles(pair, granularity=3600)
        print(f"  candles (hourly, 24h): {len(candles)} rows")
        if len(candles):
            row = candles.iloc[-1]
            print(f"  latest: ts={row['ts']} O={row['open']:.2f} H={row['high']:.2f} L={row['low']:.2f} C={row['close']:.2f} V={row['volume']:.2f}")

        vol = get_realized_vol(pair, lookback_days=30)
        print(f"  30d realized vol (annualized): {vol*100:.1f}%")

    print("\n[OK] coinbase client functional")
    return 0


if __name__ == "__main__":
    sys.exit(main())
