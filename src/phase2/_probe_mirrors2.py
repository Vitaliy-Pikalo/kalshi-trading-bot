"""second pass at finding ATP + WTA match results.

v1 failed for three reasons, all mine:
  - searched repo NAMES, so it returned student projects, not data clones
  - sorted by recency, so real forks (old, starred) were pushed off the page
  - guessed tennis-data.co.uk's URL scheme (got 300 = content negotiation)

this version: named candidates first, then search sorted by stars, then
hugging face, then a proper look at what TML-Database actually holds.
"""
from __future__ import annotations

import io
import sys

import httpx
import pandas as pd

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

UAH = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
APIH = dict(UAH, Accept="application/vnd.github+json")

# clones found by web search / exact-name search
NAMED = [
    ("stakah/tennis_atp", "atp"),
    ("gilmullinau/tennis_wta", "wta"),
    ("mare-imbrium/tennis-atp", "atp"),
    ("EdwinTh/tennis", "atp"),
    ("msolonskyi/ManTennisData", "atp"),
]
WANT = {"atp": ["atp_matches_2026.csv", "atp_matches_2025.csv",
                "atp_matches_2023.csv", "atp_matches_qual_chall_2026.csv"],
        "wta": ["wta_matches_2026.csv", "wta_matches_2025.csv",
                "wta_matches_2023.csv", "wta_matches_qual_itf_2026.csv"]}


def get(c, url, headers=None, **kw):
    try:
        r = c.get(url, headers=headers or UAH, **kw)
        return r
    except Exception as e:  # noqa: BLE001
        print(f"    {type(e).__name__}: {str(e)[:60]}")
        return None


def test_repo(c, full: str, tour: str):
    hits = []
    for branch in ("master", "main"):
        for fname in WANT[tour]:
            r = get(c, f"https://raw.githubusercontent.com/{full}/{branch}/{fname}")
            if r is not None and r.status_code == 200:
                hits.append(f"{branch}/{fname} {len(r.content)/1e6:.2f}MB")
        if hits:
            break
    print(f"  {full:<32} {tour}  -> {'; '.join(hits) if hits else 'nothing'}")
    return hits


def main() -> int:
    with httpx.Client(timeout=60.0, follow_redirects=True) as c:
        print("=" * 78)
        print("1. named clone candidates")
        print("=" * 78)
        for full, tour in NAMED:
            test_repo(c, full, tour)

        print("\n" + "=" * 78)
        print("2. exact-name search, sorted by stars")
        print("=" * 78)
        for name, tour in (("tennis_atp", "atp"), ("tennis_wta", "wta")):
            r = get(c, "https://api.github.com/search/repositories", headers=APIH,
                    params={"q": f"{name} in:name", "sort": "stars", "per_page": 10})
            if r is None or r.status_code != 200:
                print(f"  search {name}: HTTP {r.status_code if r else '-'}")
                continue
            for it in r.json().get("items", []):
                full = it["full_name"]
                if full.lower().startswith("jeffsackmann/"):
                    continue
                if it["name"].lower() != name:
                    continue
                test_repo(c, full, tour)

        print("\n" + "=" * 78)
        print("3. hugging face datasets")
        print("=" * 78)
        r = get(c, "https://huggingface.co/api/datasets",
                params={"search": "tennis", "limit": 25})
        if r is not None and r.status_code == 200:
            for d in r.json():
                print(f"  {d.get('id'):<55} downloads {d.get('downloads', 0)}")
        else:
            print(f"  HTTP {r.status_code if r else '-'}")

        print("\n" + "=" * 78)
        print("4. what is actually in TML-Database")
        print("=" * 78)
        for year in (2026, 2025):
            r = get(c, f"https://raw.githubusercontent.com/Tennismylife/TML-Database/master/{year}.csv")
            if r is None or r.status_code != 200:
                print(f"  {year}: HTTP {r.status_code if r else '-'}")
                continue
            df = pd.read_csv(io.StringIO(r.text), low_memory=False)
            print(f"  {year}: {len(df)} rows, {len(df.columns)} cols")
            for col in ("Date", "Tournament", "Series", "Surface", "Round"):
                if col in df.columns:
                    vals = df[col].dropna()
                    if col == "Date" and len(vals):
                        print(f"    Date range: {vals.min()} -> {vals.max()}")
                    elif col in ("Series", "Surface") and len(vals):
                        print(f"    {col}: {dict(vals.value_counts().head(6))}")
            print(f"    columns: {list(df.columns)[:14]}")

        print("\n" + "=" * 78)
        print("5. tennis-data.co.uk, correct extensions")
        print("=" * 78)
        for label, url in [
            ("ATP 2026 xlsx", "http://www.tennis-data.co.uk/2026/2026.xlsx"),
            ("ATP 2026 xls", "http://www.tennis-data.co.uk/2026/2026.xls"),
            ("WTA 2026 xlsx", "http://www.tennis-data.co.uk/2026w/2026.xlsx"),
            ("ATP all zip", "http://www.tennis-data.co.uk/alldata.php"),
        ]:
            r = get(c, url)
            print(f"  {label:<16} -> {r.status_code if r else '-'} "
                  f"{len(r.content)/1e6 if r else 0:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
