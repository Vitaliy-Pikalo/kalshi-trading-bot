"""smoke test — phase 0 deliverable.

verifies:
1. .env is configured
2. private key loads
3. api request signs + authenticates
4. we can pull live market data

run:
    .venv\\Scripts\\python.exe -m src.kalshi.smoke_test
"""
from __future__ import annotations

import io
import sys

# force utf-8 stdout on windows so cmd doesn't choke on non-ascii
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from src.config import settings  # noqa: E402
from src.kalshi.client import KalshiClient, _load_private_key  # noqa: E402


def main() -> int:
    print("=" * 60)
    print("kalshi smoke test")
    print("=" * 60)

    # 1. config check
    print(f"env:           {settings.kalshi_env}")
    print(f"api base:      {settings.api_base_url}")
    print(f"key id set:    {'yes' if settings.kalshi_key_id else 'NO'}")
    print(f"key path:      {settings.kalshi_private_key_path or '(not set)'}")
    print()

    if not settings.kalshi_key_id:
        print("FAIL: KALSHI_KEY_ID not set in .env")
        return 1
    if not settings.kalshi_private_key_path:
        print("FAIL: KALSHI_PRIVATE_KEY_PATH not set in .env")
        return 1

    # 2. private key loads
    print("loading private key...", end=" ")
    try:
        _load_private_key()
        print("ok")
    except Exception as e:
        print(f"FAIL: {e}")
        return 1

    # 3. + 4. authenticated request
    print("calling GET /markets?status=open&limit=5 ...", end=" ")
    try:
        with KalshiClient() as k:
            data = k.list_markets(status="open", limit=5)
        print("ok")
    except Exception as e:
        print(f"FAIL: {e}")
        return 1

    markets = data.get("markets", [])
    print()
    print(f"got {len(markets)} markets:")
    for m in markets:
        ticker = m.get("ticker", "?")
        title = (m.get("title") or "")[:60]
        yes_bid = m.get("yes_bid", 0)
        yes_ask = m.get("yes_ask", 0)
        print(f"  {ticker:30s}  {yes_bid:3}/{yes_ask:3}  {title}")

    print()
    print("[OK] phase 0 verification passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
