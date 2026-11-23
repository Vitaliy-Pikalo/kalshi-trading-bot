"""phase 2.0.2 - calibration baseline analysis.

reads data/calibration_dataset_v1.csv (from extract step 2.0.1) and computes:
- bucketed calibration curve (predicted vs actual)
- brier score + log loss (standard calibration metrics)
- per-strike-type slice (does the leak live in between or across all strike types?)
- per-series slice (BTC daily vs BTC range etc)
- diagnostic comparison to the diagnose_v1 buckets

design notes:
- pure stdlib + math, no pandas / sklearn (matches diagnose_v1 style)
- uses 6 buckets matching diagnose_v1 for direct comparison
- prints text tables, no plots (text is enough for the leak diagnosis)
- if [0.20, 0.40) bucket is the only one with a meaningful gap, isotonic calibration
  is the clean fix. if multiple buckets are off, model has deeper issues.

usage:
    .venv\\Scripts\\python.exe -m src.phase2.calibration_baseline
"""
from __future__ import annotations

import csv
import math
import sys
from collections import defaultdict
from pathlib import Path

from src.utils import setup_utf8_stdout

setup_utf8_stdout()


DATA_PATH = "data/calibration_dataset_v1.csv"


# buckets matching diagnose_v1.py for apples-to-apples comparison.
# each bucket: (lower, upper, expected_midpoint, label)
BUCKETS = [
    (0.00, 0.20, 0.10, "[0.00, 0.20)"),
    (0.20, 0.40, 0.30, "[0.20, 0.40)"),
    (0.40, 0.60, 0.50, "[0.40, 0.60)"),
    (0.60, 0.80, 0.70, "[0.60, 0.80)"),
    (0.80, 0.95, 0.88, "[0.80, 0.95)"),
    (0.95, 1.01, 0.98, "[0.95, 1.01)"),
]


def find_bucket(p: float) -> int:
    """return index of bucket that contains predicted prob p."""
    for i, (lo, hi, _, _) in enumerate(BUCKETS):
        if lo <= p < hi:
            return i
    return len(BUCKETS) - 1  # final bucket catches p>=0.95


def brier_score(rows: list[dict]) -> float:
    """mean((predicted - actual)^2). lower is better. perfect = 0."""
    if not rows:
        return float("nan")
    sq = sum((float(r["predicted_prob"]) - int(r["actual_outcome"])) ** 2 for r in rows)
    return sq / len(rows)


def log_loss(rows: list[dict], eps: float = 1e-15) -> float:
    """-mean(y*log(p) + (1-y)*log(1-p)). lower is better."""
    if not rows:
        return float("nan")
    total = 0.0
    for r in rows:
        p = float(r["predicted_prob"])
        y = int(r["actual_outcome"])
        p = max(eps, min(1 - eps, p))  # clip to avoid log(0)
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(rows)


def expected_calibration_error(rows: list[dict]) -> float:
    """ECE = sum_b (|B_b| / N) * |actual_b - mean_pred_b|.

    weighted by bucket size. 0 = perfect, higher = worse.
    """
    if not rows:
        return float("nan")
    buckets: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for r in rows:
        p = float(r["predicted_prob"])
        y = int(r["actual_outcome"])
        buckets[find_bucket(p)].append((p, y))

    n = len(rows)
    ece = 0.0
    for b_idx, items in buckets.items():
        size = len(items)
        mean_pred = sum(p for p, _ in items) / size
        mean_actual = sum(y for _, y in items) / size
        ece += (size / n) * abs(mean_actual - mean_pred)
    return ece


