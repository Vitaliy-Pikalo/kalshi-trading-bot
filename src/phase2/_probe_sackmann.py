"""diagnose why the sackmann CSV downloads fail.

the elo cache was built from these URLs on 2026-05-16, so they worked then.
checks, in order: does the repo exist, what is its default branch, which
match files does it actually contain, and do the raw URLs resolve on
master vs main.
"""
from __future__ import annotations

import sys

import httpx

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

REPOS = ["JeffSackmann/tennis_atp", "JeffSackmann/tennis_wta"]
UA = {"User-Agent": "kalshi-bot-probe/1.0", "Accept": "application/vnd.github+json"}


def main() -> int:
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        for repo in REPOS:
            print("=" * 70)
            print(repo)
            print("=" * 70)

            # 1. repo metadata + default branch
            branch = "master"
            try:
                r = c.get(f"https://api.github.com/repos/{repo}", headers=UA)
                print(f"  api /repos      -> {r.status_code}")
                if r.status_code == 200:
                    j = r.json()
                    branch = j.get("default_branch", "master")
                    print(f"  default_branch  -> {branch}")
                    print(f"  pushed_at       -> {j.get('pushed_at')}")
                else:
                    print(f"  body: {r.text[:200]}")
            except Exception as e:  # noqa: BLE001
                print(f"  api /repos      -> {type(e).__name__}: {e}")

            # 2. what match files actually exist
            try:
                r = c.get(f"https://api.github.com/repos/{repo}/contents/", headers=UA)
                print(f"  api /contents   -> {r.status_code}")
                if r.status_code == 200:
                    names = [x["name"] for x in r.json()
                             if "matches" in x["name"] and x["name"].endswith(".csv")]
                    recent = sorted(n for n in names if "2025" in n or "2026" in n)
                    print(f"  match csvs total: {len(names)}")
                    print(f"  2025/2026 files : {recent if recent else 'NONE'}")
            except Exception as e:  # noqa: BLE001
                print(f"  api /contents   -> {type(e).__name__}: {e}")

            # 3. raw URLs, both branches
            tour = "atp" if repo.endswith("atp") else "wta"
            for br in dict.fromkeys([branch, "master", "main"]):
                for name in (f"{tour}_matches_2026.csv", f"{tour}_matches_2025.csv"):
                    url = f"https://raw.githubusercontent.com/{repo}/{br}/{name}"
                    try:
                        r = c.get(url, headers={"User-Agent": UA["User-Agent"]})
                        size = len(r.content) if r.status_code == 200 else 0
                        print(f"  raw {br:<7} {name:<34} -> {r.status_code} "
                              f"{size / 1e6:.2f} MB" if size else
                              f"  raw {br:<7} {name:<34} -> {r.status_code}")
                    except Exception as e:  # noqa: BLE001
                        print(f"  raw {br:<7} {name:<34} -> {type(e).__name__}")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
