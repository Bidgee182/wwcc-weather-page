#!/usr/bin/env python3
"""
Davis WeatherLink v2 — Full History Backfill
=============================================
Fetches daily summaries from the Davis API back to START_DATE and merges
them into data/davis_weather_history.json.

Run once locally:
    python scripts/davis_backfill.py

Fetches in 7-day chunks to stay within API rate limits.
Skips dates already in the JSON. Safe to re-run if interrupted.
"""

import json, time, logging
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
DAVIS_V2_API_KEY    = 'kvsweiywmnahb6ayvc7gstbdigst1k9x'
DAVIS_V2_API_SECRET = 'urw4q7amnhwnajydf3r1ubggcrvcicvh'
DAVIS_V2_STATION_ID = 10489
DAVIS_V2_BASE       = 'https://api.weatherlink.com/v2'

DATA_DIR            = Path(__file__).parent.parent / 'data'
HISTORY_JSON        = DATA_DIR / 'davis_weather_history.json'

SYDNEY_TZ           = ZoneInfo('Australia/Sydney')
START_DATE          = datetime(2014, 1, 1, tzinfo=SYDNEY_TZ)
CHUNK_DAYS          = 7       # fetch this many days per API call
SLEEP_BETWEEN       = 0.75    # seconds between API calls (rate limit safety)

# ── API ───────────────────────────────────────────────────────────────────────
def davis_get(endpoint, params):
    p = dict(params)
    p['api-key'] = DAVIS_V2_API_KEY
    r = requests.get(f'{DAVIS_V2_BASE}/{endpoint}', params=p,
                     headers={'X-Api-Secret': DAVIS_V2_API_SECRET}, timeout=60)
    r.raise_for_status()
    return r.json()

