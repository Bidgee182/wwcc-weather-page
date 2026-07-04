#!/usr/bin/env python3
"""
Davis WeatherLink v2 — 12-month history backfill
=================================================
Fetches one calendar month at a time (12 API calls) and writes daily
summaries to data/davis_weather_history.json.

Run once from repo root:
    pip install requests
    python scripts/davis_history_backfill.py

Re-running is safe — existing days are preserved and only missing/current
month data is re-fetched unless you pass --full to force a complete refresh.
"""

import json
import time
import argparse
import logging
import calendar
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
log = logging.getLogger(__name__)

# ── Davis credentials ─────────────────────────────────────────────────────
V2_API_KEY    = 'kvsweiywmnahb6ayvc7gstbdigst1k9x'
V2_API_SECRET = 'urw4q7amnhwnajydf3r1ubggcrvcicvh'
V2_STATION_ID = 10489
V2_BASE       = 'https://api.weatherlink.com/v2'

SYDNEY_TZ = ZoneInfo('Australia/Sydney')
DATA_FILE = Path(__file__).parent.parent / 'data' / 'davis_weather_history.json'


# ── API ───────────────────────────────────────────────────────────────────
def v2_get(endpoint, params):
    params = dict(params)
    params['api-key'] = V2_API_KEY
    url  = f'{V2_BASE}/{endpoint}'
    resp = requests.get(url, params=params,
                        headers={'X-Api-Secret': V2_API_SECRET}, timeout=60)
    resp.raise_for_status()
    return resp.json()


def fetch_month(year, month):
    """Fetch a full calendar month from the Davis v2 historic endpoint."""
    last_day  = calendar.monthrange(year, month)[1]
    start     = datetime(year, month, 1,        0, 0, 0, tzinfo=SYDNEY_TZ)
    end       = datetime(year, month, last_day, 23, 59, 59, tzinfo=SYDNEY_TZ)
    start_ts  = int(start.timestamp())
    end_ts    = int(end.timestamp())
    log.info(f'  Fetching {year}-{month:02d}  ({start.date()} → {end.date()})')
    return v2_get(f'historic/{V2_STATION_ID}',
                  {'start-timestamp': start_ts, 'end-timestamp': end_ts})


# ── Processing ────────────────────────────────────────────────────────────
def process_response(data):
    """Aggregate 15-min sensor records into per-day summaries.
    Returns dict keyed by 'YYYY-MM-DD'."""
    day_data = {}

    for sensor in (data.get('sensors') or []):
        for r in (sensor.get('data') or []):
            ts = r.get('ts') or r.get('timestamp')
            if not ts:
                continue
            dt   = datetime.fromtimestamp(ts, tz=SYDNEY_TZ)
            date = dt.strftime('%Y-%m-%d')

            if date not in day_data:
                day_data[date] = {
                    'tMax':     -999.0,
                    'tMin':      999.0,
                    'rain':      0.0,
                    'humSum':    0.0,
                    'humCount':  0,
                    'windMax':   0.0,
                    'hasTemp':   False,
                }

            # Temperature °F → °C
            t_f = r.get('temp') or r.get('temp_out')
            if t_f is not None:
                t_c = (float(t_f) - 32.0) / 1.8
                d   = day_data[date]
                if t_c > d['tMax']: d['tMax'] = t_c
                if t_c < d['tMin']: d['tMin'] = t_c
                d['hasTemp'] = True

            # Rainfall mm
            rain = r.get('rainfall_mm')
            if rain is not None:
                day_data[date]['rain'] += float(rain)

            # Humidity %
            hum = r.get('hum') or r.get('hum_out')
            if hum is not None:
                day_data[date]['humSum']   += float(hum)
                day_data[date]['humCount'] += 1

            # Wind speed mph → km/h  (try several field names)
            wind_mph = (r.get('wind_speed_last')
                        or r.get('wind_speed_avg_last_1_min')
                        or r.get('wind_speed_hi_last_2_min')
                        or r.get('wind_speed_avg_last_10_min'))
            if wind_mph is not None:
                kph = float(wind_mph) * 1.60934
                if kph > day_data[date]['windMax']:
                    day_data[date]['windMax'] = kph

    # Build clean records
    result = {}
    for date, d in day_data.items():
        result[date] = {
            'tMax':     round(d['tMax'], 1) if d['hasTemp']      else None,
            'tMin':     round(d['tMin'], 1) if d['hasTemp']      else None,
            'rain':     round(d['rain'], 1),
            'humidity': round(d['humSum'] / d['humCount'], 0)
                        if d['humCount'] > 0 else None,
            'windMax':  round(d['windMax'], 1) if d['windMax'] > 0 else None,
        }
    return result


# ── Main ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--full', action='store_true',
                        help='Re-fetch all 12 months even if already cached')
    args = parser.parse_args()

    now = datetime.now(tz=SYDNEY_TZ)

    # Load existing data
    all_days = {}
    if DATA_FILE.exists():
        existing = json.loads(DATA_FILE.read_text())
        all_days = {r['date']: r for r in existing}
        log.info(f'Loaded {len(all_days)} existing records from {DATA_FILE.name}')
    else:
        log.info('No existing file — starting fresh')

    # Determine months to fetch
    months_to_fetch = []
    for i in range(12):
        m = now.month - i
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        month_key = f'{y}-{m:02d}'
        is_current = (y == now.year and m == now.month)
        # Always re-fetch current month; skip older months if data exists unless --full
        if is_current or args.full:
            months_to_fetch.append((y, m))
        else:
            # Check if we have any days from this month already
            has_data = any(d.startswith(month_key) for d in all_days)
            if not has_data:
                months_to_fetch.append((y, m))
            else:
                log.info(f'  Skipping {month_key} (already cached)')

    log.info(f'Fetching {len(months_to_fetch)} month(s)...')

    for year, month in months_to_fetch:
        try:
            data    = fetch_month(year, month)
            monthly = process_response(data)
            for date, rec in monthly.items():
                all_days[date] = {'date': date, **rec}
            log.info(f'    → {len(monthly)} days processed')
        except Exception as e:
            log.error(f'    ✗ {year}-{month:02d}: {e}')
        time.sleep(0.3)  # brief pause between API calls

    # Sort and save
    records = sorted(all_days.values(), key=lambda r: r['date'])
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(records, indent=2))
    log.info(f'✓ Saved {len(records)} daily records → {DATA_FILE}')


if __name__ == '__main__':
    main()
