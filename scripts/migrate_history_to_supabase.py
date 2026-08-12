#!/usr/bin/env python3
"""
One-time migration: import data/pump_station_history.json into Supabase pump_minute_stats.

The old GitHub Actions poller wrote 5-minute readings. This script maps each reading
to the pump_minute_stats schema so the pre-Pi history appears in the dashboard charts.

Run from the repo root:
    python scripts/migrate_history_to_supabase.py

Safe to run multiple times - Supabase upsert on ts handles duplicates.
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

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "../data/pump_station_history.json")


def arr(r, key, idx):
    lst = r.get(key) or []
    return lst[idx] if idx < len(lst) else None


def run_secs_from_speed(speed):
    """5-minute poll: if speed > 0 the pump was running; approximate as 300 seconds."""
    if speed is None:
        return None
    return 300 if speed > 0 else 0


def map_reading(r):
    sp = r.get("sp") or []
    pw = r.get("pw") or []
    rh = r.get("rh") or []
    st = r.get("st") or []
    tm = r.get("tm") or []

    p1_spd = arr(r, "sp", 0)
    p2_spd = arr(r, "sp", 1)
    p3_spd = arr(r, "sp", 2)
    jk_spd = arr(r, "sp", 3)

    return {
        "ts":               r["ts"],
        "pressure_avg":     r.get("pa"),
        "pressure_min":     r.get("pa"),   # point reading - min=avg=max
        "pressure_max":     r.get("pa"),
        "pressure_inlet":   r.get("pi"),
        "setpoint_bar":     r.get("ps"),
        "flow_avg":         r.get("fl"),
        "power_avg_kw":     r.get("pk"),
        "spec_energy_avg":  r.get("se"),
        "system_on":        (r.get("nr") or 0) > 0,
        "samples":          1,
        # Per-pump speed (%)
        "p1_speed_pct":     p1_spd,
        "p2_speed_pct":     p2_spd,
        "p3_speed_pct":     p3_spd,
        "jockey_speed_pct": jk_spd,
        # Per-pump power (kW)
        "p1_power_kw":      arr(r, "pw", 0),
        "p2_power_kw":      arr(r, "pw", 1),
        "p3_power_kw":      arr(r, "pw", 2),
        "jockey_power_kw":  arr(r, "pw", 3),
        # Per-pump cumulative run hours (as read from CU352)
        "p1_run_hours":     arr(r, "rh", 0),
        "p2_run_hours":     arr(r, "rh", 1),
        "p3_run_hours":     arr(r, "rh", 2),
        "jockey_run_hours": arr(r, "rh", 3),
        # Starts within the 5-minute window
        "p1_starts":        arr(r, "st", 0),
        "p2_starts":        arr(r, "st", 1),
        "p3_starts":        arr(r, "st", 2),
        "jockey_starts":    arr(r, "st", 3),
        # Motor temperatures (were None for old poller - no MGE scan)
        "p1_temp_c":        arr(r, "tm", 0),
        "p2_temp_c":        arr(r, "tm", 1),
        "p3_temp_c":        arr(r, "tm", 2),
        "jockey_temp_c":    arr(r, "tm", 3),
        # Estimated run_secs: if pump had speed > 0 assume running for full 5 min (300s)
        "p1_run_secs":      run_secs_from_speed(p1_spd),
        "p2_run_secs":      run_secs_from_speed(p2_spd),
        "p3_run_secs":      run_secs_from_speed(p3_spd),
        "jockey_run_secs":  run_secs_from_speed(jk_spd),
    }


def insert_batch(rows):
    url  = f"{SUPABASE_URL}/rest/v1/pump_minute_stats"
    data = json.dumps(rows).encode()
    req  = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type":  "application/json",
            "apikey":        SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Prefer":        "resolution=merge-duplicates,return=minimal",
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
        print(f"ERROR: not found: {path}")
        sys.exit(1)

    with open(path) as f:
        data = json.load(f)

    readings = data.get("readings", [])
    print(f"Loaded {len(readings)} readings")
    print(f"  First: {readings[0]['ts']}")
    print(f"  Last:  {readings[-1]['ts']}")

    rows = [map_reading(r) for r in readings]

    batch_size = 50
    total_ok   = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        ok, info = insert_batch(batch)
        if ok:
            total_ok += len(batch)
            print(f"  batch {i // batch_size + 1}: {len(batch)} rows OK")
        else:
            print(f"  batch {i // batch_size + 1}: FAILED - {info}")

    print(f"\nDone. {total_ok}/{len(rows)} rows inserted into pump_minute_stats.")


if __name__ == "__main__":
    main()
