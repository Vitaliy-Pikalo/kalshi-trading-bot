"""Kalshi trading fees.

`paper_sim.py` records `cost_usd = contracts * price_cents / 100.0` and nothing
else, so every baseline this project has produced (v1 +14.3%, v2 +24.4%,
v3 -24.4%, v4 +0.1%) is a GROSS number. On a book that averages ~$0.73 per
fill and places ~1 contract per order, the fee is not a rounding detail --
it is larger than the entire measured edge.

Kalshi's published trading fee:

    taker fee per order = roundup( 0.07 * C * P * (1 - P) )
    maker fee per order = 25% of the taker fee

where C = number of contracts and P = price in dollars (0.01 - 0.99), and the
result is rounded UP to the next cent, per order.

The round-up is what actually bites here. A single contract at 73c owes
0.07 * 1 * 0.73 * 0.27 = $0.0138, which rounds to $0.02 -- a 45% markup on
the raw formula. Because this book places one contract per order, it pays
that round-up 355 times instead of amortising it across a larger order.

The fee is maximised at P = 0.50 (worst case $0.0175/contract raw) and shrinks
toward both extremes, so cheap tails and near-certs are relatively cheaper per
contract -- but as a FRACTION of the position, cheap tails are far worse:
at 19c the fee is 5.7% of the stake; at 73c it is 1.9%.
"""
from __future__ import annotations

import math

TAKER_COEFF = 0.07
MAKER_MULTIPLIER = 0.25


def kalshi_fee_usd(price_cents: int, contracts: int, maker: bool = False) -> float:
    """Fee in USD for one order of `contracts` at `price_cents`.

    Rounded up to the next cent, matching Kalshi's stated formula. Returns 0.0
    for degenerate prices (<=0 or >=100c) where no fee can apply.
    """
    if contracts <= 0:
        return 0.0
    p = price_cents / 100.0
    if p <= 0.0 or p >= 1.0:
        return 0.0
    raw = TAKER_COEFF * contracts * p * (1.0 - p)
    if maker:
        raw *= MAKER_MULTIPLIER
    # subtract an epsilon before ceil so a value that is exactly N cents in
    # real arithmetic but N + 1e-16 in float doesn't round up an extra cent
    return math.ceil(raw * 100.0 - 1e-9) / 100.0


def round_trip_fee_usd(price_cents: int, contracts: int, maker: bool = False) -> float:
    """Fee for entry only.

    Kalshi contracts that are HELD TO SETTLEMENT are not charged an exit fee --
    settlement is free. This book holds to settlement on every fill (there are
    no closing fills in the db, only settlements), so entry fee is the whole
    cost. Kept as a named function so the assumption is explicit and one place
    changes if the bot ever starts closing early.
    """
    return kalshi_fee_usd(price_cents, contracts, maker=maker)