# ── Process ───────────────────────────────────────────────────────────────────
def process_sensors(sensors):
    """Aggregate a list of sensor records into per-day summaries {date_str: summary}."""
    day_data = {}  # date_str -> aggregated buckets

    for sensor in sensors:
        for r in (sensor.get('data') or []):
            # Determine Sydney date from record timestamp
            ts = r.get('ts')
            if ts is None:
                continue
            dt = datetime.fromtimestamp(int(ts), tz=SYDNEY_TZ)
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

            # Temperature (°F → °C)
            t_f = r.get('temp') or r.get('temp_out')
            if t_f is not None:
                t_c = (float(t_f) - 32.0) / 1.8
                if t_c > b['t_max']: b['t_max'] = t_c
                if t_c < b['t_min']: b['t_min'] = t_c
                b['has_temp'] = True

            # Rain
            rn = r.get('rainfall_mm')
            if rn is not None:
                b['rain'] += float(rn)

            # Humidity
            hm = r.get('hum') or r.get('hum_out')
            if hm is not None:
                b['hum_sum'] += float(hm); b['hum_count'] += 1

            # Wind
            wm = (r.get('wind_speed_last') or r.get('wind_speed_avg_last_1_min')
                  or r.get('wind_speed_hi_last_2_min') or r.get('wind_speed_avg_last_10_min'))
            if wm is not None:
                kph = float(wm) * 1.60934
                if kph > b['wind_max']: b['wind_max'] = kph

            # Pressure (inHg → hPa if <200, else already mb)
            bp = (r.get('bar_sea_level_in') or r.get('bar_sea_level')
                  or r.get('bar_hi_in') or r.get('bar'))
            if bp is not None:
                bp_f = float(bp)
                hpa = bp_f * 33.8639 if bp_f < 200 else bp_f
                if 850 < hpa < 1100:   # sanity check
                    b['bar_sum'] += hpa; b['bar_count'] += 1

    # Convert buckets to summaries
    summaries = {}
    for day, b in day_data.items():
        summaries[day] = {
            'date':     day,
            'tMax':     round(b['t_max'], 1) if b['has_temp'] else None,
            'tMin':     round(b['t_min'], 1) if b['has_temp'] else None,
            'rain':     round(b['rain'],  1),
            'humidity': round(b['hum_sum'] / b['hum_count'], 0) if b['hum_count'] > 0 else None,
            'windMax':  round(b['wind_max'], 1) if b['wind_max'] > 0 else None,
            'pressAvg': round(b['bar_sum']  / b['bar_count'], 1) if b['bar_count'] > 0 else None,
        }
    return summaries

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    # Load existing history
    all_days = {}
    if HISTORY_JSON.exists():
        try:
            existing = json.loads(HISTORY_JSON.read_text())
            all_days = {r['date']: r for r in existing}
            log.info(f'Loaded {len(all_days)} existing records ({min(all_days)} – {max(all_days)})')
        except Exception as e:
            log.warning(f'Could not load existing history: {e}')

    # Determine fetch range: START_DATE up to yesterday (today's data comes from regular poll)
    now_syd   = datetime.now(tz=SYDNEY_TZ)
    end_date  = (now_syd - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    cursor    = START_DATE

    chunks_total  = 0
    chunks_done   = 0
    days_added    = 0
    days_updated  = 0

    # Count chunks for progress reporting
    d = cursor
    while d < end_date:
        chunks_total += 1
        d += timedelta(days=CHUNK_DAYS)

    log.info(f'Fetching {cursor.strftime("%Y-%m-%d")} → {end_date.strftime("%Y-%m-%d")} '
             f'in {CHUNK_DAYS}-day chunks ({chunks_total} calls)')

    while cursor < end_date:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS - 1), end_date)
        chunk_end_eod = chunk_end.replace(hour=23, minute=59, second=59)

        start_ts = int(cursor.timestamp())
        end_ts   = int(chunk_end_eod.timestamp())

        # Check if all days in this chunk are already cached with full data
        chunk_dates = []
        d = cursor
        while d <= chunk_end:
            chunk_dates.append(d.strftime('%Y-%m-%d'))
            d += timedelta(days=1)

        already_complete = all(
            day in all_days and all_days[day].get('tMax') is not None
            for day in chunk_dates
        )
        if already_complete:
            log.debug(f'Chunk {cursor.strftime("%Y-%m-%d")} – all days cached, skipping')
            cursor += timedelta(days=CHUNK_DAYS)
            chunks_done += 1
            continue

        try:
            data    = davis_get(f'historic/{DAVIS_V2_STATION_ID}',
                                {'start-timestamp': start_ts, 'end-timestamp': end_ts})
            sensors = data.get('sensors') or []

            if sensors:
                summaries = process_sensors(sensors)
                for day, summary in summaries.items():
                    if day in all_days:
                        days_updated += 1
                    else:
                        days_added += 1
                    all_days[day] = summary
            else:
                log.debug(f'No sensor data returned for {cursor.strftime("%Y-%m-%d")}')

        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                log.warning('Rate limited — sleeping 10s')
                time.sleep(10)
                continue   # retry same chunk
            elif e.response is not None and e.response.status_code in (400, 404):
                log.debug(f'No data available for {cursor.strftime("%Y-%m-%d")} ({e.response.status_code})')
            else:
                log.warning(f'HTTP error for {cursor.strftime("%Y-%m-%d")}: {e}')
        except Exception as e:
            log.warning(f'Error for {cursor.strftime("%Y-%m-%d")}: {e}')

        chunks_done += 1
        if chunks_done % 20 == 0 or chunks_done == chunks_total:
            pct = chunks_done / chunks_total * 100
            log.info(f'Progress: {chunks_done}/{chunks_total} chunks ({pct:.0f}%) '
                     f'— {days_added} added, {days_updated} updated')

            # Save incrementally every 20 chunks
            records = sorted(all_days.values(), key=lambda r: r['date'])
            HISTORY_JSON.write_text(json.dumps(records, indent=2))

        cursor += timedelta(days=CHUNK_DAYS)
        time.sleep(SLEEP_BETWEEN)

    # Final save
    records = sorted(all_days.values(), key=lambda r: r['date'])
    HISTORY_JSON.write_text(json.dumps(records, indent=2))
    log.info(f'Done. {len(records)} total records saved to {HISTORY_JSON}')
    log.info(f'Range: {records[0]["date"]} – {records[-1]["date"]}')
    log.info(f'{days_added} new days added, {days_updated} existing days updated')

if __name__ == '__main__':
    main()
