#!/usr/bin/env python3
"""
backfill_history.py
=============================================================
One-off script.  Run once from the repo root:
    python scripts/backfill_history.py

What it does
------------
1. Reads all Davis WeatherLink 5-minute export CSVs (Bidgee_Pumps*.csv)
   and builds complete daily weather rows back to August 2015.
2. For existing CSV rows (Sep 2025+): backfills only the missing
   temp_max_time / temp_min_time fields from the export CSVs.
3. Makes 5 Davis API calls for Jul 5-9 2026 (those dates fall after
   the last export file ends on Jul 4 2026).
4. Backfills FarmBot tank fields (Feb 10 - Jul 12 2026) from
   farmbot_history.json and farmbot_readings.json.
5. Writes a complete daily_log.csv ordered by date.
"""

import csv, json, math, os, requests, sys
from collections import deque
from datetime import datetime, timezone, timedelta, date as dt_date
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
BASE   = Path(__file__).parent.parent
CSV_IN = BASE / 'data' / 'daily_log.csv'

EXPORT_GLOB = 'Bidgee_Pumps__Irrigation_-_LAKE_ALBERT_4-7-*.csv'
EXPORT_FILES = sorted(BASE.glob(EXPORT_GLOB))  # one file per year

# ── Davis API creds (for Jul 5-9 2026) ──────────────────────────────────────
DAVIS_V2_KEY     = os.environ.get('DAVIS_V2_KEY',     'kvsweiywmnahb6ayvc7gstbdigst1k9x')
DAVIS_V2_SECRET  = os.environ.get('DAVIS_V2_SECRET',  'urw4q7amnhwnajydf3r1ubggcrvcicvh')
DAVIS_V2_STATION = os.environ.get('DAVIS_V2_STATION', '10489')

# ── Constants ────────────────────────────────────────────────────────────────
TZ              = timezone(timedelta(hours=10))   # AEST
TANK_CAPACITY_L = 250_000

CSV_HEADERS = [
    'date',
    'temp_max', 'temp_min', 'temp_max_time', 'temp_min_time', 'temp_mean',
    'rh_mean',
    'wind_max_kmh', 'wind_mean_kmh', 'wind_dir_deg', 'wind_run_km',
    'rain_mm', 'et_mm', 'rain_rate_max_mmhr',
    'pressure_mean_hpa',
    'solar_rad_avg', 'solar_rad_hi', 'solar_energy_ly',
    'dew_point_c', 'wet_bulb_c',
    'heat_index_c', 'wind_chill_c', 'thsw_index_c',
    'delta_t_mean',
    'uv_max', 'uv_index_avg', 'uv_dose',
    'emc', 'air_density_kgm3', 'night_cloud_cover', 'iss_reception',
    'gdd_bent', 'gdd_kik',
    'gdd_bent_7d', 'gdd_kik_7d',
    'leaf_wet_hours',
    'dollar_spot_pct', 'dollar_spot_risk',
    'fusarium_score', 'fusarium_risk',
    'brown_patch_risk', 'pythium_risk',
    'soil_balance_7d', 'soil_zone',
    'spray_go_hours', 'spray_caution_hours', 'spray_nogo_hours',
    'rain_day', 'frost_flag', 'disease_alert',
    'fog_forecast', 'lightning_forecast',
    'tank_pct', 'tank_volume_l', 'tank_used_l', 'tank_refill',
]

COMPASS = {
    'N':0,'NNE':22.5,'NE':45,'ENE':67.5,
    'E':90,'ESE':112.5,'SE':135,'SSE':157.5,
    'S':180,'SSW':202.5,'SW':225,'WSW':247.5,
    'W':270,'WNW':292.5,'NW':315,'NNW':337.5,
}

# ── Pure maths helpers (mirrors daily_report.py) ─────────────────────────────

def _sf(v):
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


def _compass_deg(s):
    if not s:
        return None
    return COMPASS.get(s.strip().upper())


