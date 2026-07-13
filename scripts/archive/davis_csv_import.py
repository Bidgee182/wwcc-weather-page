#!/usr/bin/env python3
"""
Davis WeatherLink CSV Import
============================
Aggregates 5-minute interval Bidgee CSV exports into daily summaries
and merges them into data/davis_weather_history.json.

Run once locally:
    python scripts/davis_csv_import.py

The CSVs have 5 header rows before the column row (row 6), then data from row 7.
Date format: D/M/YY H:MM AM/PM  (Australian)
"""

import csv, json, glob
from pathlib import Path
from datetime import datetime

DATA_DIR     = Path(__file__).parent.parent / 'data'
HISTORY_JSON = DATA_DIR / 'davis_weather_history.json'
CSV_GLOB     = str(Path(__file__).parent.parent / 'Bidgee_Pumps*.csv')

def parse_dt(s):
    s = s.strip().strip('"')
    for fmt in ('%d/%m/%y %I:%M %p', '%d/%m/%Y %I:%M %p'):
        try: return datetime.strptime(s, fmt)
        except: pass
    return None

def import_csvs():
    files = sorted(glob.glob(CSV_GLOB))
    if not files:
        print('No Bidgee_Pumps*.csv files found in repo root')
        return

    # day_data[date_str] -> aggregation buckets
    day_data = {}

    for fpath in files:
        print(f'Reading {Path(fpath).name[:60]}...')
        with open(fpath, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()

        # Row 6 (index 5) is the header row
        if len(lines) < 7:
            print(f'  Skipping — too few lines')
            continue

        header = next(csv.reader([lines[5]]))
        # Normalise header names
        col = {h.strip().strip('"'): i for i, h in enumerate(header)}

        def ci(name):
            return col.get(name)

        rows_read = 0
        for line in lines[6:]:
            line = line.strip()
            if not line or line == '""':
                continue
            parts = next(csv.reader([line]))

            dt_raw = parts[0].strip().strip('"') if parts else ''
            dt = parse_dt(dt_raw)
            if dt is None:
                continue

            day = dt.strftime('%Y-%m-%d')
            if day not in day_data:
                day_data[day] = {
                    't_max': -999.0, 't_min': 999.0, 'has_temp': False,
                    'rain': 0.0,
                    'hum_sum': 0.0, 'hum_count': 0,
                    'wind_max': 0.0,
                    'bar_sum': 0.0, 'bar_count': 0,
                }
            b = day_data[day]

            def get(col_name):
                idx = ci(col_name)
                if idx is None or idx >= len(parts):
                    return None
                v = parts[idx].strip().strip('"')
                try: return float(v) if v else None
                except ValueError: return None

            # Temperature — use outdoor "Temp - °C", High Temp, Low Temp
            t    = get('Temp - \xb0C') or get('Temp - °C')
            t_hi = get('High Temp - \xb0C') or get('High Temp - °C')
            t_lo = get('Low Temp - \xb0C') or get('Low Temp - °C')
            for tv in [t, t_hi, t_lo]:
                if tv is not None:
                    if tv > b['t_max']: b['t_max'] = tv
                    if tv < b['t_min']: b['t_min'] = tv
                    b['has_temp'] = True

            # Rain
            rn = get('Rain - mm')
            if rn is not None:
                b['rain'] += rn

            # Humidity
            hm = get('Hum - %')
            if hm is not None:
                b['hum_sum'] += hm; b['hum_count'] += 1

            # Wind max
            wm = get('High Wind Speed - km/h') or get('Wind Speed - km/h')
            if wm is not None and wm > b['wind_max']:
                b['wind_max'] = wm

            # Pressure
            bp = get('Barometer - hPa')
            if bp is not None and 850 < bp < 1100:
                b['bar_sum'] += bp; b['bar_count'] += 1

            rows_read += 1

        print(f'  {rows_read:,} rows, up to {max(day_data)}')

    if not day_data:
        print('No data extracted.')
        return

    # Convert buckets to summaries
    new_records = {}
    for day, b in day_data.items():
        new_records[day] = {
            'date':     day,
            'tMax':     round(b['t_max'], 1) if b['has_temp'] else None,
            'tMin':     round(b['t_min'], 1) if b['has_temp'] else None,
            'rain':     round(b['rain'],  1),
            'humidity': round(b['hum_sum'] / b['hum_count'], 0) if b['hum_count'] > 0 else None,
            'windMax':  round(b['wind_max'], 1) if b['wind_max'] > 0 else None,
            'pressAvg': round(b['bar_sum']  / b['bar_count'], 1) if b['bar_count'] > 0 else None,
        }

    print(f'\nCSV data spans: {min(new_records)} to {max(new_records)} ({len(new_records)} days)')

    # Load existing JSON and merge (CSV wins for older dates, existing wins for recent)
    existing = {}
    if HISTORY_JSON.exists():
        try:
            for r in json.loads(HISTORY_JSON.read_text()):
                existing[r['date']] = r
            print(f'Existing JSON: {min(existing)} to {max(existing)} ({len(existing)} records)')
        except Exception as e:
            print(f'Could not load existing JSON: {e}')

    # Merge: use CSV data as base, overlay existing where it has richer data
    merged = dict(new_records)
    for day, rec in existing.items():
        if day in merged:
            # Keep existing pressAvg from daily_log.csv if it's there (more accurate daily avg)
            if rec.get('pressAvg') is not None:
                merged[day]['pressAvg'] = rec['pressAvg']
        else:
            merged[day] = rec  # keep existing records not in CSV range

    records = sorted(merged.values(), key=lambda r: r['date'])
    HISTORY_JSON.write_text(json.dumps(records, indent=2))
    print(f'\nWrote {len(records)} records to {HISTORY_JSON}')
    print(f'Range: {records[0]["date"]} to {records[-1]["date"]}')

if __name__ == '__main__':
    import_csvs()
