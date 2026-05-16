"""market discovery + category classifier.

walks kalshi series → events → markets, scoping to only crypto + tennis.
much faster than iterating all 10k+ markets blindly.

modes:
    --explore   print breakdown only
    --store     persist to db (idempotent)
    --category  crypto | tennis (default: both)

run:
    .venv\\Scripts\\python.exe -m src.ingest.discover --explore
    .venv\\Scripts\\python.exe -m src.ingest.discover --store
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime
from typing import Any, Iterable

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from sqlalchemy.dialects.sqlite import insert as sqlite_insert  # noqa: E402

from src.db import Market, session_scope  # noqa: E402
from src.kalshi.client import KalshiClient  # noqa: E402

# series-level filters. these are the source of truth — we query kalshi for
# series matching these criteria, then iterate markets within them.
_TENNIS_SERIES_RE = re.compile(
    r"\b(ATP|WTA|TENNIS)\b", re.IGNORECASE
)


_BTC_ETH_RE = re.compile(r"\b(BTC|ETH|BITCOIN|ETHEREUM)\b", re.IGNORECASE)


def find_target_series(client: KalshiClient) -> dict[str, list[dict]]:
    """Returns {'crypto': [series], 'tennis': [series]}.

    crypto: only BTC + ETH series (skip altcoin noise like KXFDVPLASMA)
    tennis: ATP/WTA/TENNIS-tagged Sports series, excluding table tennis
    """
    resp = client.list_series()
    series_list = resp.get("series", [])

    crypto: list[dict] = []
    tennis: list[dict] = []
    for s in series_list:
        cat = (s.get("category") or "").strip()
        ticker = (s.get("ticker") or "")
        title = (s.get("title") or "")
        haystack = f"{ticker} {title}"

        # crypto: Crypto category AND BTC/ETH only (skip altcoins for now)
        if cat == "Crypto" and _BTC_ETH_RE.search(haystack):
            crypto.append(s)
            continue

        # tennis: Sports category + tennis-specific ticker/title
        if cat == "Sports":
            # exclude table tennis (different sport, different markets)
            if "TABLETENNIS" in ticker.upper() or "table tennis" in title.lower():
                continue
            if "TT" in ticker.upper() and "ELITE" in ticker.upper():
                continue
            if _TENNIS_SERIES_RE.search(haystack):
                tennis.append(s)

    return {"crypto": crypto, "tennis": tennis}


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def iter_markets_in_series(
    client: KalshiClient,
    series_ticker: str,
    status: str | None = "open",
) -> Iterable[dict]:
    """All markets within a series, paginating."""
    yield from client.iter_markets(
        status=status, page_size=1000, max_pages=20, series_ticker=series_ticker
    )


def explore(client: KalshiClient) -> None:
    targets = find_target_series(client)

    for cat, series_list in targets.items():
        print(f"\n=== {cat.upper()} ===")
        print(f"target series: {len(series_list)}")
        for s in series_list:
            print(f"  {s.get('ticker', '?'):30s} {(s.get('title') or '?')[:55]}")

        # only deep-probe markets for first 5 series per category to stay under rate limit
        market_counts: list[tuple[str, str, int]] = []
        total_markets = 0
        for s in series_list[:5]:
            ticker = s.get("ticker", "?")
            title = (s.get("title") or "?")[:50]
            count = sum(1 for _ in iter_markets_in_series(client, ticker))
            market_counts.append((ticker, title, count))
            total_markets += count

        market_counts.sort(key=lambda x: x[2], reverse=True)
        print(f"\nopen market counts (top 5 series sampled):")
        for ticker, title, count in market_counts:
            print(f"  {ticker:30s} {count:5d} markets  {title}")
        print(f"total in sample: {total_markets}")

        # show 5 actual sample markets
        print(f"\nsample {cat} markets:")
        shown = 0
        for ticker, _, count in market_counts:
            if count == 0:
                continue
            for m in iter_markets_in_series(client, ticker):
                t = m.get("ticker", "?")
                title_m = (m.get("title") or "?")[:55]
                yb = m.get("yes_bid") or 0
                ya = m.get("yes_ask") or 0
                vol = m.get("volume", 0)
                close = m.get("close_time", "?")[:19]
                print(f"  {t:35s} {yb:3}/{ya:3} vol={vol:5}  closes={close}  {title_m}")
                shown += 1
                if shown >= 5:
                    break
            if shown >= 5:
                break


def store(
    client: KalshiClient,
    only_category: str | None = None,
) -> None:
    targets = find_target_series(client)
    by_cat: Counter[str] = Counter()

    with session_scope() as s:
        for cat, series_list in targets.items():
            if only_category and cat != only_category:
                continue
            for series in series_list:
                series_ticker = series.get("ticker")
                if not series_ticker:
                    continue
                for m in iter_markets_in_series(client, series_ticker):
                    by_cat[cat] += 1
                    stmt = sqlite_insert(Market).values(
                        ticker=m.get("ticker"),
                        title=(m.get("title") or "")[:500],
                        series_ticker=series_ticker,
                        event_ticker=m.get("event_ticker"),
                        open_ts=parse_iso(m.get("open_time")),
                        close_ts=parse_iso(m.get("close_time")),
                        settled_outcome=None,
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["ticker"],
                        set_=dict(
                            title=stmt.excluded.title,
                            series_ticker=stmt.excluded.series_ticker,
                            event_ticker=stmt.excluded.event_ticker,
                            open_ts=stmt.excluded.open_ts,
                            close_ts=stmt.excluded.close_ts,
                        ),
                    )
                    s.execute(stmt)

    print(f"stored: {dict(by_cat)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--explore", action="store_true", help="print breakdown only")
    parser.add_argument("--store", action="store_true", help="persist to db")
    parser.add_argument(
        "--category", choices=["crypto", "tennis"], help="only this category"
    )
    args = parser.parse_args()

    if not (args.explore or args.store):
        args.explore = True

    with KalshiClient() as k:
        if args.explore:
            explore(k)
        if args.store:
            store(k, only_category=args.category)

    return 0


if __name__ == "__main__":
    sys.exit(main())
