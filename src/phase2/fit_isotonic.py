"""phase 2.0.3 - fit isotonic regression calibration models.

reads data/calibration_dataset_v1.csv and fits sklearn.isotonic.IsotonicRegression
per strike_type (between + greater separately — they have different calibration profiles
per phase 2.0.2 analysis: between ECE=0.0351, greater ECE=0.0595).

outputs:
    data/calibration/iso_v1_between.pkl
    data/calibration/iso_v1_greater.pkl

also prints before/after calibration tables + brier/log_loss drop to confirm the fit
is doing the right thing. expect:
    - [0.40, 0.60) bucket: raw ~50% → calibrated ~71% (massive underconfidence fix)
    - [0.60, 0.80) bucket: raw ~70% → calibrated ~98% (near-certs getting right label)
    - log_loss should drop significantly on both strike types

usage:
    .venv\\Scripts\\python.exe -m src.phase2.fit_isotonic
"""
from __future__ import annotations

import csv
import math
import pickle
import sys
from collections import defaultdict
from pathlib import Path

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

try:
    import numpy as np
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import brier_score_loss, log_loss as sklearn_log_loss
except ImportError:
    print("ERROR: scikit-learn not installed.")
    print("fix: .venv\\Scripts\\python.exe -m pip install scikit-learn")
    sys.exit(1)


DATA_PATH = Path("data/calibration_dataset_v1.csv")
CALIBRATION_DIR = Path("data/calibration")

BUCKETS = [
    (0.00, 0.20, "[0.00, 0.20)"),
    (0.20, 0.40, "[0.20, 0.40)"),
    (0.40, 0.60, "[0.40, 0.60)"),
    (0.60, 0.80, "[0.60, 0.80)"),
    (0.80, 0.95, "[0.80, 0.95)"),
    (0.95, 1.01, "[0.95, 1.01)"),
]


def find_bucket(p: float) -> int:
    for i, (lo, hi, _) in enumerate(BUCKETS):
        if lo <= p < hi:
            return i
    return len(BUCKETS) - 1


def load_data(strike_type: str | None = None) -> tuple[np.ndarray, np.ndarray]:
    """load (predicted_prob, actual_outcome) arrays, optionally filtered by strike_type."""
    probs, outcomes = [], []
    with DATA_PATH.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["actual_outcome"] not in ("0", "1") or not r["predicted_prob"]:
                continue
            if strike_type is not None and r["strike_type"] != strike_type:
                continue
            probs.append(float(r["predicted_prob"]))
            outcomes.append(int(r["actual_outcome"]))
    return np.array(probs), np.array(outcomes)


def calibration_table(probs: np.ndarray, outcomes: np.ndarray, label: str) -> None:
    """print per-bucket breakdown matching calibration_baseline.py style."""
    n = len(probs)
    probs_clipped = np.clip(probs, 1e-15, 1 - 1e-15)
    bs = brier_score_loss(outcomes, probs_clipped)
    ll = sklearn_log_loss(outcomes, probs_clipped)

    buckets: list[list[tuple[float, int]]] = [[] for _ in BUCKETS]
    for p, y in zip(probs, outcomes):
        buckets[find_bucket(p)].append((p, y))

    print(f"\n{label}  (n={n}, brier={bs:.4f}, log_loss={ll:.4f})")
    print(f"  {'bucket':<14} {'n':>6} {'mean_pred':>10} {'actual%':>9} {'gap_pp':>8}")
    print(f"  {'-'*14} {'-'*6} {'-'*10} {'-'*9} {'-'*8}")
    for i, items in enumerate(buckets):
        _, _, lbl = BUCKETS[i]
        if not items:
            print(f"  {lbl:<14} {'0':>6}  {'-':>10} {'-':>9} {'-':>8}")
            continue
        bn = len(items)
        mean_pred = sum(p for p, _ in items) / bn
        actual = sum(y for _, y in items) / bn
        gap = (actual - mean_pred) * 100
        flag = " <-- LEAK" if abs(gap) >= 10 else ""
        print(f"  {lbl:<14} {bn:>6} {mean_pred*100:>9.1f}% {actual*100:>8.1f}% {gap:>+7.1f}{flag}")


