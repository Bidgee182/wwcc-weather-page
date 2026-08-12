#!/usr/bin/env python3
"""
One-time migration: import data/pump_alarm_history.json into Supabase pump_events_hires.

Run from the repo root:
    python scripts/migrate_alarm_history.py

Safe to run more than once - duplicate timestamps are just re-inserted (no unique key conflict).
"""

import json
import os
import sys
import urllib.request
import urllib.error

SUPABASE_URL = "https://sduzxijjvpbfgvlwcwpp.supabase.co"
SUPABASE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNkdXp4aWpqdnBiZmd2bHdjd3BwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY1ODE2NzgsImV4cCI6MjA5MjE1NzY3OH0"
    ".fbYf9-F987DUSlsibuGnqGYEQe6tsQsOf7NMmNMrBT8"
)

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "../data/pump_alarm_history.json")

# Maps (type_name, source_name) -> event_type used in pump_events_hires
TYPE_MAP = {
    ("Alarm appeared",          "System"):          "alarm_on",
    ("Alarm cleared",           "System"):          "alarm_off",
    ("Alarm appeared",          "Pump"):            "pump_alarm_on",
    ("Alarm cleared",           "Pump"):            "pump_alarm_off",
    ("Alarm changed",           "Pump"):            "pump_alarm_on",
    ("Alarm appeared",          "CU352"):           "alarm_on",
    ("Alarm cleared",           "CU352"):           "alarm_off",
    ("Low-Flow Stop activated",   "SystemFunction"): "sysactive_on",
    ("Low-Flow Stop deactivated", "SystemFunction"): "sysactive_off",
    ("Power cycle",             "System"):          "power_cycle",
}


def map_event(ev):
    type_name   = ev.get("type_name", "")
    source_name = ev.get("source_name", "")
    code        = ev.get("code", 0) or 0

    key = (type_name, source_name)
    event_type = TYPE_MAP.get(key)
    if not event_type:
        # Generic fallback
        low = type_name.lower()
        if "appeared" in low or "activated" in low:
            event_type = "alarm_on"
        elif "cleared" in low or "deactivated" in low:
            event_type = "alarm_off"
        else:
            event_type = type_name.lower().replace(" ", "_")

    details = {
        "source_name": source_name,
        "type_code":   ev.get("type_code"),
        "type_name":   type_name,
        "migrated":    True,
    }
    if ev.get("imported"):
        details["originally_imported"] = True

    return {
        "ts":         ev["timestamp_iso"],
        "event_type": event_type,
        "pump_label": ev.get("pump_label"),
        # power_cycle events have code=0 - don't store as alarm_code
        "alarm_code": None if event_type in ("power_cycle",) else (code or None),
        "alarm_desc": ev.get("description") or None,
        "details":    json.dumps(details),
    }


def insert_batch(rows):
    url  = f"{SUPABASE_URL}/rest/v1/pump_events_hires"
    data = json.dumps(rows).encode()
    req  = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type":  "application/json",
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Prefer":        "return=minimal",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:400]
        return False, f"HTTP {e.code}: {body}"
    except Exception as e:
        return False, str(e)


def main():
    path = os.path.abspath(HISTORY_FILE)
    if not os.path.exists(path):
        print(f"ERROR: file not found: {path}")
        sys.exit(1)

    with open(path) as f:
        data = json.load(f)

    events = data.get("events", [])
    print(f"Loaded {len(events)} events from {path}")

    rows = [map_event(ev) for ev in events]

    # Show a preview
    for r in rows[:3]:
        print(f"  {r['ts']}  {r['event_type']:20s}  {r['pump_label']}  code={r['alarm_code']}")
    if len(rows) > 3:
        print(f"  ... and {len(rows)-3} more")

    print(f"\nInserting into Supabase pump_events_hires ...")
    batch_size = 50
    total_ok   = 0
    for i in range(0, len(rows), batch_size):
        batch   = rows[i:i + batch_size]
        ok, info = insert_batch(batch)
        if ok:
            total_ok += len(batch)
            print(f"  batch {i // batch_size + 1}: {len(batch)} rows OK")
        else:
            print(f"  batch {i // batch_size + 1}: FAILED - {info}")

    print(f"\nDone. {total_ok}/{len(rows)} rows inserted.")


if __name__ == "__main__":
    main()