def _stull_wb(t, rh):
    if t is None or rh is None or rh <= 0:
        return None
    return (t * math.atan(0.151977 * math.sqrt(rh + 8.313659))
            + math.atan(t + rh)
            - math.atan(rh - 1.676331)
            + 0.00391838 * (rh ** 1.5) * math.atan(0.023101 * rh)
            - 4.686035)


def _delta_t(t, rh):
    wb = _stull_wb(t, rh)
    return round(t - wb, 1) if (t is not None and wb is not None) else None


def _is_cart_wet(t, rh, ws_ms, rain):
    if rain and rain > 0:
        return True
    if t is None or rh is None:
        return False
    if rh is None or rh <= 0:
        return False
    gamma = math.log(rh / 100) + (17.625 * t) / (243.04 + t)
    dp = (243.04 * gamma) / (17.625 - gamma)
    ws = ws_ms if ws_ms is not None else 999
    return (t - dp) <= 2.7 and ws <= 3.6


def _spray_status(dt_val, t, ws_kmh):
    if dt_val is None or t is None or ws_kmh is None:
        return 'NO-GO'
    if 2 <= dt_val <= 8 and t < 28 and ws_kmh < 15:
        return 'GO'
    if 2 <= dt_val <= 10 and t < 35 and ws_kmh < 20:
        return 'CAUTION'
    return 'NO-GO'


def _gdd(tmax, tmin, base):
    if tmax is None or tmin is None:
        return 0.0
    return max(0.0, (tmax + tmin) / 2.0 - base)


def _smith_kerns(mean_rh, mean_temp):
    if mean_temp is None or mean_rh is None:
        return 0.0
    if mean_temp < 10 or mean_temp > 35:
        return 0.0
    logit = -11.4041 + 0.0894 * mean_rh + 0.1932 * mean_temp
    prob  = math.exp(logit) / (1 + math.exp(logit))
    return round(prob * 100, 1)


def _dollar_spot_risk(wet_h, t_mean, sk):
    in_range = 15 <= (t_mean or 0) <= 30
    if   wet_h >= 12 and in_range: risk = 'HIGH'
    elif wet_h >= 8  and in_range: risk = 'MODERATE'
    elif wet_h >= 4  and in_range: risk = 'LOW-MOD'
    else:                          risk = 'LOW'
    if sk > 40 and risk in ('LOW', 'LOW-MOD'):  risk = 'MODERATE'
    elif sk > 20 and risk == 'LOW':             risk = 'LOW-MOD'
    return risk


def _fusarium_risk(wet_h, consec_rh90, rain_days_6, t_mean):
    if t_mean is None or not (0 <= t_mean <= 21):
        return 0, 'LOW'
    s = 0
    if consec_rh90 >= 20: s += 5
    elif consec_rh90 >= 12: s += 3
    elif consec_rh90 >= 6:  s += 1
    if wet_h >= 24: s += 3
    elif wet_h >= 10: s += 2
    elif wet_h >= 4:  s += 1
    if rain_days_6 >= 3: s += 2
    elif rain_days_6 >= 1: s += 1
    if 6 <= t_mean <= 13: s += 1
    if   s >= 8: risk = 'HIGH'
    elif s >= 4: risk = 'MODERATE'
    elif s >= 1: risk = 'LOW-MOD'
    else:        risk = 'LOW'
    return s, risk


def _brown_patch_risk(night_wet_h, night_min, day_t):
    if night_wet_h >= 12 and (night_min or 0) > 15 and (day_t or 0) > 25: return 'HIGH'
    if night_wet_h >= 8  and (night_min or 0) > 15: return 'MODERATE'
    if night_wet_h >= 4  and (night_min or 0) > 15: return 'LOW-MOD'
    return 'LOW'


def _pythium_risk(night_wet_h, night_min, day_t):
    if night_wet_h >= 12 and (night_min or 0) > 20 and (day_t or 0) > 30: return 'SEVERE'
    if night_wet_h >= 10 and (night_min or 0) > 20: return 'HIGH'
    if night_wet_h >= 6  and (night_min or 0) > 20: return 'MODERATE'
    return 'LOW'


def _soil_zone(deficit):
    if   deficit < -12: return 'Waterlogged'
    elif deficit < -3:  return 'Wet'
    elif deficit < 12:  return 'Optimal'
    elif deficit < 22:  return 'Dry'
    else:               return 'Very Dry'


