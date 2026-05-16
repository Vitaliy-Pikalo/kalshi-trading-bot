"""abstract baseline interface.

a baseline is anything that, given a market, returns a predicted probability
that the YES side wins. paper sim consumes any baseline.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Prediction:
    ticker: str
    predicted_prob: float       # 0.0 - 1.0
    market_implied_prob: float  # 0.0 - 1.0, from yes_ask or mid
    edge: float                 # predicted - implied
    rationale: str = ""         # human-readable for journaling


class Baseline(ABC):
    """Predicts P(yes wins) for a given market."""

    name: str = "unnamed"

    @abstractmethod
    def predict(self, market: dict, snapshot: dict | None = None) -> Prediction | None:
        """Returns a Prediction or None if this baseline can't handle the market."""
        ...


def implied_prob_from_snapshot(snapshot: dict, side: str = "yes") -> float | None:
    """Market-implied prob from bid/ask. Use ask if buying, bid if selling.

    Returns None if no liquidity.
    """
    if side == "yes":
        ask = snapshot.get("yes_ask")
        bid = snapshot.get("yes_bid")
    else:
        ask = snapshot.get("no_ask")
        bid = snapshot.get("no_bid")
    if ask is None or ask <= 0:
        return None
    # use mid of bid+ask if both exist, else use ask
    if bid and bid > 0:
        return (ask + bid) / 200.0  # kalshi prices in cents
    return ask / 100.0
