#!/usr/bin/env python3
"""
farmbot_csv_import.py
=====================
One-off script: reads the exported FarmBot CSV and writes data/farmbot_history.json
with the same daily-summary structure as farmbot_poll.py backfill.

Usage:
    python scripts/farmbot_csv_import.py

CSV columns (dates in Australia/Sydney local time):
    Date and Time, Level (cm), Volume (L)
"""

import csv, json, sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TOTAL_HEIGHT   = 170      # cm — from FarmBot sensor config
TANK_CAPACITY  = 250000   # litres

CSV_PATH      = Path('0-4295727 Water Tank - 20260101-20260630.csv')
HISTORY_JSON  = Path('data/farmbot_history.json')
READINGS_JSON = Path('data/farmbot_readings.json')

SYDNEY_TZ = ZoneInfo('Australia/Sydney')

def calc_pct(level_cm):
    if level_cm is None:
        return None
    return round(min(100.0, max(0.0, (level_cm / TOTAL_HEIGHT) * 100)), 1)

def calc_volume(pct):
    if pct is None:
        return None
    return round((pct / 100) * TANK_CAPACITY)

if not CSV_PATH.exists():
    print(f'ERROR: {CSV_PATH} not found. Run from repo root.', file=sys.stderr)
    sys.exit(1)

# Group readings by Sydney date
by_date = {}
with open(CSV_PATH, newline='', encoding='utf-8-sig') as f:
    reader = csv.DictReader(f)
    for row in reader:
        dt_str   = row.get('Date and Time', '').strip()
        level_s  = row.get('Level (cm)', '').strip()
        if not dt_str or not level_s:
            continue
        try:
            # CSV is already in Sydney local time
            dt_local = datetime.strptime(dt_str, '%Y-%m-%d %H:%M')
            level_cm = float(level_s)
        except (ValueError, TypeError):
            continue

        d = dt_local.date().isoformat()
        by_date.setdefault(d, []).append((dt_local, level_cm))

history = []
for d in sorted(by_date.keys()):
    day = sorted(by_date[d], key=lambda x: x[0])
    morning_pct = calc_pct(day[0][1])
    evening_pct = calc_pct(day[-1][1])
    all_pcts    = [calc_pct(r[1]) for r in day]
    all_pcts    = [p for p in all_pcts if p is not None]
    used_pct    = max(0.0, (morning_pct or 0) - (evening_pct or 0))
    used_l      = round(used_pct / 100 * TANK_CAPACITY)
    refill      = (evening_pct or 0) > (morning_pct or 0) + 2

    history.append({
        'date':        d,
        'morning_pct': morning_pct,
        'evening_pct': evening_pct,
        'min_pct':     round(min(all_pcts), 1) if all_pcts else None,
        'max_pct':     round(max(all_pcts), 1) if all_pcts else None,
        'used_l':      used_l,
        'used_pct':    round(used_pct, 1),
        'refill':      refill,
        'readings':    len(day),
    })

HISTORY_JSON.parent.mkdir(parents=True, exist_ok=True)
HISTORY_JSON.write_text(json.dumps(history, indent=2))
print(f'Wrote {len(history)} daily records to {HISTORY_JSON}')

# ── All individual readings → farmbot_readings.json ──────────────────
readings = []
for d in sorted(by_date.keys()):
    for dt_local, level_cm in sorted(by_date[d], key=lambda x: x[0]):
        pct = calc_pct(level_cm)
        vol = calc_volume(pct)
        if pct is None:
            continue
        # Convert Sydney local time → UTC
        dt_aware = dt_local.replace(tzinfo=SYDNEY_TZ)
        dt_utc   = dt_aware.astimezone(timezone.utc)
        readings.append({
            'date':     dt_utc.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'pct':      pct,
            'volume_l': vol,
        })

READINGS_JSON.write_text(json.dumps(readings, indent=2))
print(f'Wrote {len(readings)} individual readings to {READINGS_JSON}')