def _disease_alert(ds_risk, fus_risk, bp_risk, py_risk):
    high = {'HIGH', 'SEVERE', 'MODERATE'}
    if any(r in high for r in (ds_risk, fus_risk, bp_risk, py_risk)):
        return True
    return False


def _air_density(t_c, p_hpa):
    if t_c is None or p_hpa is None:
        return None
    return round(p_hpa * 100 / (287.058 * (t_c + 273.15)), 4)


def _circ_mean(degs):
    if not degs:
        return None
    sc = [(math.sin(math.radians(d)), math.cos(math.radians(d))) for d in degs]
    ss = sum(x[0] for x in sc)
    cs = sum(x[1] for x in sc)
    return round((math.degrees(math.atan2(ss, cs)) + 360) % 360)


# ── Davis 5-minute export record processing ──────────────────────────────────

def _parse_export_dt(s):
    """Parse 'D/M/YY H:MM AM/PM' -> datetime in AEST."""
    try:
        naive = datetime.strptime(s.strip(), '%d/%m/%y %I:%M %p')
        return naive.replace(tzinfo=TZ)
    except ValueError:
        return None


def load_export_csvs(file_list):
    """
    Read all Davis export CSVs and return dict:
        date_str (YYYY-MM-DD) -> list of per-interval dicts (metric units)
    """
    by_date = {}
    for fp in file_list:
        print(f'  Loading {fp.name} ...', end=' ', flush=True)
        count = 0
        with open(fp, 'r', encoding='latin-1') as f:
            lines = f.readlines()
        # Find header row
        h_idx = None
        for i, l in enumerate(lines[:12]):
            if 'Date' in l and 'Time' in l:
                h_idx = i
                break
        if h_idx is None:
            print('no header found, skipping')
            continue
        reader = csv.DictReader(lines[h_idx:])
        for row in reader:
            dt_str = row.get('Date & Time', '').strip('"')
            dt = _parse_export_dt(dt_str)
            if dt is None:
                continue
            date_key = dt.strftime('%Y-%m-%d')
            rec = {
                'dt':         dt,
                'hour':       dt.hour,
                't_c':        _sf(row.get('Temp - \xb0C') or row.get('Temp - °C')),
                'rh':         _sf(row.get('Hum - %')),
                'ws_kmh':     _sf(row.get('Wind Speed - km/h')),
                'ws_hi_kmh':  _sf(row.get('High Wind Speed - km/h')),
                'wd_raw':     row.get('Wind Direction', '').strip('"').strip(),
                'wind_run':   _sf(row.get('Wind Run - km')),
                'rain':       _sf(row.get('Rain - mm')) or 0.0,
                'rain_rate':  _sf(row.get('Rain Rate - mm/h')) or 0.0,
                'et':         _sf(row.get('ET - mm')) or 0.0,
                'solar_avg':  _sf(row.get('Solar Rad - W/m^2')),
                'solar_hi':   _sf(row.get('High Solar Rad - W/m^2')),
                'solar_e':    _sf(row.get('Solar Energy - Ly')) or 0.0,
                'pressure':   _sf(row.get('Barometer - hPa')),
                'dew_pt':     _sf(row.get('Dew Point - \xb0C') or row.get('Dew Point - °C')),
                'wet_bulb':   _sf(row.get('Wet Bulb - \xb0C') or row.get('Wet Bulb - °C')),
                'heat_idx':   _sf(row.get('Heat Index - \xb0C') or row.get('Heat Index - °C')),
                'wind_chill': _sf(row.get('Wind Chill - \xb0C') or row.get('Wind Chill - °C')),
                'thsw':       _sf(row.get('THSW Index - \xb0C') or row.get('THSW Index - °C')),
                'uv_idx':     _sf(row.get('UV Index')),
                'uv_dose':    _sf(row.get('UV Dose - MEDs')) or 0.0,
                'uv_hi':      _sf(row.get('High UV Index')),
            }
            if date_key not in by_date:
                by_date[date_key] = []
            by_date[date_key].append(rec)
            count += 1
        print(f'{count:,} records')
    return by_date


