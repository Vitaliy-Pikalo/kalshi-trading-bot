"""find a reachable source of ATP + WTA match results.

the sackmann repos 404 from this machine on every github host, while
control repos return 200. so: discover clones/forks via github's search API
(which works here), then test whether they actually serve the match files.

also checks two independent sources:
  Tennismylife/TML-Database  ATP yearly csvs, updated daily
  tennis-data.co.uk          ATP+WTA main tour, includes closing odds
"""
from __future__ import annotations

import sys

import httpx

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Accept": "application/vnd.github+json"}
WANT = {"atp": ["atp_matches_2026.csv", "atp_matches_2025.csv",
                "atp_matches_qual_chall_2026.csv"],
        "wta": ["wta_matches_2026.csv", "wta_matches_2025.csv",
                "wta_matches_qual_itf_2026.csv"]}


def head_ok(c: httpx.Client, url: str) -> tuple[int, float]:
    try:
        r = c.get(url, headers={"User-Agent": UA["User-Agent"]})
        return r.status_code, len(r.content) / 1e6
    except Exception:  # noqa: BLE001
        return -1, 0.0


def search(c: httpx.Client, name: str) -> list[dict]:
    url = "https://api.github.com/search/repositories"
    try:
        r = c.get(url, headers=UA, params={"q": f"{name} in:name", "per_page": 15,
                                           "sort": "updated"})
        if r.status_code != 200:
            print(f"  search {name}: HTTP {r.status_code} {r.text[:120]}")
            return []
        return r.json().get("items", [])
    except Exception as e:  # noqa: BLE001
        print(f"  search {name}: {type(e).__name__}")
        return []


def main() -> int:
    with httpx.Client(timeout=45.0, follow_redirects=True) as c:
        for tour in ("atp", "wta"):
            print("=" * 78)
            print(f"candidate mirrors for tennis_{tour}")
            print("=" * 78)
            items = search(c, f"tennis_{tour}")
            if not items:
                print("  no search results")
            for it in items:
                full = it["full_name"]
                if full.lower().startswith("jeffsackmann/"):
                    print(f"  {full:<45} (the blocked original, skipping)")
                    continue
                branch = it.get("default_branch", "master")
                hits = []
                for fname in WANT[tour]:
                    code, mb = head_ok(
                        c, f"https://raw.githubusercontent.com/{full}/{branch}/{fname}")
                    if code == 200:
                        hits.append(f"{fname.split('_')[-1].replace('.csv','')}:{mb:.1f}MB")
                stars = it.get("stargazers_count", 0)
                pushed = str(it.get("pushed_at", ""))[:10]
                print(f"  {full:<45} {branch:<8} pushed {pushed} stars {stars:<5} "
                      f"-> {', '.join(hits) if hits else 'no match files'}")
            print()

        print("=" * 78)
        print("independent sources")
        print("=" * 78)
        for label, url in [
            ("TML ATP 2026", "https://raw.githubusercontent.com/Tennismylife/TML-Database/master/2026.csv"),
            ("TML ATP 2025", "https://raw.githubusercontent.com/Tennismylife/TML-Database/master/2025.csv"),
            ("tennis-data ATP 2026", "http://www.tennis-data.co.uk/2026/2026.zip"),
            ("tennis-data WTA 2026", "http://www.tennis-data.co.uk/2026w/2026.zip"),
        ]:
            code, mb = head_ok(c, url)
            print(f"  {label:<24} -> {code} {mb:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
