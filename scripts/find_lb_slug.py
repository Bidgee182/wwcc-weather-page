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
    crawl_queue = []
    for path in PAGES:
        url = _WWCC_BASE + path
        try:
            page = _wwcc_get(url, jar)
            print(f"fetched {path} ({len(page)} bytes)")
            found |= scan(page, path)
            if path == "/":
                # queue same-host links that smell like golf/comp/score pages
                for href in set(re.findall(r'href="([^"]+)"', page)):
                    if not re.search(r"golf|comp|result|score|leader|event|fixture", href, re.I):
                        continue
                    if href.startswith("#") or href.startswith("mailto"):
                        continue
                    if href.startswith("http"):
                        if _WWCC_BASE not in href:
                            continue
                        href = href[len(_WWCC_BASE):]
                    crawl_queue.append(href if href.startswith("/") else "/" + href)
        except Exception as e:
            print(f"fetch {path} failed: {e}")

    for path in sorted(set(crawl_queue))[:20]:
        try:
            page = _wwcc_get(_WWCC_BASE + path, jar)
            hits = scan(page, path)
            if hits:
                print(f"crawled {path} ({len(page)} bytes) - {len(hits)} hit(s)")
            found |= hits
        except Exception as e:
            print(f"crawl {path} failed: {type(e).__name__}")

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
