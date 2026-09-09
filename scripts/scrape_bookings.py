#!/usr/bin/env python3
"""
WWCC bookings / tee-sheet scraper (MiClub member portal).

Pulls the day's tee sheet - times, groups, player names - which the live
leaderboard scrape does NOT capture. Logs in with the same member creds as
miscore/live.py (WWCC_USERNAME / WWCC_PASSWORD), fetches the timesheet URL,
always saves the raw logged-in page (out/bookings_raw.html) so it can be
eyeballed, and makes a best-effort parse into out/bookings.html + .json.

Pass the exact tee-sheet URL from the browser address bar with --url; the
default is the bare ViewPublishedEvent endpoint, which usually needs a
resource/date query string to show a real sheet.

Usage:
    WWCC_USERNAME=.. WWCC_PASSWORD=.. python scripts/scrape_bookings.py --url "<tee sheet URL>"
"""

import argparse
import html
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
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
            print("login OK (cookies: %s)" % [c.name for c in jar])
            return opener
    print("ERROR: login failed - check credentials", file=sys.stderr)
    return None


def get(opener, url):
    """GET returning (body, final_url, status). Never raises on HTTP errors."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with opener.open(req, timeout=20) as r:
            return r.read().decode("utf-8", "replace"), r.url, r.status
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        return body, url, e.code
    except Exception as e:
        return "(request failed: %s)" % e, url, 0


def parse_timesheet(body):
    """Best-effort: pull (time, [names]) rows from a MiClub timesheet.

    Looks for a time token (h:mm, optional am/pm) in each table row and the
    name-shaped cells alongside it. Returns [{time, players[]}]; empty when the
    markup does not match - then the raw dump is used to refine this.
    """
    slots = []
    rows = re.split(r"<tr\b", body, flags=re.I)
    time_re = re.compile(r"\b(\d{1,2}[:.]\d{2}\s*(?:am|pm)?)\b", re.I)
    junk = ("available", "book", "cart", "buggy", "competition", "resource",
            "member", "guest", "total", "hole", "tee")
    for row in rows:
        tm = time_re.search(re.sub(r"<[^>]+>", " ", row))
        if not tm:
            continue
        names = []
        for cm in re.finditer(r"<td[^>]*>(.*?)</td>", row, re.I | re.S):
            txt = html.unescape(re.sub(r"<[^>]+>", " ", cm.group(1))).strip()
            txt = re.sub(r"\s+", " ", txt)
            if not txt or time_re.search(txt) or len(txt) > 40:
                continue
            looks_name = re.search(r"[A-Za-z]{2,},\s*[A-Za-z]", txt) or (
                2 <= len(txt.split()) <= 4 and txt[0].isalpha())
            if looks_name and not any(w in txt.lower() for w in junk):
                names.append(txt)
        if names:
            slots.append({"time": tm.group(1).strip(), "players": names})
    return slots


def render_html(slots, title, source_url):
    rows = "".join(
        "<tr><td class='t'>%s</td><td>%s</td></tr>" % (
            html.escape(s["time"]), html.escape(", ".join(s["players"])))
        for s in slots)
    n = sum(len(s["players"]) for s in slots)
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>%s</title>'
        '<style>body{font-family:Arial,sans-serif;background:#1a252f;color:#ecf0f1;padding:20px}'
        'h1{font-size:1.3em}.sub{color:#7f8c8d;font-size:.85em;margin-bottom:16px}'
        'table{border-collapse:collapse;width:100%%;max-width:720px}'
        'td{padding:7px 10px;border-bottom:1px solid #2c3e50;vertical-align:top}'
        '.t{color:#5dade2;font-weight:bold;white-space:nowrap;width:90px}</style></head><body>'
        '<h1>%s</h1><div class="sub">%d tee times &bull; %d players &bull; scraped %s &bull; source: %s</div>'
        '<table>%s</table></body></html>' % (
            html.escape(title), html.escape(title), len(slots), n,
            datetime.now().strftime("%d %b %Y %H:%M"), html.escape(source_url),
            rows or "<tr><td>No tee times parsed - see out/bookings_raw.html for the raw page.</td></tr>"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="exact timesheet URL (from the browser address bar)")
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    opener = login()
    if not opener:
        sys.exit(1)
    os.makedirs(OUT, exist_ok=True)

    url = args.url or (BASE + "/members/bookings/ViewPublishedEvent.msp")
    body, real, status = get(opener, url)
    with open(os.path.join(OUT, "bookings_raw.html"), "w", encoding="utf-8") as f:
        f.write("<!-- %s -> %s [%s] -->\n" % (url, real, status) + body)
    slots = parse_timesheet(body)
    title = "WWCC Tee Sheet - %s" % args.date
    with open(os.path.join(OUT, "bookings.html"), "w", encoding="utf-8") as f:
        f.write(render_html(slots, title, real))
    with open(os.path.join(OUT, "bookings.json"), "w", encoding="utf-8") as f:
        json.dump({"date": args.date, "source": real, "status": status, "slots": slots}, f, indent=1)

    print("[%s] %s -> %s" % (status, url, real))
    print("parsed %d tee times, %d players" % (len(slots), sum(len(x["players"]) for x in slots)))
    print("wrote out/bookings.html, out/bookings.json, out/bookings_raw.html")
    for x in slots[:8]:
        print("  %8s  %s" % (x["time"], ", ".join(x["players"])))


if __name__ == "__main__":
    main()
