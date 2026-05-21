"""shared utilities — keep idempotent."""
from __future__ import annotations

import io
import sys
from datetime import datetime, timezone

_stdout_wrapped = False


def utcnow_naive() -> datetime:
    """Returns current UTC time as a naive datetime.

    The codebase stores all timestamps as naive UTC (close_ts, snapshot.ts,
    fill.ts, etc) for sqlite simplicity. This replaces the deprecated
    `datetime.utcnow()` while preserving the same return value.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def setup_utf8_stdout() -> None:
    """Force utf-8 stdout/stderr on windows. Safe to call multiple times."""
    global _stdout_wrapped
    if _stdout_wrapped:
        return
    if sys.platform == "win32":
        try:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace"
            )
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer, encoding="utf-8", errors="replace"
            )
        except Exception:
            pass  # if buffers are unusual (e.g. test runner), just skip
    _stdout_wrapped = True
