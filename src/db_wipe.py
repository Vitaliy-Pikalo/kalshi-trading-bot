"""wipe data tables (markets, snapshots, predictions, fills, bankroll).

leaves schema intact. use when switching envs (demo <-> prod).

run: .venv\\Scripts\\python.exe -m src.db_wipe
"""
from __future__ import annotations

import sys

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from sqlalchemy import delete  # noqa: E402

from src.db import (  # noqa: E402
    BankrollSnapshot,
    Fill,
    Market,
    Prediction,
    Snapshot,
    session_scope,
)


def main() -> int:
    with session_scope() as s:
        for tbl in (Fill, Prediction, Snapshot, BankrollSnapshot, Market):
            n = s.execute(delete(tbl)).rowcount
            print(f"  cleared {tbl.__tablename__:15s} ({n} rows)")
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
