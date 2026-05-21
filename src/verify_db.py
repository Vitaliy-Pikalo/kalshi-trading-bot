"""Quick sanity check that WAL mode + pragmas are active.

run: .venv\\Scripts\\python.exe -m src.verify_db
"""
from __future__ import annotations

import sys

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from src.db import engine  # noqa: E402


def main() -> int:
    with engine.connect() as conn:
        for pragma in ("journal_mode", "synchronous", "busy_timeout", "foreign_keys"):
            row = conn.exec_driver_sql(f"PRAGMA {pragma}").fetchone()
            val = row[0] if row else None
            print(f"  {pragma:15s} = {val}")
    print()
    print("expected: journal_mode=wal, synchronous=1 (NORMAL),")
    print("          busy_timeout=5000, foreign_keys=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
