#!/usr/bin/env python3
"""
Davis WeatherLink — 12-month history backfill
=============================================
Converts the existing daily_log.csv (already written by the greenkeeper page
pipeline) into the davis_weather_history.json format used by lake-albert.html.
Then optionally tops up with the Davis v2 API for any days missing from the CSV.

Run from repo root:
    python scripts/davis_history_backfill.py

The API is only called for days not already present in the CSV (typically just
today). Re-running is safe — existing records are preserved.
"""

import csv
import json
import time
import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
log = logging.getLogger(__name__)

# ── Paths & credentials ───────────────────────────────────────────────────
REPO_ROOT  = Path(__file__).parent.parent
DATA_DIR   = REPO_ROOT / 'data'
CSV_FILE   = DATA_DIR  / 'daily_log.csv'
DATA_FILE  = DATA_DIR  / 'davis_weather_history.json'

V2_API_KEY    = 'kvsweiywmnahb6ayvc7gstbdigst1k9x'
V2_API_SECRET = 'urw4q7amnhwnajydf3r1ubggcrvcicvh'
V2_STATION_ID = 10489
V2_BASE       = 'https://api.weatherlink.com/v2'

SYDNEY_TZ = ZoneInfo('Australia/Sydney')


# ── CSV → dict ────────────────────────────────────────────────────────────
def load_csv():
    """Read daily_log.csv and return a dict keyed by 'YYYY-MM-DD'."""
    if not CSV_FILE.exists():
        log.warning(f'CSV not found at {CSV_FILE}')
        return {}
    records = {}
    with open(CSV_FILE, newline='') as f:
        for row in csv.DictReader(f):
            d = row.get('date', '').strip()
            if not d:
                continue
            def flt(key, default=None):
                v = row.get(key, '').strip()
                try: return round(float(v), 1)
                except (ValueError, TypeError): return default
            records[d] = {
                'date':     d,
                'tMax':     flt('temp_max'),
                'tMin':     flt('temp_min'),
                'rain':     flt('rain_mm', 0.0),
                'humidity': flt('rh_mean'),
                'windMax':  flt('wind_max_kmh'),
            }
    log.info(f'Loaded {len(records)} days from {CSV_FILE.name}')
    return records


# ── Davis v2 API (single day) ─────────────────────────────────────────────
def fetch_day_api(day_str: str):
    """Fetch a single calendar day from Davis v2 (max 86400 s window)."""
    day   = datetime.strptime(day_str, '%Y-%m-%d').date()
    start = datetime(day.year, day.month, day.day,  0,  0,  0, tzinfo=SYDNEY_TZ)
    end   = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=SYDNEY_TZ)
    params = {
        'start-timestamp': int(start.timestamp()),
        'end-timestamp':   int(end.timestamp()),
        'api-key': V2_API_KEY,
    }
    resp = requests.get(f'{V2_BASE}/historic/{V2_STATION_ID}', params=params,
                        headers={'X-Api-Secret': V2_API_SECRET}, timeout=60)
    resp.raise_for_status()
    return resp.json()


def process_day_api(data, target_date: str):
    """Aggregate 5-min sensor records for target_date into a daily summary."""
    acc = dict(tMax=-999.0, tMin=999.0, rain=0.0, humSum=0.0, humCount=0,
               windMax=0.0, hasTemp=False)
    for sensor in (data.get('sensors') or []):
        for r in (sensor.get('data') or []):
            ts = r.get('ts') or r.get('timestamp')
            if not ts:
                continue
            if datetime.fromtimestamp(ts, tz=SYDNEY_TZ).strftime('%Y-%m-%d') != target_date:
                continue
            hi_f = r.get('temp_out_hi') or r.get('temp_out')
            lo_f = r.get('temp_out_lo') or r.get('temp_out')
            if hi_f is not None:
                hi_c = (float(hi_f) - 32.0) / 1.8
                if hi_c > acc['tMax']: acc['tMax'] = hi_c
                acc['hasTemp'] = True
            if lo_f is not None:
                lo_c = (float(lo_f) - 32.0) / 1.8
                if lo_c < acc['tMin']: acc['tMin'] = lo_c
            rain = r.get('rainfall_mm')
            if rain is not None: acc['rain'] += float(rain)
            hum = r.get('hum_out') or r.get('hum')
            if hum is not None:
                acc['humSum'] += float(hum); acc['humCount'] += 1
            wind = r.get('wind_speed_hi') or r.get('wind_speed_avg')
            if wind is not None:
                kph = float(wind) * 1.60934
                if kph > acc['windMax']: acc['windMax'] = kph
    return {
        'date':     target_date,
        'tMax':     round(acc['tMax'], 1) if acc['hasTemp'] else None,
        'tMin':     round(acc['tMin'], 1) if acc['hasTemp'] else None,
        'rain':     round(acc['rain'], 1),
        'humidity': round(acc['humSum'] / acc['humCount'], 0) if acc['humCount'] > 0 else None,
        'windMax':  round(acc['windMax'], 1) if acc['windMax'] > 0 else None,
    }


# ── Main ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--full', action='store_true',
                        help='Re-build entirely from CSV + API (ignores existing JSON)')
    args = parser.parse_args()

    now_sydney = datetime.now(tz=SYDNEY_TZ)
    today_str  = now_sydney.strftime('%Y-%m-%d')
    cutoff_str = (now_sydney - timedelta(days=365)).strftime('%Y-%m-%d')

    # Step 1 — load from CSV (this is the bulk of the data)
    csv_days = load_csv()

    # Step 2 — merge with existing JSON (keeps any API-fetched days not in CSV)
    all_days: dict[str, dict] = {}
    if not args.full and DATA_FILE.exists():
        existing = json.loads(DATA_FILE.read_text())
        all_days = {r['date']: r for r in existing}
        log.info(f'Loaded {len(all_days)} existing JSON records')

    all_days.update(csv_days)  # CSV takes precedence

    # Step 3 — identify days still missing (not in CSV and not in JSON)
    d = datetime.strptime(cutoff_str, '%Y-%m-%d')
    missing = []
    while d.strftime('%Y-%m-%d') <= today_str:
        ds = d.strftime('%Y-%m-%d')
        if ds not in all_days:
            missing.append(ds)
        d += timedelta(days=1)

    # Always refresh today via API (CSV may not have it yet)
    if today_str not in missing:
        missing.append(today_str)

    if missing:
        log.info(f'Fetching {len(missing)} day(s) from Davis API...')
        for day_str in missing:
            try:
                data = fetch_day_api(day_str)
                rec  = process_day_api(data, day_str)
                all_days[day_str] = rec
                log.info(f'  {day_str} ✓  tMax={rec["tMax"]}°C  rain={rec["rain"]}mm  wind={rec["windMax"]}km/h')
            except Exception as e:
                log.error(f'  {day_str} ✗  {e}')
            time.sleep(0.25)

    # Step 4 — drop records outside 13-month window and save
    all_days = {k: v for k, v in all_days.items() if k >= cutoff_str}
    records  = sorted(all_days.values(), key=lambda r: r['date'])
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(records, indent=2))
    log.info(f'✓ Saved {len(records)} daily records → {DATA_FILE}')


if __name__ == '__main__':
    main()
