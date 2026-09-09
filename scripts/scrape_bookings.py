#!/usr/bin/env python3
"""
WWCC bookings / tee-sheet scraper (MiClub member portal).

Purpose: pull the day's tee sheet - times, groups, player names - which the
live leaderboard scrape does NOT capture. Unlocks playing-group and
morning/afternoon story angles, and is useful on its own as a viewable sheet.

Because the exact MiClub timesheet markup for WWCC has not been seen yet, this
runs in two halves:
  1. Always saves the RAW logged-in page to out/bookings_raw.html so it can be
     eyeballed (open it, or view it on the Pages site after the workflow
     commits it). This is the "so you can see it" part.
  2. Makes a best-effort parse of time-slots + names into a clean
     out/bookings.html and out/bookings.json. If the parse comes back thin,
     the raw dump is there to refine the parser against real markup.

Login reuses the same member creds as miscore/live.py (WWCC_USERNAME /
WWCC_PASSWORD). Discovery: with no --url given, it logs in, opens the bookings
landing page and lists the timesheet links it finds so the right one can be
picked.

Usage:
    WWCC_USERNAME=.. WWCC_PASSWORD=.. python scripts/scrape_bookings.py
    python scripts/scrape_bookings.py --url "https://wwcc.com.au/members/bookings/ViewPublishedEvent.msp?..."
    python scripts/scrape_bookings.py --date 2026-09-16
"""

import argparse
import html
import http.cookiejar
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, date

BASE = "https://wwcc.com.au"
USER = os.getenv("WWCC_USERNAME", "")
PWD  = os.getenv("WWCC_PASSWORD", "")
UA   = "Mozilla/5.0 (compatible; WWCC-Kiosk/1.0)"
OUT  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "out")


