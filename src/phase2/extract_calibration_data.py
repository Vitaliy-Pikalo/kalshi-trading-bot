"""phase 2.0.1 — extract (predicted_prob, actual_outcome) pairs for calibration.

joins predictions to markets on ticker where settled_outcome is not null.
output: data/calibration_dataset_v1.csv

usage:
    .venv\\Scripts\\python.exe -m src.phase2.extract_calibration_data

safe to run while daemon is up (read-only sqlite access via WAL).

design notes:
- uses ALL predictions with a settled market, not just predictions that produced
  a fill. this gives ~13k+ data points vs ~150 fill-only points. for calibration
  fitting, more is better (each prob bucket gets more samples).
- the prediction is "P(YES wins)". the actual outcome is binary (1=yes, 0=no).
- output is csv (not parquet) to avoid the pyarrow dependency. small dataset
  anyway (<100MB at current scale).
- preserves prediction_id so we can later join back to fills for fill-only views.
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path
from typing import Iterator

from src.utils import setup_utf8_stdout

setup_utf8_stdout()


DB_PATH = "data/poker_quant.db"
OUT_PATH = "data/calibration_dataset_v1.csv"


SQL = """
SELECT
    p.id              AS prediction_id,
    p.ticker          AS ticker,
    p.ts              AS prediction_ts,
    p.model_version   AS model_version,
    p.predicted_prob  AS predicted_prob,
    p.market_implied_prob AS market_implied_prob,
    p.edge            AS edge,
    m.close_ts        AS close_ts,
    m.settled_outcome AS settled_outcome,
    CASE WHEN m.settled_outcome = 'yes' THEN 1
         WHEN m.settled_outcome = 'no'  THEN 0
         ELSE NULL END AS actual_outcome,
    m.strike_type     AS strike_type,
    m.series_ticker   AS series_ticker,
    m.floor_strike    AS floor_strike,
    m.cap_strike      AS cap_strike
FROM predictions p
JOIN markets m ON p.ticker = m.ticker
WHERE m.settled_outcome IS NOT NULL
ORDER BY p.ts ASC
"""


COLUMNS = [
    "prediction_id",
    "ticker",
    "prediction_ts",
    "model_version",
    "predicted_prob",
    "market_implied_prob",
    "edge",
    "close_ts",
    "settled_outcome",
    "actual_outcome",
    "strike_type",
    "series_ticker",
    "floor_strike",
    "cap_strike",
]


def stream_rows(cur: sqlite3.Cursor) -> Iterator[tuple]:
    """yield rows from cursor one at a time (memory-safe for large extracts)."""
    cur.execute(SQL)
    while True:
        row = cur.fetchone()
        if row is None:
            break
        yield row


def main() -> int:
    db_path = Path(DB_PATH)
    if not db_path.exists():
        print(f"ERROR: db not found at {DB_PATH}", file=sys.stderr)
        return 1

    out_path = Path(OUT_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # read-only WAL-friendly connection
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    cur = conn.cursor()

    n_rows = 0
    n_yes = 0
    n_no = 0
    by_strike: dict[str, int] = {}
    by_series: dict[str, int] = {}
    by_model: dict[str, int] = {}

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)

        for row in stream_rows(cur):
            w.writerow(row)
            n_rows += 1
            # row indexes: 9=actual_outcome, 10=strike_type, 11=series_ticker, 3=model_version
            if row[9] == 1:
                n_yes += 1
            elif row[9] == 0:
                n_no += 1
            st = row[10] or "unknown"
            sr = row[11] or "unknown"
            mv = row[3] or "unknown"
            by_strike[st] = by_strike.get(st, 0) + 1
            by_series[sr] = by_series.get(sr, 0) + 1
            by_model[mv] = by_model.get(mv, 0) + 1

    conn.close()

    print("=" * 60)
    print("PHASE 2.0.1 — CALIBRATION DATA EXTRACT")
    print("=" * 60)
    print(f"output:           {OUT_PATH}")
    print(f"total rows:       {n_rows}")
    print(f"actual_outcome=1: {n_yes:>6}  ({n_yes/n_rows:5.1%})" if n_rows else "n_yes:  0")
    print(f"actual_outcome=0: {n_no:>6}  ({n_no/n_rows:5.1%})" if n_rows else "n_no:   0")
    print()
    print("by strike_type:")
    for k, v in sorted(by_strike.items(), key=lambda x: -x[1]):
        print(f"  {k:12s} n={v}")
    print()
    print("by series_ticker (top 10):")
    for k, v in sorted(by_series.items(), key=lambda x: -x[1])[:10]:
        print(f"  {k:12s} n={v}")
    print()
    print("by model_version:")
    for k, v in sorted(by_model.items(), key=lambda x: -x[1]):
        print(f"  {k:30s} n={v}")
    print()
    print("=" * 60)
    print("SAMPLE ROWS (first 5)")
    print("=" * 60)
    with out_path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i > 5:
                break
            print(line.rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