def fit_and_save(strike_type: str) -> IsotonicRegression | None:
    """fit isotonic model for one strike_type. returns model or None if too few rows."""
    probs, outcomes = load_data(strike_type)
    if len(probs) < 50:
        print(f"\nskipping strike_type='{strike_type}': only {len(probs)} rows (need ≥50)")
        return None

    print(f"\n{'='*80}")
    print(f"FITTING: strike_type = {strike_type}  (n={len(probs)})")
    print(f"{'='*80}")

    # --- before calibration ---
    calibration_table(probs, outcomes, "BEFORE calibration (raw model)")

    # --- fit isotonic ---
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(probs, outcomes)

    # --- after calibration ---
    calibrated = iso.predict(probs)
    calibration_table(calibrated, outcomes, "AFTER  calibration (isotonic)")

    # --- delta summary ---
    bs_before = brier_score_loss(outcomes, np.clip(probs, 1e-15, 1 - 1e-15))
    bs_after  = brier_score_loss(outcomes, np.clip(calibrated, 1e-15, 1 - 1e-15))
    ll_before = sklearn_log_loss(outcomes, np.clip(probs, 1e-15, 1 - 1e-15))
    ll_after  = sklearn_log_loss(outcomes, np.clip(calibrated, 1e-15, 1 - 1e-15))

    print(f"\n  DELTA:")
    print(f"    brier:    {bs_before:.4f} → {bs_after:.4f}  ({bs_after - bs_before:+.4f})")
    print(f"    log_loss: {ll_before:.4f} → {ll_after:.4f}  ({ll_after - ll_before:+.4f})")
    if ll_after < ll_before:
        print("    ✅ log_loss improved — calibration is helping")
    else:
        print("    ⚠️  log_loss did NOT improve — check curve shape in 2.0.4")

    # --- save model ---
    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CALIBRATION_DIR / f"iso_v1_{strike_type}.pkl"
    with out_path.open("wb") as f:
        pickle.dump(iso, f)
    print(f"\n  saved → {out_path}")

    return iso


def verify_curve_shape(iso: IsotonicRegression, strike_type: str) -> None:
    """quick sanity check: does the learned mapping make sense?"""
    test_points = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 0.99]
    calibrated = iso.predict(np.array(test_points))

    print(f"\n  CURVE SHAPE CHECK ({strike_type}):")
    print(f"  {'raw':>6} → {'calibrated':>11}")
    print(f"  {'------':>6}   {'----------':>11}")
    prev_c = -1.0
    monotone = True
    for raw, cal in zip(test_points, calibrated):
        flag = ""
        if cal < prev_c - 1e-6:
            flag = " ⚠️  NON-MONOTONE"
            monotone = False
        print(f"  {raw*100:>5.0f}% → {cal*100:>10.1f}%{flag}")
        prev_c = cal

    if monotone:
        print("  ✅ curve is monotonically non-decreasing (correct for isotonic)")
    else:
        print("  ❌ non-monotone detected — something is wrong")


def main() -> int:
    if not DATA_PATH.exists():
        print(f"ERROR: {DATA_PATH} not found. run _phase2_data_extract.bat first.")
        return 1

    print(f"phase 2.0.3 — isotonic calibration fit")
    print(f"data: {DATA_PATH}")

    models: dict[str, IsotonicRegression] = {}

    for st in ["between", "greater"]:
        iso = fit_and_save(st)
        if iso is not None:
            models[st] = iso
            verify_curve_shape(iso, st)

    if not models:
        print("\nERROR: no models fit. check data quality.")
        return 1

    print(f"\n{'='*80}")
    print("PHASE 2.0.3 COMPLETE")
    print(f"{'='*80}")
    print(f"  models saved to: {CALIBRATION_DIR}/")
    for st in models:
        print(f"    iso_v1_{st}.pkl")
    print()
    print("  next steps:")
    print("  2.0.4 — verify curve shape makes sense (done inline above)")
    print("  2.0.5 — wire into paper_sim.py with --use-calibration flag")
    print("  2.0.6 — restart daemon, set v4 cutoff, accumulate n≥30 fills")
    print("  2.0.7 — diagnose v4 vs v3")
    return 0


if __name__ == "__main__":
    sys.exit(main())
