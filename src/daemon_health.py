"""Daemon health check — for use during long unattended runs.

Reports:
  * is daemon alive (last log entry within 2 min)?
  * counts of POLL_ERROR / DISCOVER_ERROR / SETTLE_ERROR in log
  * db totals: markets, snapshots, predictions, fills, settled
  * delta since last health check (cached in _daemon_health_baseline.json)

run: .venv\\Scripts\\python.exe -m src.daemon_health
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from src.utils import setup_utf8_stdout, utcnow_naive

setup_utf8_stdout()

from sqlalchemy import func, select  # noqa: E402

from src.db import (  # noqa: E402
    Fill, Market, Prediction, Snapshot, session_scope,
)

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = ROOT / "_daemon_health_baseline.json"
LOG_PATHS = [ROOT / "_daemon.log", ROOT / "_daemon_cons.log", ROOT / "_daemon_bal.log"]

# matches "[2026-05-17 18:35:09]" prefix
TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


def _parse_last_ts(path: Path) -> datetime | None:
    if not path.exists():
        return None
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return None
    last_ts = None
    for line in text.splitlines()[-200:]:  # only need recent lines
        m = TS_RE.search(line)
        if m:
            try:
                last_ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
    return last_ts


def _count_errors(path: Path) -> dict[str, int]:
    counts = {"POLL_ERROR": 0, "DISCOVER_ERROR": 0, "SETTLE_ERROR": 0, "TRACEBACK": 0}
    if not path.exists():
        return counts
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return counts
    for key in counts:
        counts[key] = text.count(key) if key != "TRACEBACK" else text.count("Traceback")
    return counts


def _load_baseline() -> dict:
    if BASELINE_PATH.exists():
        try:
            return json.loads(BASELINE_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_baseline(data: dict) -> None:
    try:
        BASELINE_PATH.write_text(json.dumps(data, indent=2, default=str))
    except Exception:
        pass


def main() -> int:
    now = utcnow_naive()
    print("=" * 70)
    print(f"DAEMON HEALTH CHECK  ({now.isoformat(sep=' ', timespec='seconds')} UTC)")
    print("=" * 70)

    # ---- log file freshness
    print("\n[log files]")
    for p in LOG_PATHS:
        if not p.exists():
            print(f"  {p.name:25} (missing)")
            continue
        last_ts = _parse_last_ts(p)
        if last_ts is None:
            print(f"  {p.name:25} no parseable timestamps")
            continue
        age = (now - last_ts).total_seconds()
        flag = "ALIVE" if age < 120 else ("STALE" if age < 600 else "DEAD?")
        print(f"  {p.name:25} last={last_ts.isoformat(sep=' ', timespec='seconds')}  "
              f"age={age:6.0f}s  [{flag}]")

    # ---- error counts in main log
    print("\n[errors in _daemon.log]")
    counts = _count_errors(LOG_PATHS[0])
    for k, v in counts.items():
        flag = "" if v == 0 else "  ⚠️" if v < 5 else "  🚨"
        print(f"  {k:18} = {v:4d}{flag}")

    # ---- db totals
    print("\n[db totals]")
    with session_scope() as s:
        totals = {
            "markets":         s.scalar(select(func.count()).select_from(Market)),
            "snapshots":       s.scalar(select(func.count()).select_from(Snapshot)),
            "predictions":     s.scalar(select(func.count()).select_from(Prediction)),
            "fills":           s.scalar(select(func.count()).select_from(Fill)),
            "fills_settled":   s.scalar(
                select(func.count()).select_from(Fill).where(Fill.realized_pnl.isnot(None))
            ),
        }
        pnl_row = s.execute(
            select(func.coalesce(func.sum(Fill.realized_pnl), 0.0))
            .where(Fill.realized_pnl.isnot(None))
        ).first()
        realized_pnl = pnl_row[0] if pnl_row else 0.0

    baseline = _load_baseline()
    for k, v in totals.items():
        prev = baseline.get(k)
        delta = f"  (+{v - prev})" if isinstance(prev, int) else ""
        print(f"  {k:18} = {v:6d}{delta}")
    prev_pnl = baseline.get("realized_pnl")
    pnl_delta = (f"  (Δ ${realized_pnl - prev_pnl:+.2f})"
                 if isinstance(prev_pnl, (int, float)) else "")
    print(f"  {'realized_pnl':18} = ${realized_pnl:+.2f}{pnl_delta}")

    # baseline write
    new_baseline = dict(totals)
    new_baseline["realized_pnl"] = realized_pnl
    new_baseline["checked_at"] = now.isoformat(sep=" ", timespec="seconds")
    _save_baseline(new_baseline)

    # ---- closing market check (are we about to lose visibility?)
    soon = now + timedelta(hours=1)
    with session_scope() as s:
        closing_soon = s.scalar(
            select(func.count()).select_from(Market)
            .where(Market.close_ts > now)
            .where(Market.close_ts < soon)
        )
    print(f"\n[markets closing within 1h]  {closing_soon}")

    print("\nbaseline updated. next call will show deltas vs this snapshot.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
