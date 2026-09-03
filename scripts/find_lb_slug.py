#!/usr/bin/env python3
"""One-shot maintenance tool: discover the club's MiScore leaderboard slug.

Used when leaderboard.miclub.com.au says "Organisation doesn't exist" for our
club (first seen 3 Sep 2026): logs into wwcc.com.au with the poller's
credentials and scans members' pages for live-leaderboard links to find what
club= identifier MiClub now uses. Prints findings to stdout; changes nothing.
Run via the Leaderboard Slug Hunt workflow (workflow_dispatch).
"""
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from miscore.live import _wwcc_login, _wwcc_get, _WWCC_BASE  # noqa: E402

PAGES = [
    "/",
    "/spring/home.msp",
    "/common/home.msp",
    "/cms/golf/",
    f"/common/Ajax?doAction=getResults&date={date.today().isoformat()}",
    f"/spring/bookings.msp?selectedDate={date.today().isoformat()}",
    "/common/leaderboard.msp",
    "/spring/leaderboard.msp",
]


def scan(text, source):
    hits = set()
    for m in re.findall(r'https?://[^"\'<>\s]*leaderboard[^"\'<>\s]*', text, re.I):
        hits.add(m)
    for m in re.findall(r'(?:display-leaderboard|show-leaderboards)[^"\'<>\s]*', text, re.I):
        hits.add(m)
    for m in re.findall(r'club=([A-Za-z0-9_-]+)', text):
        hits.add(f"club={m}")
    for h in sorted(hits):
        print(f"  [{source}] {h[:160]}")
    return hits


def main():
    jar = _wwcc_login()
    print("login:", "OK" if jar else "FAILED (checking public pages only)")
    found = set()
    for path in PAGES:
        url = _WWCC_BASE + path
        try:
            page = _wwcc_get(url, jar)
            print(f"fetched {path} ({len(page)} bytes)")
            found |= scan(page, path)
        except Exception as e:
            print(f"fetch {path} failed: {e}")

    # Also probe the leaderboard host with any club= values we discovered
    import urllib.request
    slugs = {h.split("=", 1)[1] for h in found if h.startswith("club=")}
    for slug in sorted(slugs):
        try:
            req = urllib.request.Request(
                f"https://leaderboard.miclub.com.au/show-leaderboards?club={slug}&days=3",
                headers={"User-Agent": "Mozilla/5.0"})
            p = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
            ok = "exist" not in p[:2000]
            n = len(re.findall(r"leaderboardId=\d+", p))
            print(f"probe club={slug}: {'WORKS - ' + str(n) + ' boards' if ok else 'org missing'}")
        except Exception as e:
            print(f"probe club={slug}: {e}")

    if not found:
        print("No leaderboard references found on any scanned page.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