def calibration_table(rows: list[dict], title: str) -> None:
    """print per-bucket calibration breakdown."""
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)
    if not rows:
        print("  (no data)")
        return

    buckets: list[list[tuple[float, int]]] = [[] for _ in BUCKETS]
    for r in rows:
        p = float(r["predicted_prob"])
        y = int(r["actual_outcome"])
        buckets[find_bucket(p)].append((p, y))

    print(f"  n={len(rows)}, brier={brier_score(rows):.4f}, "
          f"log_loss={log_loss(rows):.4f}, ECE={expected_calibration_error(rows):.4f}")
    print()
    print(f"  {'bucket':<14} {'n':>6} {'mean_pred':>10} {'actual%':>9} "
          f"{'expected%':>10} {'gap_pp':>8}")
    print(f"  {'-'*14} {'-'*6} {'-'*10} {'-'*9} {'-'*10} {'-'*8}")
    for i, items in enumerate(buckets):
        lo, hi, mid, label = BUCKETS[i]
        if not items:
            print(f"  {label:<14} {'0':>6}  {'-':>10} {'-':>9} {mid*100:>9.0f}% {'-':>8}")
            continue
        n = len(items)
        mean_pred = sum(p for p, _ in items) / n
        actual = sum(y for _, y in items) / n
        gap = (actual - mean_pred) * 100  # percentage points
        flag = " <-- LEAK" if abs(gap) >= 10 else ""
        print(f"  {label:<14} {n:>6} {mean_pred*100:>9.1f}% {actual*100:>8.1f}% "
              f"{mid*100:>9.0f}% {gap:>+7.1f}{flag}")


def slice_by(rows: list[dict], col: str) -> dict[str, list[dict]]:
    """group rows by a column value."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[r[col] or "(null)"].append(r)
    return dict(groups)


def main() -> int:
    data_path = Path(DATA_PATH)
    if not data_path.exists():
        print(f"ERROR: {DATA_PATH} not found. run _phase2_data_extract.bat first.")
        return 1

    print(f"loading {DATA_PATH}...")
    rows: list[dict] = []
    with data_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            # filter rows with valid outcome + prob
            if r["actual_outcome"] in ("0", "1") and r["predicted_prob"]:
                rows.append(r)
    print(f"loaded {len(rows)} rows with valid outcome + predicted_prob")

    # ========== OVERALL CALIBRATION ==========
    calibration_table(rows, "OVERALL CALIBRATION (all rows)")

    # ========== BY STRIKE_TYPE ==========
    by_strike = slice_by(rows, "strike_type")
    print()
    print("=" * 80)
    print("CALIBRATION BY STRIKE_TYPE (where is the leak?)")
    print("=" * 80)
    for st in ["between", "greater", "less", "(null)"]:
        if st in by_strike and len(by_strike[st]) >= 5:
            calibration_table(by_strike[st], f"strike_type = {st}")

    # ========== BY SERIES_TICKER ==========
    by_series = slice_by(rows, "series_ticker")
    print()
    print("=" * 80)
    print("CALIBRATION BY SERIES (top 4 by row count)")
    print("=" * 80)
    top_series = sorted(by_series.items(), key=lambda x: -len(x[1]))[:4]
    for series, srows in top_series:
        if len(srows) >= 50:
            calibration_table(srows, f"series_ticker = {series}")

    # ========== STRUCTURAL READ ==========
    overall_ece = expected_calibration_error(rows)
    between_ece = expected_calibration_error(by_strike.get("between", []))
    greater_ece = expected_calibration_error(by_strike.get("greater", []))

    print()
    print("=" * 80)
    print("STRUCTURAL READ")
    print("=" * 80)
    print(f"  overall ECE:  {overall_ece:.4f}")
    print(f"  between ECE:  {between_ece:.4f}  (n={len(by_strike.get('between', []))})")
    print(f"  greater ECE:  {greater_ece:.4f}  (n={len(by_strike.get('greater', []))})")
    print()
    if between_ece > greater_ece * 1.5:
        print("  -> between-market calibration is WORSE than greater. range markets need")
        print("     their own calibration map. isotonic on between only might be the fix.")
    elif greater_ece > between_ece * 1.5:
        print("  -> greater calibration is WORSE than between. between isn't the problem.")
        print("     the leak we see in fills lives elsewhere or is variance.")
    else:
        print("  -> calibration error is similar across strike types. global isotonic")
        print("     calibration should work for all markets.")
    print()
    print("  next: 2.0.3 - fit isotonic regression on this dataset.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