def compute_day_from_export(date_str, recs):
    """Compute daily summary from list of 5-minute export records."""
    if not recs:
        return None

    temps, temps_ct = [], []
    night_temps = []
    rh_vals, ws_vals, wd_vals = [], [], []
    wind_run = 0.0
    rain_tot, et_tot, rain_rate_hi = 0.0, 0.0, 0.0
    sol_avg, sol_hi = [], []
    sol_e = 0.0
    pressures = []
    dew_pts, wet_bs, heat_idxs, wind_chills, thsws = [], [], [], [], []
    uv_idxs, uv_doses = [], 0.0

    hourly = {}  # hour -> {wet, night, rh, t_c, ws_kmh, dt}

    for r in recs:
        t = r['t_c']
        rh = r['rh']
        ws_kmh = r['ws_kmh']
        ws_ms  = ws_kmh / 3.6 if ws_kmh is not None else None
        rain   = r['rain']
        hour   = r['hour']
        night  = hour >= 20 or hour < 8

        if t is not None:
            temps.append(t)
            temps_ct.append((t, r['dt']))
        if rh  is not None: rh_vals.append(rh)
        if ws_kmh is not None: ws_vals.append(ws_kmh)
        if r['ws_hi_kmh'] is not None: ws_vals.append(r['ws_hi_kmh'])

        wd_deg = _compass_deg(r['wd_raw'])
        if wd_deg is not None:
            wd_vals.append(wd_deg)

        if r['wind_run'] is not None: wind_run += r['wind_run']
        rain_tot += rain
        et_tot   += r['et']
        if r['rain_rate'] > rain_rate_hi: rain_rate_hi = r['rain_rate']

        if r['solar_avg'] is not None: sol_avg.append(r['solar_avg'])
        if r['solar_hi']  is not None: sol_hi.append(r['solar_hi'])
        sol_e += r['solar_e']

        if r['pressure']   is not None: pressures.append(r['pressure'])
        if r['dew_pt']     is not None: dew_pts.append(r['dew_pt'])
        if r['wet_bulb']   is not None: wet_bs.append(r['wet_bulb'])
        if r['heat_idx']   is not None: heat_idxs.append(r['heat_idx'])
        if r['wind_chill'] is not None: wind_chills.append(r['wind_chill'])
        if r['thsw']       is not None: thsws.append(r['thsw'])
        if r['uv_idx']     is not None: uv_idxs.append(r['uv_idx'])
        if r['uv_hi']      is not None: uv_idxs.append(r['uv_hi'])
        uv_doses += r['uv_dose']

        if night and t is not None:
            night_temps.append(t)

        wet = _is_cart_wet(t, rh, ws_ms, rain)
        if hour not in hourly:
            dt_v = (t - r['wet_bulb']) if (t is not None and r['wet_bulb'] is not None) else _delta_t(t, rh)
            hourly[hour] = {'wet': wet, 'night': night, 'rh': rh or 0,
                            't_c': t, 'ws_kmh': ws_kmh, 'dt': dt_v}
        else:
            hourly[hour]['wet']   = hourly[hour]['wet'] or wet
            hourly[hour]['rh']    = max(hourly[hour]['rh'], rh or 0)

    if not temps:
        return None

    # ── Aggregates ────────────────────────────────────────────
    t_max  = round(max(temps), 1)
    t_min  = round(min(temps), 1)
    t_mean = round(sum(temps) / len(temps), 1)

    max_rec = max(temps_ct, key=lambda x: x[0])
    min_rec = min(temps_ct, key=lambda x: x[0])
    # strftime with %-I won't work on Windows; use manual formatting
    def fmt_time(dt):
        h = dt.hour % 12 or 12
        return f"{h}:{dt.strftime('%M')} {'AM' if dt.hour < 12 else 'PM'}"
    t_max_time = fmt_time(max_rec[1])
    t_min_time = fmt_time(min_rec[1])

    rh_mean    = round(sum(rh_vals) / len(rh_vals), 1) if rh_vals else None
    ws_mean    = round(sum(ws_vals) / len(ws_vals), 1) if ws_vals else None
    ws_max     = round(max(ws_vals), 1)                if ws_vals else None
    wd_mean    = _circ_mean(wd_vals)
    wr_km      = round(wind_run, 1) if wind_run > 0 else None

    rain_mm    = round(rain_tot, 1)
    et_mm      = round(et_tot, 2)
    rr_max     = round(rain_rate_hi, 1) if rain_rate_hi > 0 else None

    sol_avg_d  = round(sum(sol_avg) / len(sol_avg), 1) if sol_avg else None
    sol_hi_d   = round(max(sol_hi), 0)                 if sol_hi  else None
    sol_e_d    = round(sol_e, 2)                       if sol_e > 0 else None

    pres_mean  = round(sum(pressures) / len(pressures), 1) if pressures else None
    dp_mean    = round(sum(dew_pts) / len(dew_pts), 1)     if dew_pts   else None
    wb_mean    = round(sum(wet_bs)  / len(wet_bs),  1)     if wet_bs    else None
    hi_mean    = round(sum(heat_idxs) / len(heat_idxs), 1) if heat_idxs else None
    wc_min     = round(min(wind_chills), 1)                if wind_chills else None
    thsw_max   = round(max(thsws), 1)                     if thsws      else None

    if wb_mean is not None and t_mean is not None:
        dt_mean = round(t_mean - wb_mean, 1)
    else:
        dt_vals = [_delta_t(t, r) for t, r in zip(temps, rh_vals) if t is not None and r is not None]
        dt_mean = round(sum(dt_vals) / len(dt_vals), 1) if dt_vals else None

    uv_max_d  = round(max(uv_idxs), 1) if uv_idxs else None
    uv_dose_d = round(uv_doses, 2)     if uv_doses > 0 else None

    air_d = _air_density(t_mean, pres_mean)

    # Leaf wetness
    wet_h       = sum(1 for h in hourly.values() if h['wet'])
    night_wet_h = sum(1 for h in hourly.values() if h['wet'] and h['night'])
    night_min   = round(min(night_temps), 1) if night_temps else t_min
    rh90_h      = sum(1 for h in hourly.values() if h['rh'] >= 90)

    # Spray
    spray = {'GO': 0, 'CAUTION': 0, 'NO-GO': 0}
    for hdata in hourly.values():
        spray[_spray_status(hdata['dt'], hdata['t_c'], hdata['ws_kmh'])] += 1

    return {
        'date':            date_str,
        'temp_max':        t_max,
        'temp_min':        t_min,
        'temp_max_time':   t_max_time,
        'temp_min_time':   t_min_time,
        'temp_mean':       t_mean,
        'rh_mean':         rh_mean,
        'wind_max_kmh':    ws_max,
        'wind_mean_kmh':   ws_mean,
        'wind_dir_deg':    wd_mean,
        'wind_run_km':     wr_km,
        'rain_mm':         rain_mm,
        'et_mm':           et_mm,
        'rain_rate_max_mmhr': rr_max,
        'pressure_mean_hpa':  pres_mean,
        'solar_rad_avg':   sol_avg_d,
        'solar_rad_hi':    sol_hi_d,
        'solar_energy_ly': sol_e_d,
        'dew_point_c':     dp_mean,
        'wet_bulb_c':      wb_mean,
        'heat_index_c':    hi_mean,
        'wind_chill_c':    wc_min,
        'thsw_index_c':    thsw_max,
        'delta_t_mean':    dt_mean,
        'uv_max':          uv_max_d,
        'uv_index_avg':    None,   # not in export CSV
        'uv_dose':         uv_dose_d,
        'emc':             None,   # only available from API
        'air_density_kgm3': air_d,
        'night_cloud_cover': None,
        'iss_reception':   None,
        # rolling fields computed later
        '_wet_h':          wet_h,
        '_night_wet_h':    night_wet_h,
        '_night_min':      night_min,
        '_rh90_h':         rh90_h,
        '_spray':          spray,
    }


