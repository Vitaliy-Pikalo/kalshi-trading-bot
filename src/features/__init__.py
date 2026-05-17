"""feature extraction — turn (market, snapshot, spot, baseline) into a feature row.

modules:
    crypto.py    — crypto-market features (moneyness, vol regime, microstructure)
    tennis.py    — tennis-match features (Elo diff, surface, recency)
    dataset.py   — assemble training matrix from db
"""
