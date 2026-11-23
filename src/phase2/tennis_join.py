"""join kalshi tennis markets to sackmann match records -- and validate the join.

why this exists:
  the elo baseline matched players by string ("last name endswith"), which
  missed 36% of ATP name mentions and silently fell back to elo 1500. it also
  guessed surface from a hand-maintained prefix table.

  instead: find the actual sackmann match. for each kalshi market we know the
  tour, an approximate date (from the ticker) and both surnames (from the
  title). sackmann's tourney_date is the TOURNAMENT start, not the match date,
  so we search a +/-21 day window and require the surname pair to match.

what that buys:
  - exact player_ids -> no fuzzy elo lookup, no silent 1500 fallback
  - the true surface -> replaces the guessed _EVENT_SURFACE table
  - an INDEPENDENT winner -> cross-checked against kalshi's `result`.
    if these disagree at any material rate, the join is wrong and nothing
    downstream is trustworthy. this is the whole point of the step.

downloads are cached under data/tennis/raw/ (delete to force a refresh).

run:
    .venv\\Scripts\\python.exe -m src.phase2.tennis_join
    .venv\\Scripts\\python.exe -m src.phase2.tennis_join --window-days 30
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import httpx
import pandas as pd

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

DB = "data/poker_quant.db"
RAW_DIR = Path("data") / "tennis" / "raw"
OUT_CSV = "data/tennis_join.csv"
SERIES = ("KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH")

# main tour + the lower tiers kalshi actually lists (challengers were never
# downloaded before -- that alone explains a big chunk of the 36% miss rate)
FILES = {
    "atp": ["atp_matches_{y}.csv", "atp_matches_qual_chall_{y}.csv"],
    "wta": ["wta_matches_{y}.csv", "wta_matches_qual_itf_{y}.csv"],
}
REPO = {"atp": "JeffSackmann/tennis_atp", "wta": "JeffSackmann/tennis_wta"}

_TITLE_RE = re.compile(
    r"Will\s+(?P<winner>[A-Za-z\.\-' ]+?)\s+win\s+"
    r"(?:the\s+)?(?P<p1>[A-Za-z\.\-' ]+?)\s+vs\.?\s+(?P<p2>[A-Za-z\.\-' ]+?)[:?,]",
    re.IGNORECASE,
)
_TICKER_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def norm(s: str) -> str:
    """lowercase, strip accents/punctuation -- 'Auger-Aliassime' -> 'augeraliassime'."""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", s.lower())


def surname_key(full_name: str) -> str:
    """last token of a sackmann name, normalized. 'Felix Auger-Aliassime' -> augeraliassime."""
    parts = str(full_name).split()
    return norm(parts[-1]) if parts else ""


def surname_keys_all(full_name: str) -> set[str]:
    """every trailing-token combination, to survive multi-word surnames
    ('Dalla Valle' -> {vallle-ish, dallavalle})."""
    parts = [p for p in str(full_name).split() if p]
    out = set()
    for i in range(1, min(3, len(parts)) + 1):
        out.add(norm("".join(parts[-i:])))
    out.discard("")
    return out


def download(tour: str, years: list[int]) -> pd.DataFrame:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    frames = []
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        for year in years:
            for tpl in FILES[tour]:
                name = tpl.format(y=year)
                cached = RAW_DIR / name
                if cached.exists():
                    frames.append(pd.read_csv(cached, low_memory=False))
                    print(f"  {name}: cached ({len(frames[-1])} rows)")
                    continue
                url = f"https://raw.githubusercontent.com/{REPO[tour]}/master/{name}"
                try:
                    r = client.get(url)
                    r.raise_for_status()
                except Exception as e:  # noqa: BLE001
                    print(f"  {name}: MISSING ({type(e).__name__})")
                    continue
                cached.write_text(r.text, encoding="utf-8")
                df = pd.read_csv(StringIO(r.text), low_memory=False)
                frames.append(df)
                print(f"  {name}: downloaded ({len(df)} rows)")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["tourney_date"] = pd.to_datetime(df["tourney_date"], format="%Y%m%d", errors="coerce")
    return df.dropna(subset=["tourney_date", "winner_name", "loser_name"])


def ticker_date(ticker: str):
    m = _TICKER_DATE_RE.search(ticker)
    if not m:
        return None
    yy, mon, dd = m.groups()
    if mon not in _MONTHS:
        return None
    try:
        return datetime(2000 + int(yy), _MONTHS[mon], int(dd))
    except ValueError:
        return None


def load_markets() -> list[dict]:
    con = sqlite3.connect(f"file:{DB}?immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        f"""select ticker, series_ticker, event_ticker, title, close_ts, settled_outcome
            from markets where series_ticker in ({','.join('?' * len(SERIES))})""",
        SERIES,
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def build_index(df: pd.DataFrame) -> dict:
    """(surname_a, surname_b) -> list of match rows, unordered pair key."""
    idx = defaultdict(list)
    for row in df.itertuples(index=False):
        wk = surname_keys_all(row.winner_name)
        lk = surname_keys_all(row.loser_name)
        for a in wk:
            for b in lk:
                idx[frozenset((a, b))].append(row)
    return idx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2025, 2026])
    ap.add_argument("--window-days", type=int, default=21)
    ap.add_argument("--out", default=OUT_CSV)
    args = ap.parse_args()

    markets = load_markets()
    print(f"kalshi tennis match markets: {len(markets)}")

    data = {}
    for tour in ("atp", "wta"):
        print(f"\n[{tour.upper()}] sackmann files")
        df = download(tour, args.years)
        if df.empty:
            print(f"  no data for {tour} -- cannot join")
            return 1
        print(f"  {len(df)} matches, {df.surface.nunique()} surfaces, "
              f"{df.tourney_date.min().date()} -> {df.tourney_date.max().date()}")
        data[tour] = {"df": df, "idx": build_index(df)}

    stats = Counter()
    rows = []
    for m in markets:
        tour = "atp" if "KXATP" in m["ticker"].upper() else "wta"
        stats[f"{tour}_total"] += 1

        parsed = _TITLE_RE.search(m["title"] or "")
        if not parsed:
            stats["unparsed_title"] += 1
            continue
        yes_name = parsed.group("winner").strip()
        p1, p2 = parsed.group("p1").strip(), parsed.group("p2").strip()

        mdate = ticker_date(m["ticker"])
        if mdate is None:
            stats["no_ticker_date"] += 1
            continue

        cands = []
        for a in surname_keys_all(p1):
            for b in surname_keys_all(p2):
                cands.extend(data[tour]["idx"].get(frozenset((a, b)), []))
        # dedupe + date window
        seen = set()
        window = []
        for r in cands:
            key = (r.winner_name, r.loser_name, r.tourney_date, getattr(r, "round", ""))
            if key in seen:
                continue
            seen.add(key)
            if abs((r.tourney_date.to_pydatetime() - mdate).days) <= args.window_days:
                window.append(r)

        if not window:
            stats[f"{tour}_no_match"] += 1
            continue
        if len({(r.winner_name, r.loser_name) for r in window}) > 1:
            stats[f"{tour}_ambiguous"] += 1
            continue

        r = min(window, key=lambda x: abs((x.tourney_date.to_pydatetime() - mdate).days))
        stats[f"{tour}_joined"] += 1

        # who does sackmann say won, in kalshi's yes/no terms?
        yk = surname_keys_all(yes_name)
        win_k = surname_keys_all(r.winner_name)
        lose_k = surname_keys_all(r.loser_name)
        if yk & win_k and not (yk & lose_k):
            sack_result = "yes"
        elif yk & lose_k and not (yk & win_k):
            sack_result = "no"
        else:
            sack_result = "?"
            stats["yes_side_ambiguous"] += 1

        agree = ""
        if m["settled_outcome"] in ("yes", "no") and sack_result in ("yes", "no"):
            agree = "OK" if sack_result == m["settled_outcome"] else "MISMATCH"
            stats[f"agree_{agree}"] += 1

        rows.append({
            "ticker": m["ticker"], "series_ticker": m["series_ticker"],
            "close_ts": m["close_ts"], "match_date_ticker": mdate.date(),
            "tourney_date": r.tourney_date.date(), "tourney_name": getattr(r, "tourney_name", ""),
            "round": getattr(r, "round", ""), "surface": getattr(r, "surface", ""),
            "yes_name": yes_name,
            "winner_id": r.winner_id, "winner_name": r.winner_name,
            "loser_id": r.loser_id, "loser_name": r.loser_name,
            "sackmann_result": sack_result, "kalshi_result": m["settled_outcome"],
            "agreement": agree,
        })

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["ticker"])
        w.writeheader()
        w.writerows(rows)

    print("\n" + "=" * 74)
    print("JOIN RESULT")
    print("=" * 74)
    for tour in ("atp", "wta"):
        tot = stats[f"{tour}_total"]
        j = stats[f"{tour}_joined"]
        print(f"  {tour.upper():<4} total {tot:>6}  joined {j:>6} ({100.0 * j / max(tot, 1):5.1f}%)  "
              f"no_match {stats[f'{tour}_no_match']:>5}  ambiguous {stats[f'{tour}_ambiguous']:>4}")
    print(f"  unparsed titles {stats['unparsed_title']}  no ticker date {stats['no_ticker_date']}")

    ok, bad = stats["agree_OK"], stats["agree_MISMATCH"]
    tot = ok + bad
    print("\n" + "=" * 74)
    print("VALIDATION: sackmann winner vs kalshi settled_outcome")
    print("=" * 74)
    if tot:
        print(f"  compared {tot}  agree {ok}  MISMATCH {bad}  ->  {100.0 * ok / tot:.4f}%")
        if bad:
            print("  first 5 mismatches:")
            for r in [x for x in rows if x["agreement"] == "MISMATCH"][:5]:
                print(f"    {r['ticker']}  yes={r['yes_name']}  "
                      f"sackmann winner={r['winner_name']}  kalshi={r['kalshi_result']}")
    else:
        print("  nothing to compare -- no market had both a result and a join")

    if stats["yes_side_ambiguous"]:
        print(f"\n  yes-side unresolvable on {stats['yes_side_ambiguous']} markets "
              "(both/neither surname matched the yes player)")

    surf = Counter(r["surface"] for r in rows)
    print(f"\n  surfaces from sackmann: {dict(surf)}")
    print(f"\n  -> {args.out} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
