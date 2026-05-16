"""smoke test for expanded kalshi client.

run:
    .venv\\Scripts\\python.exe -m src.kalshi.test_endpoints

verifies each new endpoint against the LIVE api:
    list_series, list_events, get_event, get_market, get_orderbook, iter_markets
"""
from __future__ import annotations

import sys

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

from src.kalshi.client import KalshiClient  # noqa: E402


def main() -> int:
    print("=" * 70)
    print("kalshi client — expanded endpoints smoke test")
    print("=" * 70)

    with KalshiClient() as k:
        # 1. series listing
        print("\n[1] list_series() — top-level market categories")
        resp = k.list_series()
        series_list = resp.get("series", [])
        print(f"    got {len(series_list)} series")
        for s in series_list[:8]:
            t = s.get("ticker", "?")
            title = (s.get("title") or "?")[:50]
            cat = s.get("category", "?")
            print(f"    {t:25s}  [{cat:12s}]  {title}")

        # 2. events
        print("\n[2] list_events(status='open', limit=5)")
        resp = k.list_events(status="open", limit=5)
        events = resp.get("events", [])
        print(f"    got {len(events)} events")
        if events:
            sample_event_ticker = events[0].get("event_ticker")
            for e in events[:5]:
                et = e.get("event_ticker", "?")
                title = (e.get("title") or "?")[:50]
                print(f"    {et:35s}  {title}")

            # 3. get_event with nested markets
            print(f"\n[3] get_event({sample_event_ticker!r}, with_nested_markets=True)")
            try:
                ev = k.get_event(sample_event_ticker, with_nested_markets=True)
                event_obj = ev.get("event", {})
                child_markets = event_obj.get("markets", [])
                print(f"    event title: {event_obj.get('title', '?')[:60]}")
                print(f"    child markets: {len(child_markets)}")
                for m in child_markets[:5]:
                    t = m.get("ticker", "?")
                    yb = m.get("yes_bid", 0)
                    ya = m.get("yes_ask", 0)
                    print(f"      {t:35s}  {yb}/{ya}")
            except Exception as e:
                print(f"    FAIL: {e}")
                return 1

            # 4. get_market on first child
            if child_markets:
                sample_market = child_markets[0].get("ticker")
                print(f"\n[4] get_market({sample_market!r})")
                m = k.get_market(sample_market).get("market", {})
                print(f"    title: {(m.get('title') or '?')[:60]}")
                print(f"    yes_bid/ask: {m.get('yes_bid')}/{m.get('yes_ask')}")
                print(f"    volume: {m.get('volume', 0)}")
                print(f"    open_interest: {m.get('open_interest', 0)}")

                # 5. orderbook
                print(f"\n[5] get_orderbook({sample_market!r})")
                ob = k.get_orderbook(sample_market).get("orderbook", {})
                yes_levels = ob.get("yes", [])
                no_levels = ob.get("no", [])
                print(f"    yes side: {len(yes_levels)} price levels")
                print(f"    no side:  {len(no_levels)} price levels")
                if yes_levels:
                    print(f"    top yes:  {yes_levels[:3]}")
                if no_levels:
                    print(f"    top no:   {no_levels[:3]}")

        # 6. iter_markets pagination
        print("\n[6] iter_markets() — pagination test (cap at 250)")
        count = 0
        for _ in k.iter_markets(status="open", page_size=100, max_pages=3):
            count += 1
            if count >= 250:
                break
        print(f"    iterated {count} markets across pages")

    print("\n" + "=" * 70)
    print("[OK] all endpoints verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
