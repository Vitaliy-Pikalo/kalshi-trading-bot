"""probe what categories + series are available on the current env.

run: .venv\\Scripts\\python.exe -m src.kalshi.probe_categories
"""
from __future__ import annotations

import sys
from collections import Counter

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from src.kalshi.client import KalshiClient  # noqa: E402


def main() -> int:
    with KalshiClient() as k:
        print("[1] listing all series + grouping by category...")
        resp = k.list_series()
        series_list = resp.get("series", [])
        print(f"    total series: {len(series_list)}")

        by_cat: Counter[str] = Counter()
        crypto_series: list[dict] = []
        tennis_series: list[dict] = []
        sports_series: list[dict] = []
        for s in series_list:
            cat = s.get("category", "?")
            by_cat[cat] += 1
            ticker = (s.get("ticker") or "").upper()
            title = (s.get("title") or "").lower()
            if "BTC" in ticker or "ETH" in ticker or "crypto" in title or "bitcoin" in title:
                crypto_series.append(s)
            if (
                "TENNIS" in ticker or "ATP" in ticker or "WTA" in ticker
                or "tennis" in title or "wimbledon" in title
                or "open" in title and ("french" in title or "us " in title or "australian" in title)
            ):
                tennis_series.append(s)
            if cat == "Sports":
                sports_series.append(s)

        print(f"\n    by category:")
        for cat, n in by_cat.most_common(15):
            print(f"      {cat:25s} {n}")

        print(f"\n    crypto-matching series ({len(crypto_series)}):")
        for s in crypto_series[:15]:
            t = s.get("ticker", "?")
            title = (s.get("title") or "?")[:55]
            cat = s.get("category", "?")
            print(f"      {t:25s} [{cat:12s}]  {title}")

        print(f"\n    tennis-matching series ({len(tennis_series)}):")
        for s in tennis_series[:15]:
            t = s.get("ticker", "?")
            title = (s.get("title") or "?")[:55]
            cat = s.get("category", "?")
            print(f"      {t:25s} [{cat:12s}]  {title}")

        print(f"\n    all Sports series (first 30 of {len(sports_series)}):")
        for s in sports_series[:30]:
            t = s.get("ticker", "?")
            title = (s.get("title") or "?")[:55]
            print(f"      {t:25s}  {title}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
