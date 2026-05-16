"""tennis data + surface-adjusted Elo ratings.

source: JeffSackmann/tennis_atp + tennis_wta on github (free, public)
        https://github.com/JeffSackmann/tennis_atp

elo algorithm (standard 538-style):
  K = 250 / (matches_played + 5)^0.4   (decays as players play more)
  expected = 1 / (1 + 10^((opponent_elo - player_elo) / 400))
  delta = K * (actual_won - expected)

we keep separate elo per surface (hard / clay / grass / carpet).
on a new player, default 1500.

usage:
  .venv\\Scripts\\python.exe -m src.spot.tennis_data --refresh   # download + recompute
  .venv\\Scripts\\python.exe -m src.spot.tennis_data --top 30    # print top 30 by hard-court elo
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

# data cache lives next to the db
_DATA_DIR = Path("data") / "tennis"
_ELO_CACHE_ATP = _DATA_DIR / "elo_atp.parquet"
_ELO_CACHE_WTA = _DATA_DIR / "elo_wta.parquet"

# sackmann csv pattern
_ATP_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_{year}.csv"
_WTA_URL = "https://raw.githubusercontent.com/JeffSackmann/tennis_wta/master/wta_matches_{year}.csv"

# surface normalization
_SURFACE_MAP = {
    "Hard": "hard",
    "Clay": "clay",
    "Grass": "grass",
    "Carpet": "carpet",
}


def download_matches(tour: str, years: list[int]) -> pd.DataFrame:
    """Download + concat match CSVs for given years. tour in {'atp','wta'}."""
    if tour == "atp":
        url_tpl = _ATP_URL
    elif tour == "wta":
        url_tpl = _WTA_URL
    else:
        raise ValueError(f"tour must be 'atp' or 'wta', got {tour!r}")

    dfs: list[pd.DataFrame] = []
    with httpx.Client(timeout=30.0) as client:
        for year in years:
            url = url_tpl.format(year=year)
            try:
                r = client.get(url)
                r.raise_for_status()
                from io import StringIO

                df = pd.read_csv(StringIO(r.text))
                df["year"] = year
                dfs.append(df)
                print(f"  {tour} {year}: {len(df)} matches")
            except Exception as e:
                print(f"  {tour} {year}: FAILED ({e})")
    if not dfs:
        return pd.DataFrame()
    return pd.concat(dfs, ignore_index=True)


def compute_elo(matches: pd.DataFrame) -> pd.DataFrame:
    """Walk matches chronologically, compute Elo per (player, surface).

    returns dataframe with columns: player_id, player_name, surface, elo, matches_played, last_match
    """
    # keep needed cols, drop bad rows
    needed = ["tourney_date", "surface", "winner_id", "winner_name", "loser_id", "loser_name"]
    matches = matches.dropna(subset=needed)
    matches["surface_norm"] = matches["surface"].map(_SURFACE_MAP).fillna("other")
    matches["tourney_date"] = pd.to_datetime(matches["tourney_date"], format="%Y%m%d", errors="coerce")
    matches = matches.dropna(subset=["tourney_date"]).sort_values("tourney_date").reset_index(drop=True)

    # (player_id, surface) -> {elo, matches, last_date, name}
    state: dict[tuple, dict] = {}

    def get_or_init(pid, name, surface):
        key = (pid, surface)
        if key not in state:
            state[key] = {"elo": 1500.0, "matches": 0, "last_date": None, "name": name}
        return state[key]

    for _, row in matches.iterrows():
        surface = row["surface_norm"]
        w_state = get_or_init(row["winner_id"], row["winner_name"], surface)
        l_state = get_or_init(row["loser_id"], row["loser_name"], surface)

        # k-factor decays with matches played (538 style)
        k_w = 250.0 / pow(w_state["matches"] + 5, 0.4)
        k_l = 250.0 / pow(l_state["matches"] + 5, 0.4)

        # expected (from winner's perspective)
        diff = l_state["elo"] - w_state["elo"]
        expected_w = 1.0 / (1.0 + math.pow(10.0, diff / 400.0))

        # update
        w_state["elo"] += k_w * (1.0 - expected_w)
        l_state["elo"] += k_l * (0.0 - (1.0 - expected_w))
        w_state["matches"] += 1
        l_state["matches"] += 1
        w_state["last_date"] = row["tourney_date"]
        l_state["last_date"] = row["tourney_date"]
        # keep latest name (in case of rename)
        w_state["name"] = row["winner_name"]
        l_state["name"] = row["loser_name"]

    # serialize to dataframe
    rows = []
    for (pid, surface), data in state.items():
        rows.append({
            "player_id": pid,
            "player_name": data["name"],
            "surface": surface,
            "elo": data["elo"],
            "matches_played": data["matches"],
            "last_match": data["last_date"],
        })
    return pd.DataFrame(rows)


def refresh_cache(tour: str, years: list[int]) -> pd.DataFrame:
    print(f"\n[{tour.upper()}] downloading matches for {len(years)} years...")
    matches = download_matches(tour, years)
    if matches.empty:
        print(f"  no matches downloaded for {tour}, aborting")
        return pd.DataFrame()

    print(f"  total matches: {len(matches)}")
    print(f"  computing elo across {matches['surface'].nunique()} surfaces...")
    elo = compute_elo(matches)
    print(f"  computed {len(elo)} (player, surface) elo ratings")

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _ELO_CACHE_ATP if tour == "atp" else _ELO_CACHE_WTA
    elo.to_parquet(cache_path, index=False)
    print(f"  cached -> {cache_path}")
    return elo


def load_elo(tour: str) -> pd.DataFrame:
    """Load cached elo. Caller refreshes when stale."""
    cache_path = _ELO_CACHE_ATP if tour == "atp" else _ELO_CACHE_WTA
    if not cache_path.exists():
        raise FileNotFoundError(f"no elo cache at {cache_path} — run with --refresh first")
    return pd.read_parquet(cache_path)


def predict_match_prob(
    elo_df: pd.DataFrame,
    player_a_name: str,
    player_b_name: str,
    surface: str = "hard",
    default_elo: float = 1500.0,
) -> tuple[float, dict]:
    """Returns (P(A wins), debug_info).

    Tries exact name match (case-insensitive). For unknown players uses default_elo.
    """
    surface = surface.lower() if surface else "hard"
    surface_df = elo_df[elo_df["surface"] == surface]

    def lookup(name: str) -> tuple[float, int]:
        if not name:
            return default_elo, 0
        # case-insensitive contains match on last word (last name)
        lname = name.lower().strip()
        # exact match first
        exact = surface_df[surface_df["player_name"].str.lower() == lname]
        if len(exact):
            row = exact.iloc[0]
            return float(row["elo"]), int(row["matches_played"])
        # last-name match
        last = lname.split()[-1] if lname.split() else lname
        partial = surface_df[surface_df["player_name"].str.lower().str.endswith(" " + last)]
        if len(partial):
            row = partial.iloc[0]
            return float(row["elo"]), int(row["matches_played"])
        return default_elo, 0

    elo_a, mp_a = lookup(player_a_name)
    elo_b, mp_b = lookup(player_b_name)
    diff = elo_b - elo_a
    p_a = 1.0 / (1.0 + math.pow(10.0, diff / 400.0))
    return p_a, {
        "elo_a": elo_a, "matches_a": mp_a,
        "elo_b": elo_b, "matches_b": mp_b,
        "surface": surface,
        "diff": diff,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true", help="download fresh data + recompute elo")
    parser.add_argument("--tour", choices=["atp", "wta", "both"], default="both")
    parser.add_argument(
        "--years", type=int, nargs="+", default=None,
        help="years to fetch (default: 2022-current)",
    )
    parser.add_argument("--top", type=int, default=10, help="show top N players by hard-court elo")
    args = parser.parse_args()

    current_year = datetime.now().year
    years = args.years or list(range(2022, current_year + 1))
    tours = ["atp", "wta"] if args.tour == "both" else [args.tour]

    for tour in tours:
        if args.refresh:
            refresh_cache(tour, years)

        try:
            elo = load_elo(tour)
        except FileNotFoundError as e:
            print(f"{tour}: {e}")
            continue

        hard = elo[(elo["surface"] == "hard") & (elo["matches_played"] >= 20)]
        clay = elo[(elo["surface"] == "clay") & (elo["matches_played"] >= 20)]
        print(f"\n[{tour.upper()}] elo cache: {len(elo)} entries")
        print(f"  top {args.top} hard-court:")
        for _, row in hard.nlargest(args.top, "elo").iterrows():
            print(f"    {row['player_name']:30s}  elo={row['elo']:6.1f}  matches={row['matches_played']:4d}")
        print(f"  top {args.top} clay-court:")
        for _, row in clay.nlargest(args.top, "elo").iterrows():
            print(f"    {row['player_name']:30s}  elo={row['elo']:6.1f}  matches={row['matches_played']:4d}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