def login():
    if not USER or not PWD:
        print("ERROR: WWCC_USERNAME / WWCC_PASSWORD not set", file=sys.stderr)
        return None
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    data = urllib.parse.urlencode({"username": USER, "password": PWD}).encode()
    req = urllib.request.Request(f"{BASE}/spring/login", data=data,
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
    with opener.open(req, timeout=20) as r:
        body = r.read().decode("utf-8", "replace")
        if r.status == 200 and "pageName=login" not in r.url and "formLogin" not in body:
            print(f"login OK (cookies: {[c.name for c in jar]})")
            return opener
    print("ERROR: login failed - check credentials", file=sys.stderr)
    return None


def get(opener, url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener.open(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace"), r.url


def find_timesheet_links(body):
    """Timesheet / published-event links on the bookings landing page."""
    links = []
    for m in re.finditer(r'href="([^"]*(?:ViewPublishedEvent|timesheet|TimeSheet|booking)[^"]*)"',
                         body, re.I):
        href = html.unescape(m.group(1))
        if not href.startswith("http"):
            href = BASE + ("" if href.startswith("/") else "/") + href.lstrip("/")
        if href not in links:
            links.append(href)
    return links


def parse_timesheet(body):
    """Best-effort: pull (time, [names]) rows from a MiClub timesheet.

    MiClub timesheets vary; this looks for time tokens (h:mm am/pm or 24h) and
    the player-name cells that follow within the same row/table block. Returns
    a list of {time, players[]}. When the markup does not match, returns [] and
    the caller falls back to the raw dump.
    """
    slots = []
    # split into row-ish blocks on <tr>; MiClub uses table rows per time slot
    rows = re.split(r"<tr\b", body, flags=re.I)
    time_re = re.compile(r"\b(\d{1,2}[:.]\d{2}\s*(?:am|pm)?)\b", re.I)
    for row in rows:
        tm = time_re.search(re.sub(r"<[^>]+>", " ", row))
        if not tm:
            continue
        # player names: cells with a member-ish anchor or a "Surname, First" pattern
        names = []
        for cm in re.finditer(r"<td[^>]*>(.*?)</td>", row, re.I | re.S):
            txt = html.unescape(re.sub(r"<[^>]+>", " ", cm.group(1))).strip()
            txt = re.sub(r"\s+", " ", txt)
            if not txt or time_re.search(txt):
                continue
            # a name looks like "Surname, First" or "First Surname", not a header
            if re.search(r"[A-Za-z]{2,},\s*[A-Za-z]", txt) or (
                    2 <= len(txt.split()) <= 4 and txt[0].isalpha()
                    and txt.lower() not in ("available", "booking", "book now", "member")):
                if len(txt) <= 40 and not any(w in txt.lower() for w in
                        ("available", "book", "cart", "buggy", "competition", "resource")):
                    names.append(txt)
        if names:
            slots.append({"time": tm.group(1).strip(), "players": names})
    return slots


def render_html(slots, title, source_url):
    rows = "".join(
        f"<tr><td class='t'>{html.escape(s['time'])}</td>"
        f"<td>{html.escape(', '.join(s['players']))}</td></tr>"
        for s in slots)
    n_players = sum(len(s["players"]) for s in slots)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>body{{font-family:Arial,sans-serif;background:#1a252f;color:#ecf0f1;padding:20px}}
h1{{font-size:1.3em}} .sub{{color:#7f8c8d;font-size:.85em;margin-bottom:16px}}
table{{border-collapse:collapse;width:100%;max-width:720px}}
td{{padding:7px 10px;border-bottom:1px solid #2c3e50;vertical-align:top}}
.t{{color:#5dade2;font-weight:bold;white-space:nowrap;width:90px}}
</style></head><body>
<h1>{html.escape(title)}</h1>
<div class="sub">{len(slots)} tee times &bull; {n_players} players &bull;
scraped {datetime.now().strftime('%d %b %Y %H:%M')} &bull;
source: {html.escape(source_url)}</div>
<table>{rows or '<tr><td>No tee times parsed - see out/bookings_raw.html for the raw page.</td></tr>'}</table>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="explicit timesheet URL to scrape")
    ap.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD")
    ap.add_argument("--landing", default=f"{BASE}/members/bookings/",
                    help="bookings landing page to discover timesheet links")
    args = ap.parse_args()

    opener = login()
    if not opener:
        sys.exit(1)
    os.makedirs(OUT, exist_ok=True)

    url = args.url
    if not url:
        body, real = get(opener, args.landing)
        with open(os.path.join(OUT, "bookings_landing_raw.html"), "w", encoding="utf-8") as f:
            f.write(body)
        links = find_timesheet_links(body)
        print(f"bookings landing: {real}")
        print(f"timesheet links found ({len(links)}):")
        for l in links[:30]:
            print("  ", l)
        if not links:
            print("No timesheet links auto-found. Open out/bookings_landing_raw.html to "
                  "find the tee-sheet URL, then re-run with --url '<that URL>'.")
            return
        url = links[0]
        print(f"\nusing first link: {url}")

    body, real = get(opener, url)
    with open(os.path.join(OUT, "bookings_raw.html"), "w", encoding="utf-8") as f:
        f.write(body)
    slots = parse_timesheet(body)
    title = f"WWCC Tee Sheet - {args.date}"
    with open(os.path.join(OUT, "bookings.html"), "w", encoding="utf-8") as f:
        f.write(render_html(slots, title, real))
    with open(os.path.join(OUT, "bookings.json"), "w", encoding="utf-8") as f:
        json.dump({"date": args.date, "source": real, "slots": slots}, f, indent=1)

    print(f"\nparsed {len(slots)} tee times, "
          f"{sum(len(s['players']) for s in slots)} players")
    print("wrote out/bookings.html (clean view), out/bookings.json, "
          "out/bookings_raw.html (raw page to refine the parser)")
    for s in slots[:6]:
        print(f"  {s['time']:>8}  {', '.join(s['players'])}")


if __name__ == "__main__":
    main()
