#!/usr/bin/env python3
"""
Hourly live-weather-path check (runs from the Watchdog workflow's schedule).

Background (24 Aug 2026): the greenkeeper and lake pages fetch live WeatherLink
data from the BROWSER, which needs a CORS relay. The free relay the pages used
(corsproxy.io) ended anonymous access and the fallback (allorigins.win) never
forwarded the auth header, so every live figure silently became "--" until a
human noticed. Server-side emails kept working, so no alarm fired.

This check exercises the exact same path the browser now uses - the club's own
Supabase relay (wx-proxy edge function) -> api.weatherlink.com - and emails the
admins when it breaks, plus when it recovers. It distinguishes:

  relay_down     - the Supabase relay is unreachable / erroring (pages will
                   show "--" even though the station is fine)
  station_stale  - the relay works but the newest station reading is old
                   (the weather station itself has stopped reporting)
  ok             - end-to-end healthy

Alert policy: email on every state CHANGE, reminder every 6 h while broken.
State lives in the Supabase monitor_state table (key 'wx_path'), not git, so
hourly runs never race the leaderboard commits.

Env: RESEND_API_KEY (for the alert email). All other credentials are the same
already-public ones the dashboard pages embed.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mailer import send_html  # noqa: E402

SUPABASE_URL = "https://sduzxijjvpbfgvlwcwpp.supabase.co"
SUPABASE_KEY = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNkdXp4aWpqdnBiZmd2bHdjd3"
                "BwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY1ODE2NzgsImV4cCI6MjA5MjE1NzY3OH0."
                "fbYf9-F987DUSlsibuGnqGYEQe6tsQsOf7NMmNMrBT8")
WX_PROXY = f"{SUPABASE_URL}/functions/v1/wx-proxy"

# Same public credentials the dashboard pages embed (see index.html)
WL_KEY    = "kvsweiywmnahb6ayvc7gstbdigst1k9x"
WL_SECRET = "urw4q7amnhwnajydf3r1ubggcrvcicvh"
STATION   = 243271

STALE_MIN       = 45    # newest reading older than this = station not reporting
REMIND_EVERY_H  = 6     # nag interval while broken
STATE_KEY       = "wx_path"

ADMIN_FALLBACK  = ["andrew@bidgeepumps.com.au"]


def _req(url, headers=None, method="GET", data=None, timeout=25):
    req = urllib.request.Request(url, headers=headers or {}, method=method,
                                 data=data.encode() if isinstance(data, str) else data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "ignore")


def check_path():
    """Return (status, detail): status in ok / relay_down / station_stale."""
    url = f"{WX_PROXY}?endpoint=current/{STATION}&api-key={WL_KEY}"
    try:
        code, body = _req(url, headers={
            "Authorization": f"Bearer {SUPABASE_KEY}", "apikey": SUPABASE_KEY,
            "X-Api-Secret": WL_SECRET})
    except Exception as e:
        return "relay_down", f"relay request failed: {e}"
    if code != 200:
        return "relay_down", f"relay returned HTTP {code}: {body[:150]}"
    try:
        d = json.loads(body)
        newest = max(x["ts"] for s in d["sensors"] for x in s.get("data", []) if "ts" in x)
    except Exception as e:
        return "relay_down", f"relay returned unparseable data: {e} :: {body[:150]}"
    age_min = (datetime.now(timezone.utc).timestamp() - newest) / 60
    if age_min > STALE_MIN:
        return "station_stale", (f"relay OK but the newest station reading is "
                                 f"{age_min:.0f} min old (limit {STALE_MIN})")
    return "ok", f"latest reading {age_min:.0f} min ago"


def load_state():
    try:
        code, body = _req(
            f"{SUPABASE_URL}/rest/v1/monitor_state?key=eq.{STATE_KEY}&select=value",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"})
        rows = json.loads(body)
        return rows[0]["value"] if rows else {}
    except Exception as e:
        print(f"state load failed ({e}) - treating as empty")
        return {}


def save_state(value):
    try:
        payload = json.dumps({"key": STATE_KEY, "value": value,
                              "updated_at": datetime.now(timezone.utc).isoformat()})
        _req(f"{SUPABASE_URL}/rest/v1/monitor_state?on_conflict=key",
             headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
                      "Content-Type": "application/json",
                      "Prefer": "resolution=merge-duplicates,return=minimal"},
             method="POST", data=payload)
    except Exception as e:
        print(f"state save failed: {e}")


def admin_emails():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "data", "admin_users.json"), encoding="utf-8") as f:
            emails = [u["email"] for u in json.load(f) if u.get("email")]
            if emails:
                return emails
    except Exception:
        pass
    return ADMIN_FALLBACK


def alert(subject, body_html):
    ok, detail = send_html(subject, body_html, admin_emails(), stream="watchdog")
    print(f"email: {detail}")
    return ok


def main():
    status, detail = check_path()
    now = datetime.now(timezone.utc)
    print(f"wx-path check: {status} - {detail}")

    state = load_state()
    prev = state.get("status", "ok")
    last_alert = state.get("last_alert_at")
    since = state.get("since")

    MSG = {
        "relay_down": (
            "LIVE WEATHER FEED DOWN - greenkeeper/lake pages showing --",
            "<p>The browser data path (Supabase wx-proxy relay -&gt; WeatherLink) is "
            "failing, so the greenkeeper and Lake Albert pages will show <b>--</b> for "
            "all live figures.</p><p><b>Detail:</b> {detail}</p>"
            "<p><b>Where to look:</b> Supabase dashboard &gt; Edge Functions &gt; wx-proxy "
            "(logs), then the admin page's Live Weather Path card. Emails and data "
            "logging are unaffected - this is the page-display path only.</p>"),
        "station_stale": (
            "WEATHER STATION NOT REPORTING - readings are stale",
            "<p>The relay is healthy but the club weather station's newest reading is "
            "old - the station itself appears to have stopped reporting.</p>"
            "<p><b>Detail:</b> {detail}</p>"
            "<p><b>Where to look:</b> WeatherLink Live console power/WiFi at the club, "
            "then weatherlink.com for station 243271.</p>"),
    }

    if status == "ok":
        if prev != "ok":
            alert("Live weather feed RESTORED",
                  f"<p>The live weather path is healthy again ({detail}). "
                  f"Broken since {since or 'unknown'} (UTC). The greenkeeper and lake "
                  f"pages will show live figures on their next refresh.</p>")
        save_state({"status": "ok", "since": now.isoformat(),
                    "detail": detail, "last_alert_at": None,
                    "checked_at": now.isoformat()})
        return 0

    subject, body = MSG[status]
    body = body.format(detail=detail)
    if prev != status:
        send_it = True                      # state changed - always tell
        since = now.isoformat()
    else:
        hours = 999.0
        if last_alert:
            try:
                hours = (now - datetime.fromisoformat(last_alert)).total_seconds() / 3600
            except Exception:
                pass
        send_it = hours >= REMIND_EVERY_H   # still broken - nag every 6 h
        subject = f"STILL BROKEN: {subject}"

    if send_it:
        if alert(subject, body):
            last_alert = now.isoformat()
    save_state({"status": status, "since": since, "detail": detail,
                "last_alert_at": last_alert, "checked_at": now.isoformat()})
    # exit 0 either way: the email IS the alarm; a red workflow run here would
    # just generate a second, less useful GitHub notification
    return 0


if __name__ == "__main__":
    sys.exit(main())