# ── Davis API fetch for the 5 missing days ───────────────────────────────────

def _f_to_c(f):
    return (float(f) - 32) / 1.8 if f is not None else None


def fetch_times_from_api(target_date):
    """Return (temp_max_time, temp_min_time) strings via Davis v2 API."""
    day_start = datetime(target_date.year, target_date.month, target_date.day,
                         0, 0, 0, tzinfo=TZ)
    day_end   = datetime(target_date.year, target_date.month, target_date.day,
                         23, 59, 59, tzinfo=TZ)
    url = f'https://api.weatherlink.com/v2/historic/{DAVIS_V2_STATION}'
    params  = {'api-key': DAVIS_V2_KEY,
               'start-timestamp': str(int(day_start.timestamp())),
               'end-timestamp':   str(int(day_end.timestamp()))}
    headers = {'X-Api-Secret': DAVIS_V2_SECRET}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f'    API error for {target_date}: {e}')
        return None, None

    temps_ct = []
    for sensor in data.get('sensors', []):
        for rec in sensor.get('data', []):
            ts = rec.get('ts')
            t_f = rec.get('temp') or rec.get('temp_out')
            if ts and t_f is not None:
                t_c = _f_to_c(float(t_f))
                dt  = datetime.fromtimestamp(ts, tz=TZ)
                temps_ct.append((t_c, dt))
    if not temps_ct:
        return None, None

    def fmt(dt):
        h = dt.hour % 12 or 12
        return f"{h}:{dt.strftime('%M')} {'AM' if dt.hour < 12 else 'PM'}"

    return fmt(max(temps_ct, key=lambda x: x[0])[1]), \
           fmt(min(temps_ct, key=lambda x: x[0])[1])


