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
    """Parse a WWCC MiClub 'Booking Event' tee sheet.

    Structure (confirmed 9 Sep 2026): each slot is a block starting at
    id="heading-N" with <h3>08:00 am</h3> (time) and <h4>1st Tee Morning...</h4>
    (tee label); the players in that slot are <div class="booking-name">Surname,
    First [handicap]</div> entries until the next heading. Returns
    [{time, tee, players:[{name, hcp}]}].
    """
    slots = []
    parts = re.split(r'id="heading-\d+"', body)
    name_re = re.compile(r'class="booking-name"[^>]*>(.*?)</', re.S)
    for chunk in parts[1:]:                       # parts[0] is the pre-first-slot preamble
        h3 = re.search(r"<h3>\s*(.*?)\s*</h3>", chunk, re.S)
        if not h3:
            continue
        time = re.sub(r"<[^>]+>", " ", h3.group(1))
        time = re.sub(r"\s+", " ", time).strip()
        if not re.match(r"\d{1,2}[:.]\d{2}", time):
            continue
        h4 = re.search(r"<h4>\s*(.*?)\s*<", chunk, re.S)
        tee = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h4.group(1))).strip() if h4 else ""
        players = []
        # only look at THIS slot: cut the chunk at the next slot's row container
        cut = re.search(r'id="row-\d+"', chunk)
        block = chunk  # heading and its row share the split; names follow in-chunk
        for nm in name_re.finditer(block):
            raw = re.sub(r"<[^>]+>", " ", nm.group(1))
            raw = re.sub(r"\s+", " ", raw).strip()
            if not raw:
                continue
            hcp = None
            hm = re.search(r"\[([-\d.]+)\]\s*$", raw)
            if hm:
                try:
                    hcp = float(hm.group(1))
                except ValueError:
                    pass
                raw = raw[:hm.start()].strip()
            if raw:
                players.append({"name": raw, "hcp": hcp})
        if players:
            slots.append({"time": time, "tee": tee, "players": players})
    return slots


def _flip(name):
    """\"Surname, First\" -> \"First Surname\" for display."""
    if "," in name:
        sur, first = name.split(",", 1)
        return "%s %s" % (first.strip(), sur.strip())
    return name


def render_html(slots, title, source_url):
    def cell(s):
        who = " &middot; ".join(html.escape(_flip(pl["name"])) for pl in s["players"])
        tee = html.escape(s.get("tee", ""))
        return ("<tr><td class='t'>%s<div class='tee'>%s</div></td><td>%s</td></tr>"
                % (html.escape(s["time"]), tee, who))
    rows = "".join(cell(s) for s in slots)
    n = sum(len(s["players"]) for s in slots)
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>%s</title>'
        '<style>body{font-family:Arial,sans-serif;background:#1a252f;color:#ecf0f1;padding:20px}'
        'h1{font-size:1.3em}.sub{color:#7f8c8d;font-size:.85em;margin-bottom:16px}'
        'table{border-collapse:collapse;width:100%%;max-width:720px}'
        'td{padding:7px 10px;border-bottom:1px solid #2c3e50;vertical-align:top}'
        '.t{color:#5dade2;font-weight:bold;white-space:nowrap;width:150px}.tee{color:#566573;font-size:.8em;font-weight:normal}</style></head><body>'
        '<h1>%s</h1><div class="sub">%d tee times &bull; %d players &bull; scraped %s &bull; source: %s</div>'
        '<table>%s</table></body></html>' % (
            html.escape(title), html.escape(title), len(slots), n,
            datetime.now().strftime("%d %b %Y %H:%M"), html.escape(source_url),
            rows or "<tr><td>No tee times parsed - see out/bookings_raw.html for the raw page.</td></tr>"))


RESOURCE_ID = os.getenv("BOOKING_RESOURCE_ID", "3000000")


def fetch_events(opener, date_iso):
    """The day's events from the Spring JSON API (found via headless capture
    9 Sep 2026): /spring/bookings/events/between/DD-MM-YYYY/DD-MM-YYYY/RESID.
    Returns the list for date_iso (comp events carry a tee-sheet redirectUrl)."""
    dmy = "%s-%s-%s" % tuple(reversed(date_iso.split("-")))     # yyyy-mm-dd -> dd-mm-yyyy
    for host in (BASE, BASE.replace("://", "://www.")):
        u = "%s/spring/bookings/events/between/%s/%s/%s" % (host, dmy, dmy, RESOURCE_ID)
        body, real, st = get(opener, u)
        try:
            data = json.loads(body)
        except Exception:
            continue
        if isinstance(data, list):
            return [e for e in data if e.get("eventDate") == date_iso]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="scrape ONE explicit tee-sheet URL instead of resolving the day")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--all", action="store_true", help="include non-competition events too")
    args = ap.parse_args()

    opener = login()
    if not opener:
        sys.exit(1)
    os.makedirs(OUT, exist_ok=True)

    # single-URL mode (manual / debugging)
    if args.url:
        body, real, status = get(opener, args.url)
        with open(os.path.join(OUT, "bookings_raw.html"), "w", encoding="utf-8") as f:
            f.write("<!-- %s [%s] -->\n" % (args.url, status) + body)
        slots = parse_timesheet(body)
        with open(os.path.join(OUT, "bookings.html"), "w", encoding="utf-8") as f:
            f.write(render_html(slots, "WWCC Tee Sheet - %s" % args.date, real))
        json.dump({"date": args.date, "events": [{"source": real, "slots": slots}]},
                  open(os.path.join(OUT, "bookings.json"), "w", encoding="utf-8"), indent=1)
        print("[%s] %d tee times, %d players" % (status, len(slots),
              sum(len(x["players"]) for x in slots)))
        return

    # resolve the day automatically
    events = fetch_events(opener, args.date)
    print("events on %s: %d" % (args.date, len(events)))
    out_events = []
    for e in events:
        title = e.get("title", "?")
        has_comp = bool(e.get("hasCompetition"))
        if not (has_comp or args.all):
            print("  skip (no comp): %s" % title)
            continue
        rel = e.get("redirectUrl") or ""
        url = rel if rel.startswith("http") else BASE + rel
        body, real, status = get(opener, url)
        slots = parse_timesheet(body)
        print("  %-34s id=%s  %d groups, %d players" % (
            title[:34], e.get("bookingEventId"), len(slots),
            sum(len(s["players"]) for s in slots)))
        out_events.append({
            "bookingEventId": e.get("bookingEventId"),
            "title": title, "gender": e.get("eventGenderCodeFriendly"),
            "status": e.get("eventStatusCodeFriendly"), "source": real,
            "slots": slots,
        })

    with open(os.path.join(OUT, "bookings.json"), "w", encoding="utf-8") as f:
        json.dump({"date": args.date, "scrapedAt": datetime.now().isoformat(),
                   "events": out_events}, f, indent=1)
    # combined viewable page (first comp event, or all stacked)
    html_parts = []
    for ev in out_events:
        html_parts.append(render_html(ev["slots"],
            "%s - %s (%s)" % (ev["title"], args.date, ev.get("status") or ""), ev["source"]))
    with open(os.path.join(OUT, "bookings.html"), "w", encoding="utf-8") as f:
        f.write("\n<hr>\n".join(html_parts) or "<p>No competition tee sheets today.</p>")
    print("wrote out/bookings.json + out/bookings.html (%d comp event(s))" % len(out_events))


if __name__ == "__main__":
    main()
