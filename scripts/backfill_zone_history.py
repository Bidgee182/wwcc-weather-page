#!/usr/bin/env python3
"""
backfill_zone_history.py — One-time backfill of lake_zone_history.csv
from actual farmbot_lake_readings.json AHD measurements.

Creates entries only for real zone transitions detected in the sensor data.
Events are marked 'backfilled' so they are distinguishable from live alerts.
No email was sent for any of these (historical data).

Run once from repo root:
    python scripts/backfill_zone_history.py
"""
import csv, json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA_DIR = Path('data')
HISTORY_CSV = DATA_DIR / 'lake_zone_history.csv'
HEADERS = [
    'timestamp_aest', 'old_zone', 'new_zone', 'old_rate', 'new_rate',
    'ahd', 'event', 'email_sent', 'recipients',
]

cfg   = json.loads((DATA_DIR / 'lake_config.json').read_text(encoding='utf-8'))
zones = cfg['zone_thresholds']


def get_zone(ahd):
    for z in zones:
        if z['min_ahd'] is None or ahd >= z['min_ahd']:
            return z
    return zones[-1]


def zone_rate_str(z):
    return f"{z['max_pump_ml_day']} ML/day"


# ── Load and group readings by day ────────────────────────────────────────────
readings = json.loads((DATA_DIR / 'farmbot_lake_readings.json').read_text(encoding='utf-8'))
by_day = defaultdict(list)
for r in readings:
    if r.get('date') and r.get('ahd') is not None:
        by_day[r['date'][:10]].append(float(r['ahd']))

# Daily averages, sorted
daily = {}
for day in sorted(by_day):
    vals = by_day[day]
    daily[day] = sum(vals) / len(vals)

# ── Check for existing entries (don't double-backfill) ────────────────────────
existing_backfill_dates = set()
if HISTORY_CSV.exists():
    with HISTORY_CSV.open(newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row.get('event') == 'backfilled':
                existing_backfill_dates.add(row['timestamp_aest'][:10])

# ── Determine zone transitions ────────────────────────────────────────────────
new_rows = []
prev_zone = None

for day, avg_ahd in daily.items():
    if day in existing_backfill_dates:
        print(f'  Skipping {day} — already backfilled')
        prev_zone = get_zone(avg_ahd)
        continue

    zone = get_zone(avg_ahd)
    ts   = f'{day} 12:00:00'   # midday AEST as representative time

    if prev_zone is None:
        # First ever reading — initialise
        new_rows.append({
            'timestamp_aest': ts,
            'old_zone':       '',
            'new_zone':       zone['zone'],
            'old_rate':       '',
            'new_rate':       zone_rate_str(zone),
            'ahd':            f'{avg_ahd:.3f}',
            'event':          'backfilled',
            'email_sent':     'false',
            'recipients':     '',
        })
        print(f'  {day}: initialised Zone {zone["zone"]} ({avg_ahd:.3f} m AHD)')

    elif zone['zone'] != prev_zone['zone']:
        # Zone transition
        new_rows.append({
            'timestamp_aest': ts,
            'old_zone':       prev_zone['zone'],
            'new_zone':       zone['zone'],
            'old_rate':       zone_rate_str(prev_zone),
            'new_rate':       zone_rate_str(zone),
            'ahd':            f'{avg_ahd:.3f}',
            'event':          'backfilled',
            'email_sent':     'false',
            'recipients':     '',
        })
        print(f'  {day}: Zone {prev_zone["zone"]} -> Zone {zone["zone"]} ({avg_ahd:.3f} m AHD)')

    else:
        print(f'  {day}: Zone {zone["zone"]} unchanged ({avg_ahd:.3f} m AHD) — no entry')

    prev_zone = zone

if not new_rows:
    print('No new rows to write.')
else:
    # Prepend new rows before any existing rows
    existing_rows = []
    if HISTORY_CSV.exists() and HISTORY_CSV.stat().st_size > 0:
        with HISTORY_CSV.open(newline='', encoding='utf-8') as f:
            existing_rows = list(csv.DictReader(f))

    all_rows = new_rows + existing_rows
    with HISTORY_CSV.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        w.writeheader()
        w.writerows(all_rows)

    print(f'\nWrote {len(new_rows)} new row(s) to {HISTORY_CSV}  ({len(all_rows)} total)')