# ── FarmBot tank data ─────────────────────────────────────────────────────────

def load_farmbot_tank():
    """Return dict: date_str -> (pct, vol_l, used_l, refill)."""
    AEST = TZ
    tank = {}

    # farmbot_history.json (Feb 10 - Jun 30 2026) - daily summaries
    hist_path = BASE / 'data' / 'farmbot_history.json'
    if hist_path.exists():
        for entry in json.loads(hist_path.read_text()):
            d = entry.get('date')
            pct = entry.get('evening_pct')
            if d and pct is not None:
                vol  = round(pct / 100 * TANK_CAPACITY_L)
                used = entry.get('used_l', 0)
                ref  = entry.get('refill', False)
                tank[d] = (pct, vol, used, ref)

    # farmbot_readings.json - derive daily for any date not already covered
    rdg_path = BASE / 'data' / 'farmbot_readings.json'
    if rdg_path.exists():
        readings = json.loads(rdg_path.read_text())
        by_date = {}
        for r in readings:
            ts = r.get('date', '').replace('Z', '+00:00')
            try:
                dt = datetime.fromisoformat(ts).astimezone(AEST)
            except Exception:
                continue
            dk = dt.strftime('%Y-%m-%d')
            if dk not in by_date:
                by_date[dk] = []
            by_date[dk].append((dt, r.get('pct'), r.get('volume_l')))

        for dk, day_rdgs in by_date.items():
            if dk in tank:
                continue  # prefer history.json
            day_rdgs.sort(key=lambda x: x[0])
            morning_pct = day_rdgs[0][1]
            evening_pct = day_rdgs[-1][1]
            if evening_pct is None:
                continue
            vol  = round(evening_pct / 100 * TANK_CAPACITY_L)
            used = round(max(0.0, (morning_pct or 0) - evening_pct) / 100 * TANK_CAPACITY_L)
            ref  = (evening_pct or 0) > (morning_pct or 0) + 2
            tank[dk] = (evening_pct, vol, used, ref)

    return tank


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print('=== WWCC Weather History Backfill ===')
    print()

    # 1. Load existing CSV rows
    print('Loading existing daily_log.csv ...')
    existing = {}
    with open(CSV_IN, 'r', newline='') as f:
        for row in csv.DictReader(f):
            existing[row['date']] = row
    print(f'  {len(existing)} existing rows ({min(existing)} to {max(existing)})')

    # 2. Load Davis export CSVs
    print(f'\nLoading {len(EXPORT_FILES)} Davis export files ...')
    export_by_date = load_export_csvs(EXPORT_FILES)
    all_export_dates = sorted(export_by_date.keys())
    print(f'  Export covers {len(all_export_dates)} unique dates'
          f' ({all_export_dates[0]} to {all_export_dates[-1]})')

    # 3. FarmBot tank data
    print('\nLoading FarmBot tank data ...')
    tank_data = load_farmbot_tank()
    print(f'  Tank data available for {len(tank_data)} dates'
          f' ({min(tank_data)} to {max(tank_data)})')

    # 4. Determine full date range
    all_dates = sorted(set(list(export_by_date.keys()) + list(existing.keys())))
    print(f'\nTotal date range: {all_dates[0]} to {all_dates[-1]} ({len(all_dates)} dates)')

    # 5. Determine which dates need API calls (not in export, not in existing)
    api_needed = [d for d in existing
                  if d not in export_by_date
                  and not existing[d].get('temp_max_time', '').strip()]
    print(f'Dates needing Davis API for temp times: {api_needed}')

    # 6. Fetch times from API
    api_times = {}
    for d in api_needed:
        target = dt_date.fromisoformat(d)
        print(f'  API call for {d} ...')
        tmax, tmin = fetch_times_from_api(target)
        api_times[d] = (tmax, tmin)
        print(f'    max_time={tmax}  min_time={tmin}')

    # 7. Process each date in chronological order
    print('\nComputing daily rows ...')

    # Rolling windows
    gdd_bent_w  = deque(maxlen=7)
    gdd_kik_w   = deque(maxlen=7)
    soil_w      = deque(maxlen=7)   # rain - et per day
    t_mean_5    = deque(maxlen=5)
    rh_mean_5   = deque(maxlen=5)
    rain_days_6 = deque(maxlen=6)

    final_rows = {}

    for date_str in all_dates:
        is_existing = date_str in existing

        if is_existing:
            row = dict(existing[date_str])
        else:
            # Compute from export data
            recs = export_by_date.get(date_str, [])
            computed = compute_day_from_export(date_str, recs)
            if computed is None:
                continue
            row = {k: '' for k in CSV_HEADERS}
            row['date'] = date_str
            for k in ('temp_max','temp_min','temp_max_time','temp_min_time','temp_mean',
                      'rh_mean','wind_max_kmh','wind_mean_kmh','wind_dir_deg','wind_run_km',
                      'rain_mm','et_mm','rain_rate_max_mmhr','pressure_mean_hpa',
                      'solar_rad_avg','solar_rad_hi','solar_energy_ly',
                      'dew_point_c','wet_bulb_c','heat_index_c','wind_chill_c','thsw_index_c',
                      'delta_t_mean','uv_max','uv_index_avg','uv_dose',
                      'emc','air_density_kgm3','night_cloud_cover','iss_reception'):
                v = computed.get(k)
                row[k] = '' if v is None else v

        # Fill temp times from export if missing
        if not str(row.get('temp_max_time', '')).strip():
            if date_str in export_by_date:
                recs = export_by_date[date_str]
                tmp = compute_day_from_export(date_str, recs)
                if tmp:
                    row['temp_max_time'] = tmp.get('temp_max_time', '')
                    row['temp_min_time'] = tmp.get('temp_min_time', '')
            elif date_str in api_times:
                row['temp_max_time'] = api_times[date_str][0] or ''
                row['temp_min_time'] = api_times[date_str][1] or ''

        # Tank data
        if not str(row.get('tank_pct', '')).strip() and date_str in tank_data:
            pct, vol, used, ref = tank_data[date_str]
            row['tank_pct']      = pct
            row['tank_volume_l'] = vol
            row['tank_used_l']   = used
            row['tank_refill']   = ref

        # ── Rolling computed fields (for new historical rows only) ──
        if not is_existing:
            t_max  = _sf(row.get('temp_max'))
            t_min  = _sf(row.get('temp_min'))
            t_mean = _sf(row.get('temp_mean'))
            rh     = _sf(row.get('rh_mean'))
            rain   = _sf(row.get('rain_mm')) or 0.0
            et     = _sf(row.get('et_mm'))   or 0.0
            wet_h      = computed.get('_wet_h', 0)
            night_wet_h= computed.get('_night_wet_h', 0)
            night_min  = computed.get('_night_min')
            rh90_h     = computed.get('_rh90_h', 0)
            spray_cnts = computed.get('_spray', {'GO':0,'CAUTION':0,'NO-GO':0})

            gd_b = round(_gdd(t_max, t_min, 10), 1)
            gd_k = round(_gdd(t_max, t_min, 15), 1)
            gdd_bent_w.append(gd_b)
            gdd_kik_w.append(gd_k)

            soil_w.append(rain - et)
            soil_bal = round(sum(soil_w), 1)

            if t_mean is not None: t_mean_5.append(t_mean)
            if rh     is not None: rh_mean_5.append(rh)

            t5 = (sum(t_mean_5) / len(t_mean_5)) if t_mean_5 else None
            r5 = (sum(rh_mean_5) / len(rh_mean_5)) if rh_mean_5 else None
            sk = _smith_kerns(r5, t5)
            ds_risk = _dollar_spot_risk(wet_h, t_mean, sk)

            rain_days_6.append(1 if rain > 0 else 0)
            rd6 = sum(rain_days_6)

            fus_s, fus_r = _fusarium_risk(wet_h, rh90_h, rd6, t_mean)
            bp_r = _brown_patch_risk(night_wet_h, night_min, t_max)
            py_r = _pythium_risk(night_wet_h, night_min, t_max)

            row['gdd_bent']        = gd_b
            row['gdd_kik']         = gd_k
            row['gdd_bent_7d']     = round(sum(gdd_bent_w), 1)
            row['gdd_kik_7d']      = round(sum(gdd_kik_w), 1)
            row['leaf_wet_hours']  = wet_h
            row['dollar_spot_pct'] = sk
            row['dollar_spot_risk']= ds_risk
            row['fusarium_score']  = fus_s
            row['fusarium_risk']   = fus_r
            row['brown_patch_risk']= bp_r
            row['pythium_risk']    = py_r
            row['soil_balance_7d'] = soil_bal
            row['soil_zone']       = _soil_zone(soil_bal)
            row['spray_go_hours']      = spray_cnts.get('GO', 0)
            row['spray_caution_hours'] = spray_cnts.get('CAUTION', 0)
            row['spray_nogo_hours']    = spray_cnts.get('NO-GO', 0)
            row['rain_day']        = 1 if rain > 0 else 0
            row['frost_flag']      = 1 if (t_min is not None and t_min <= 2.0) else 0
            row['disease_alert']   = _disease_alert(ds_risk, fus_r, bp_r, py_r)
            row['fog_forecast']    = ''
            row['lightning_forecast'] = ''
        else:
            # Update rolling windows from existing row so subsequent new rows are accurate
            t_max  = _sf(row.get('temp_max'))
            t_min  = _sf(row.get('temp_min'))
            t_mean = _sf(row.get('temp_mean'))
            rh     = _sf(row.get('rh_mean'))
            rain   = _sf(row.get('rain_mm')) or 0.0
            et     = _sf(row.get('et_mm'))   or 0.0
            gdd_bent_w.append(round(_gdd(t_max, t_min, 10), 1))
            gdd_kik_w.append(round(_gdd(t_max, t_min, 15), 1))
            soil_w.append(rain - et)
            if t_mean is not None: t_mean_5.append(t_mean)
            if rh     is not None: rh_mean_5.append(rh)
            rain_days_6.append(1 if rain > 0 else 0)

        final_rows[date_str] = row

    # 8. Write output CSV
    print(f'\nWriting {len(final_rows)} rows to daily_log.csv ...')
    with open(CSV_IN, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction='ignore')
        writer.writeheader()
        for date_str in sorted(final_rows.keys()):
            writer.writerow(final_rows[date_str])

    print(f'Done. CSV now has {len(final_rows)} rows.')
    print(f'  Range: {min(final_rows)} to {max(final_rows)}')


if __name__ == '__main__':
    main()
