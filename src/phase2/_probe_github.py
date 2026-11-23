"""is the sackmann 404 specific to that repo, or is our whole path to github broken?

the repo renders fine in a browser and via an external fetcher, but this
machine got a genuine github 404 JSON body from api.github.com. so we test
controls: a different repo, a different host, and the rate-limit endpoint.
"""
from __future__ import annotations

import socket
import sys

import httpx

from src.utils import setup_utf8_stdout

setup_utf8_stdout()

TESTS = [
    ("html   sackmann atp", "https://github.com/JeffSackmann/tennis_atp"),
    ("html   control linux", "https://github.com/torvalds/linux"),
    ("api    sackmann atp", "https://api.github.com/repos/JeffSackmann/tennis_atp"),
    ("api    control linux", "https://api.github.com/repos/torvalds/linux"),
    ("api    rate_limit", "https://api.github.com/rate_limit"),
    ("raw    sackmann 2026", "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_2026.csv"),
    ("raw    sackmann 2023", "https://raw.githubusercontent.com/JeffSackmann/tennis_atp/master/atp_matches_2023.csv"),
    ("raw    control linux", "https://raw.githubusercontent.com/torvalds/linux/master/README"),
    ("codeload sackmann zip", "https://codeload.github.com/JeffSackmann/tennis_atp/zip/refs/heads/master"),
]


def main() -> int:
    for host in ("github.com", "api.github.com", "raw.githubusercontent.com"):
        try:
            print(f"  dns {host:<28} -> {socket.gethostbyname(host)}")
        except Exception as e:  # noqa: BLE001
            print(f"  dns {host:<28} -> {type(e).__name__}")
    print()

    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        for label, url in TESTS:
            for headers, tag in ((None, "no-UA"), (ua, "browser-UA")):
                try:
                    r = c.get(url, headers=headers)
                    body = r.text[:90].replace("\n", " ") if r.status_code != 200 else ""
                    print(f"  {label:<22} {tag:<11} -> {r.status_code} "
                          f"{len(r.content) / 1e6:.2f} MB  {body}")
                except Exception as e:  # noqa: BLE001
                    print(f"  {label:<22} {tag:<11} -> {type(e).__name__}: {str(e)[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
