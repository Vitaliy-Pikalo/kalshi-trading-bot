"""shared utilities — keep idempotent."""
from __future__ import annotations

import io
import sys

_stdout_wrapped = False


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
