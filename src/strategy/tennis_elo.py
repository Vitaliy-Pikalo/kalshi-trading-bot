"""tennis Elo baseline.

predicts P(yes side wins) for kalshi tennis match markets by:
  1. parsing the title to find which player is the YES side + opponent
  2. looking up surface-adjusted Elo ratings (atp or wta data)
  3. computing standard Elo prob: 1 / (1 + 10^((opp_elo - my_elo)/400))

title patterns we handle (from kalshi):
  "Will Dayana Yastremska win the Yastremska vs Bouzkova: Set Winner?"
  "Will Choinski win the Choinski vs Fery: Set Winner?"
  "Will Player A win the Match?"  (if just one name we infer the other)

if we can't extract both names, returns None (paper sim skips it).
"""
from __future__ import annotations

import re
import sys
from functools import lru_cache

from src.spot.tennis_data import load_elo, predict_match_prob
from src.strategy.base import Baseline, Prediction, implied_prob_from_snapshot
from src.utils import setup_utf8_stdout

setup_utf8_stdout()

# tournament prefix -> surface. expand as needed.
# default: hard court (most common)
_EVENT_SURFACE = {
    # clay
    "KXATPIT": "clay",      # italian open
    "KXATPMC": "clay",      # monte carlo
    "KXATPMAD": "clay",     # madrid
    "KXATPRO": "clay",      # roland garros (french open)
    "KXWTAIT": "clay",
    "KXWTAMAD": "clay",
    "KXWTARO": "clay",
    # grass
    "KXATPWIM": "grass",
    "KXATPHALLE": "grass",
    "KXWTAWIM": "grass",
    # hard (explicit, plus default)
    "KXATPMIA": "hard",
    "KXATPUS": "hard",
    "KXATPAUS": "hard",
    "KXATPWDDF": "hard",
    "KXATPCIN": "hard",
    "KXWTAMIA": "hard",
    "KXWTAUS": "hard",
}

# title regex: "Will <Player> win the <Player1> vs <Player2>...?"
_TITLE_RE = re.compile(
    r"Will\s+(?P<winner>[A-Za-z\.\-' ]+?)\s+win\s+the\s+(?P<p1>[A-Za-z\.\-' ]+?)\s+vs\.?\s+(?P<p2>[A-Za-z\.\-' ]+?)[:?]",
    re.IGNORECASE,
)


@lru_cache(maxsize=2)
def _elo_for_tour(tour: str):
    try:
        return load_elo(tour)
    except FileNotFoundError:
        return None


def surface_for_event(event_ticker: str | None) -> str:
    if not event_ticker:
        return "hard"
    for prefix, surface in _EVENT_SURFACE.items():
        if event_ticker.upper().startswith(prefix):
            return surface
    return "hard"


def parse_title(title: str) -> tuple[str, str, str] | None:
    """Returns (yes_player, player1_last, player2_last) or None."""
    if not title:
        return None
    m = _TITLE_RE.search(title)
    if not m:
        return None
    return m.group("winner").strip(), m.group("p1").strip(), m.group("p2").strip()


class TennisEloBaseline(Baseline):
    name = "tennis_elo_v0"

    def predict(self, market: dict, snapshot: dict | None = None) -> Prediction | None:
        ticker = market.get("ticker", "")
        # tour detection
        if "KXATP" in ticker.upper():
            tour = "atp"
        elif "KXWTA" in ticker.upper():
            tour = "wta"
        else:
            return None

        elo_df = _elo_for_tour(tour)
        if elo_df is None:
            return None  # cache not built yet

        title = market.get("title") or ""
        parsed = parse_title(title)
        if not parsed:
            return None
        yes_player, p1_last, p2_last = parsed

        # opponent = whichever of p1/p2 is NOT the yes_player
        yes_lower = yes_player.lower()
        if p1_last.lower() in yes_lower or yes_lower in p1_last.lower():
            opponent = p2_last
        elif p2_last.lower() in yes_lower or yes_lower in p2_last.lower():
            opponent = p1_last
        else:
            # ambiguous — default to p2 as opponent
            opponent = p2_last

        event_ticker = market.get("event_ticker")
        surface = surface_for_event(event_ticker)

        p_yes, info = predict_match_prob(
            elo_df, yes_player, opponent, surface=surface
        )

        # implied prob from snapshot (may be None if no liquidity)
        implied = (
            implied_prob_from_snapshot(snapshot, side="yes") if snapshot else None
        )

        if implied is None:
            implied_for_dataclass = 0.0
            edge = float("nan")
            implied_str = "n/a"
            edge_str = "n/a"
        else:
            implied_for_dataclass = implied
            edge = p_yes - implied
            implied_str = f"{implied:.3f}"
            edge_str = f"{edge:+.3f}"

        rationale = (
            f"{tour.upper()} {surface} {yes_player} vs {opponent} "
            f"elo({info['elo_a']:.0f} vs {info['elo_b']:.0f}, "
            f"matches {info['matches_a']}/{info['matches_b']}) "
            f"predicted={p_yes:.3f} implied={implied_str} edge={edge_str}"
        )
        return Prediction(
            ticker=ticker,
            predicted_prob=p_yes,
            market_implied_prob=implied_for_dataclass,
            edge=edge,
            rationale=rationale,
        )


def main() -> int:
    """Smoke test against stored tennis markets + latest snapshots."""
    from datetime import datetime, timezone

    from sqlalchemy import desc, select

    from src.db import Market, Snapshot, session_scope

    bl = TennisEloBaseline()

    print("tennis elo baseline smoke test")
    print("=" * 70)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        rows = s.execute(
            select(Market)
            .where(Market.series_ticker.in_(["KXATPMATCH", "KXWTAMATCH",
                                              "KXATPCHALLENGERMATCH",
                                              "KXWTACHALLENGERMATCH"]))
            .where(Market.close_ts > now)
            .order_by(Market.close_ts)
            .limit(20)
        ).scalars().all()

        if not rows:
            print("no future tennis markets — run discover + poller first")
            return 1

        for m in rows[:15]:
            snap = s.execute(
                select(Snapshot)
                .where(Snapshot.ticker == m.ticker)
                .order_by(desc(Snapshot.ts))
                .limit(1)
            ).scalar_one_or_none()

            mdict = {
                "ticker": m.ticker,
                "title": m.title,
                "event_ticker": m.event_ticker,
                "close_ts": m.close_ts,
            }
            sdict = (
                {
                    "yes_bid": snap.yes_bid,
                    "yes_ask": snap.yes_ask,
                    "no_bid": snap.no_bid,
                    "no_ask": snap.no_ask,
                }
                if snap
                else None
            )
            pred = bl.predict(mdict, sdict)
            if pred is None:
                print(f"  {m.ticker:50s}  (no prediction — likely unparseable title)")
            else:
                print(f"  {m.ticker:50s}  {pred.rationale}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
