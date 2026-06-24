"""
WWCC Daily Weather Report
=========================
Runs via GitHub Actions every morning at 6 AM AEST.

- Fetches yesterday's data from Davis WeatherLink v2 API
- Fetches yesterday's data from Open-Meteo archive API
- Calculates disease risk, GDD, soil moisture, spray conditions
- Appends a row to data/daily_log.csv (permanent archive)
- Saves an HTML report to data/reports/YYYY/MM/YYYY-MM-DD.html
- Emails morning briefing to greenkeeper (daily)
- Emails weekly summary to greenkeeper + committee (Monday)
- Emails monthly summary to greenkeeper + committee (1st of month)
"""

import os
import csv
import hmac
import hashlib
import math
import json
import time
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from pathlib import Path
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, To, Email

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — loaded from GitHub Secrets (environment variables)
# ─────────────────────────────────────────────────────────────────────────────

DAVIS_DID        = os.environ.get('DAVIS_DID',        '001D0A00AB84')
DAVIS_PASS       = os.environ.get('DAVIS_PASS',        'Ap08021977')
DAVIS_TOKEN      = os.environ.get('DAVIS_TOKEN',       '771389FF9E4B4856A18AD35028EAFCE8')
DAVIS_V2_KEY     = os.environ.get('DAVIS_V2_KEY',      'kvsweiywmnahb6ayvc7gstbdigst1k9x')
DAVIS_V2_SECRET  = os.environ.get('DAVIS_V2_SECRET',   'urw4q7amnhwnajydf3r1ubggcrvcicvh')
DAVIS_V2_STATION = os.environ.get('DAVIS_V2_STATION',  '10489')

SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
EMAIL_FROM       = os.environ.get('EMAIL_FROM',       'wwccweather@gmail.com')

# Comma-separated list of email addresses from secret, e.g.:
# "greenkeeper@wwcc.com.au,committee@wwcc.com.au"
EMAIL_RECIPIENTS_ALL     = os.environ.get('EMAIL_RECIPIENTS', '').split(',')
EMAIL_RECIPIENTS_GK_ONLY = [EMAIL_RECIPIENTS_ALL[0]] if EMAIL_RECIPIENTS_ALL else []

CLUB_LAT = -35.1082
CLUB_LON =  147.3598
TZ       = ZoneInfo('Australia/Sydney')

# Paths (relative to repo root, where the script runs from)
CSV_PATH     = Path('data/daily_log.csv')
REPORTS_ROOT = Path('data/reports')

CSV_HEADERS = [
    'date',
    'temp_max', 'temp_min', 'temp_mean',
    'rh_mean',
    'wind_max_kmh', 'wind_mean_kmh',
    'rain_mm', 'et_mm',
    'pressure_mean_hpa',
    'delta_t_mean',
    'uv_max',
    'gdd_bent', 'gdd_kik',
    'gdd_bent_7d', 'gdd_kik_7d',
    'leaf_wet_hours',
    'dollar_spot_pct',
    'dollar_spot_risk',
    'fusarium_score', 'fusarium_risk',
    'brown_patch_risk',
    'pythium_risk',
    'soil_balance_7d', 'soil_zone',
    'spray_go_hours', 'spray_caution_hours', 'spray_nogo_hours',
    'rain_day',
    'frost_flag',
    'disease_alert',
]


# ─────────────────────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def stull_wetbulb(temp_c, rh):
    """Stull (2011) wet-bulb approximation."""
    if temp_c is None or rh is None:
        return None
    tw = (temp_c * math.atan(0.151977 * math.sqrt(rh + 8.313659))
          + math.atan(temp_c + rh)
          - math.atan(rh - 1.676331)
          + 0.00391838 * (rh ** 1.5) * math.atan(0.023101 * rh)
          - 4.686035)
    return tw


def delta_t(temp_c, rh):
    tw = stull_wetbulb(temp_c, rh)
    if tw is None:
        return None
    return round(temp_c - tw, 1)


def dew_point(temp_c, rh):
    """Magnus formula dew point."""
    if temp_c is None or rh is None or rh <= 0:
        return None
    gamma = math.log(rh / 100) + (17.625 * temp_c) / (243.04 + temp_c)
    return (243.04 * gamma) / (17.625 - gamma)


def f_to_c(f):
    return (f - 32) / 1.8 if f is not None else None


def is_cart_wet(temp_c, rh, wind_ms, rain_mm):
    """CART leaf wetness model — matches dashboard logic exactly."""
    if rain_mm and rain_mm > 0:
        return True
    if temp_c is None or rh is None:
        return False
    dp = dew_point(temp_c, rh)
    if dp is None:
        return False
    dpd = temp_c - dp
    ws  = wind_ms if wind_ms is not None else 999
    return dpd <= 2.7 and ws <= 3.6


def spray_status(dt_val, temp_c, wind_kmh,
                 dt_min=2, dt_max=8, dt_warn=10,
                 temp_max=28, temp_warn=35,
                 wind_max=15, wind_warn=20):
    """
    Returns 'GO', 'CAUTION', or 'NO-GO' for a single hour.
    Uses General/Default thresholds unless overridden.
    """
    if dt_val is None or temp_c is None or wind_kmh is None:
        return 'NO-GO'
    dt_ok   = dt_min  <= dt_val  <= dt_max
    dt_warn_ok = dt_min <= dt_val <= dt_warn
    t_ok    = temp_c  < temp_max
    t_warn_ok  = temp_c  < temp_warn
    w_ok    = wind_kmh < wind_max
    w_warn_ok  = wind_kmh < wind_warn

    if dt_ok and t_ok and w_ok:
        return 'GO'
    if dt_warn_ok and t_warn_ok and w_warn_ok:
        return 'CAUTION'
    return 'NO-GO'


def soil_zone(deficit_mm):
    if deficit_mm < -12:
        return 'Waterlogged'
    elif deficit_mm < -3:
        return 'Wet'
    elif deficit_mm < 12:
        return 'Optimal'
    elif deficit_mm < 22:
        return 'Dry'
    else:
        return 'Very Dry'


def gdd(temp_max, temp_min, base):
    if temp_max is None or temp_min is None:
        return 0
    return max(0, (temp_max + temp_min) / 2 - base)


def smith_kerns(mean_rh, mean_temp):
    """Smith-Kerns Dollar Spot logistic regression (5-day averages)."""
    if mean_temp is None or mean_rh is None:
        return 0.0
    if mean_temp < 10 or mean_temp > 35:
        return 0.0
    logit = -11.4041 + (0.0894 * mean_rh) + (0.1932 * mean_temp)
    prob  = math.exp(logit) / (1 + math.exp(logit))
    return round(prob * 100, 1)


def dollar_spot_risk(wet_hours, temp_mean, sk_pct):
    """Dollar Spot risk level."""
    in_range = 15 <= (temp_mean or 0) <= 30
    if wet_hours >= 12 and in_range:
        risk = 'HIGH'
    elif wet_hours >= 8 and in_range:
        risk = 'MODERATE'
    elif wet_hours >= 4 and in_range:
        risk = 'LOW-MOD'
    else:
        risk = 'LOW'
    # Boost from Smith-Kerns
    if sk_pct > 40 and risk in ('LOW', 'LOW-MOD'):
        risk = 'MODERATE'
    elif sk_pct > 20 and risk == 'LOW':
        risk = 'LOW-MOD'
    return risk


def fusarium_risk(wet_hours, consec_rh90, rain_days_6, temp_mean):
    """Fusarium Patch scoring — matches dashboard exactly."""
    if temp_mean is None or not (0 <= temp_mean <= 21):
        return 0, 'LOW'
    score = 0
    if consec_rh90 >= 20:   score += 5
    elif consec_rh90 >= 12: score += 3
    elif consec_rh90 >= 6:  score += 1
    if wet_hours >= 24:     score += 3
    elif wet_hours >= 10:   score += 2
    elif wet_hours >= 4:    score += 1
    if rain_days_6 >= 3:    score += 2
    elif rain_days_6 >= 1:  score += 1
    if 6 <= temp_mean <= 13: score += 1
    if score >= 8:   risk = 'HIGH'
    elif score >= 4: risk = 'MODERATE'
    elif score >= 1: risk = 'LOW-MOD'
    else:            risk = 'LOW'
    return score, risk


def brown_patch_risk(night_wet_hours, night_min, day_temp):
    if night_wet_hours >= 12 and (night_min or 0) > 15 and (day_temp or 0) > 25:
        return 'HIGH'
    elif night_wet_hours >= 8 and (night_min or 0) > 15:
        return 'MODERATE'
    elif night_wet_hours >= 4 and (night_min or 0) > 15:
        return 'LOW-MOD'
    return 'LOW'


def pythium_risk(night_wet_hours, night_min, day_temp):
    if night_wet_hours >= 14 and (night_min or 0) > 20 and (day_temp or 0) > 30:
        return 'SEVERE'
    elif night_wet_hours >= 10 and (night_min or 0) > 20:
        return 'HIGH'
    elif night_wet_hours >= 6 and (night_min or 0) > 20:
        return 'MODERATE'
    return 'LOW'


# ─────────────────────────────────────────────────────────────────────────────
# DAVIS WEATHERLINK v2 API
# ─────────────────────────────────────────────────────────────────────────────

def davis_v2_sign(params):
    """Compute HMAC-SHA256 signature for Davis v2 API."""
    params['api-key'] = DAVIS_V2_KEY
    params['t']       = str(int(time.time()))
    sorted_params     = sorted(params.items())
    param_str         = ''.join(f'{k}{v}' for k, v in sorted_params)
    sig = hmac.new(
        DAVIS_V2_SECRET.encode(),
        param_str.encode(),
        hashlib.sha256
    ).hexdigest()
    params['api-signature'] = sig
    return params


def fetch_davis_historic(target_date):
    """
    Fetch sub-hourly records for target_date (date object, Sydney time).
    Returns list of record dicts from all sensors combined.
    """
    # Start/end of day in Sydney time, converted to UTC Unix timestamps
    day_start = datetime(target_date.year, target_date.month, target_date.day,
                         0, 0, 0, tzinfo=TZ)
    day_end   = datetime(target_date.year, target_date.month, target_date.day,
                         23, 59, 59, tzinfo=TZ)
    start_ts  = int(day_start.timestamp())
    end_ts    = int(day_end.timestamp())

    params = davis_v2_sign({
        'start-timestamp': str(start_ts),
        'end-timestamp':   str(end_ts),
    })

    url = f'https://api.weatherlink.com/v2/historic/{DAVIS_V2_STATION}'
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f'Davis v2 API error: {e}')
        return []

    records = []
    for sensor in data.get('sensors', []):
        for rec in sensor.get('data', []):
            records.append(rec)
    return records


def process_davis_records(records):
    """
    Extract daily summary and hourly wetness from Davis v2 records.
    Returns dict of processed values.
    """
    temps_c, rh_vals, wind_ms_vals, rain_total, et_total = [], [], [], 0.0, 0.0
    pressures = []
    hourly    = {}  # hour_int -> {'wet': bool, 'night': bool, 'rh': float}

    for rec in records:
        ts = rec.get('ts')
        if not ts:
            continue
        rec_dt = datetime.fromtimestamp(ts, tz=TZ)
        hour   = rec_dt.hour

        # Temperature (F -> C)
        t_f = rec.get('temp') or rec.get('temp_out')
        t_c = f_to_c(float(t_f)) if t_f is not None else None

        # Humidity
        rh = rec.get('hum') or rec.get('hum_out')
        rh = float(rh) if rh is not None else None

        # Wind (mph -> m/s and km/h)
        ws_mph  = rec.get('wind_speed_avg') or rec.get('wind_speed_last')
        ws_ms   = float(ws_mph) * 0.44704 if ws_mph is not None else None
        ws_kmh  = float(ws_mph) * 1.60934 if ws_mph is not None else None

        # Rain
        rain_mm = float(rec.get('rainfall_mm') or rec.get('rain_mm') or 0)

        # ET (inches -> mm)
        et_in = rec.get('et') or 0
        et_mm = float(et_in) * 25.4

        # Pressure (inHg -> hPa)
        bar_in  = rec.get('bar_sea_level_in') or rec.get('bar_in')
        if bar_in:
            pressures.append(float(bar_in) * 33.8639)

        if t_c is not None: temps_c.append(t_c)
        if rh   is not None: rh_vals.append(rh)
        if ws_kmh is not None: wind_ms_vals.append(ws_kmh)
        rain_total += rain_mm
        et_total   += et_mm

        # Leaf wetness per hour (CART model)
        wet   = is_cart_wet(t_c, rh, ws_ms, rain_mm)
        night = hour >= 22 or hour < 6
        if hour not in hourly:
            hourly[hour] = {'wet': wet, 'night': night, 'rh': rh or 0}
        else:
            hourly[hour]['wet']   = hourly[hour]['wet'] or wet
            hourly[hour]['night'] = night
            hourly[hour]['rh']    = max(hourly[hour]['rh'], rh or 0)

    temp_max  = round(max(temps_c), 1)          if temps_c     else None
    temp_min  = round(min(temps_c), 1)          if temps_c     else None
    temp_mean = round(sum(temps_c)/len(temps_c), 1) if temps_c else None
    rh_mean   = round(sum(rh_vals)/len(rh_vals), 1) if rh_vals  else None
    wind_max  = round(max(wind_ms_vals), 1)     if wind_ms_vals else None
    wind_mean = round(sum(wind_ms_vals)/len(wind_ms_vals), 1) if wind_ms_vals else None
    pres_mean = round(sum(pressures)/len(pressures), 1) if pressures else None

    wet_hours       = sum(1 for h in hourly.values() if h['wet'])
    night_wet_hours = sum(1 for h in hourly.values() if h['wet'] and h['night'])
    night_min       = min((t for t in temps_c
                           if datetime.fromtimestamp(0, tz=TZ)), default=None)
    rh90_hours      = sum(1 for h in hourly.values() if h['rh'] >= 90)

    # Consecutive RH >= 90 (simplified: total hours as proxy)
    consec_rh90 = rh90_hours  # conservative — full consecutive calc needs ordering

    dt_vals = [delta_t(t, r) for t, r in zip(temps_c, rh_vals) if t and r]
    dt_mean = round(sum(dt_vals)/len(dt_vals), 1) if dt_vals else None

    # Spray condition hours (using general/default thresholds)
    spray_counts = {'GO': 0, 'CAUTION': 0, 'NO-GO': 0}
    for h_idx, hdata in hourly.items():
        # Get representative values for this hour
        hour_recs = [r for r in records
                     if datetime.fromtimestamp(r.get('ts', 0), tz=TZ).hour == h_idx]
        if not hour_recs:
            continue
        t_f  = hour_recs[0].get('temp') or hour_recs[0].get('temp_out')
        t_c  = f_to_c(float(t_f)) if t_f else None
        rh_v = hour_recs[0].get('hum') or hour_recs[0].get('hum_out')
        rh_v = float(rh_v) if rh_v else None
        ws_m = hour_recs[0].get('wind_speed_avg') or hour_recs[0].get('wind_speed_last')
        ws_k = float(ws_m) * 1.60934 if ws_m else None
        dt_v = delta_t(t_c, rh_v)
        status = spray_status(dt_v, t_c, ws_k)
        spray_counts[status] += 1

    return {
        'temp_max':        temp_max,
        'temp_min':        temp_min,
        'temp_mean':       temp_mean,
        'rh_mean':         rh_mean,
        'wind_max_kmh':    wind_max,
        'wind_mean_kmh':   wind_mean,
        'rain_mm':         round(rain_total, 1),
        'et_mm':           round(et_total, 2),
        'pressure_mean':   pres_mean,
        'delta_t_mean':    dt_mean,
        'wet_hours':       wet_hours,
        'night_wet_hours': night_wet_hours,
        'night_min':       min(temps_c) if temps_c else None,
        'consec_rh90':     consec_rh90,
        'spray_go':        spray_counts['GO'],
        'spray_caution':   spray_counts['CAUTION'],
        'spray_nogo':      spray_counts['NO-GO'],
        'rain_day':        rain_total > 0.2,
    }


# ─────────────────────────────────────────────────────────────────────────────
# OPEN-METEO ARCHIVE API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_openmeteo_archive(target_date):
    """Fetch yesterday's data from Open-Meteo archive API."""
    date_str = target_date.strftime('%Y-%m-%d')
    url = 'https://archive-api.open-meteo.com/v1/archive'
    params = {
        'latitude':   CLUB_LAT,
        'longitude':  CLUB_LON,
        'start_date': date_str,
        'end_date':   date_str,
        'daily':      'temperature_2m_max,temperature_2m_min,precipitation_sum,et0_fao_evapotranspiration,uv_index_max',
        'hourly':     'temperature_2m,relative_humidity_2m,windspeed_10m,precipitation',
        'timezone':   'Australia/Sydney',
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f'Open-Meteo archive error: {e}')
        return None


# ─────────────────────────────────────────────────────────────────────────────
# CSV MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def read_csv_history(n_days=7):
    """Read the last n_days rows from the CSV. Returns list of dicts."""
    if not CSV_PATH.exists():
        return []
    rows = []
    with open(CSV_PATH, newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows[-n_days:]


def append_csv_row(row_dict):
    """Append a row to the CSV, creating it with headers if needed."""
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_header = not CSV_PATH.exists()
    with open(CSV_PATH, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        if write_header:
            writer.writeheader()
        writer.writerow(row_dict)


def safe_float(val, default=None):
    try:
        return float(val) if val not in (None, '', 'None') else default
    except (ValueError, TypeError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# HTML REPORT GENERATION
# ─────────────────────────────────────────────────────────────────────────────

RISK_COLOURS = {
    'LOW':      ('#d1fae5', '#065f46'),
    'LOW-MOD':  ('#fef9c3', '#713f12'),
    'MODERATE': ('#ffedd5', '#9a3412'),
    'HIGH':     ('#fee2e2', '#991b1b'),
    'SEVERE':   ('#ede9fe', '#4c1d95'),
}

def risk_badge(risk):
    bg, fg = RISK_COLOURS.get(risk, ('#f1f5f9', '#374151'))
    return (f'<span style="background:{bg};color:{fg};padding:2px 10px;'
            f'border-radius:12px;font-size:12px;font-weight:700;">{risk}</span>')


def build_daily_html(row, target_date, history):
    """Generate the HTML email body for the daily morning report."""
    date_str   = target_date.strftime('%A, %-d %B %Y')
    frost_flag = row['frost_flag'] == 'True' or row['frost_flag'] is True
    da_flag    = row['disease_alert'] == 'True' or row['disease_alert'] is True

    frost_banner = ''
    if frost_flag:
        frost_banner = '''
        <tr><td style="background:#0a1628;border-left:4px solid #60a5fa;
            padding:10px 16px;color:#93c5fd;font-size:13px;border-radius:4px;margin-bottom:8px;">
            ❄️ <strong>Frost recorded overnight</strong> — greens should have been checked
            before early morning activity.</td></tr>'''

    disease_banner = ''
    if da_flag:
        disease_banner = '''
        <tr><td style="background:#1a0505;border-left:4px solid #ef4444;
            padding:10px 16px;color:#fca5a5;font-size:13px;border-radius:4px;margin-bottom:8px;">
            🦠 <strong>Disease alert active yesterday</strong> — at least one disease
            reached HIGH or SEVERE risk level.</td></tr>'''

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:24px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

  <!-- HEADER -->
  <tr><td style="background:linear-gradient(135deg,#1a4a2e,#2d7a4e);padding:28px 32px;
      border-radius:12px 12px 0 0;">
    <table width="100%"><tr>
      <td style="color:white;">
        <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;
            color:#6ee7b7;margin-bottom:6px;">Wagga Wagga Country Club</div>
        <div style="font-size:22px;font-weight:700;">Morning Weather Briefing</div>
        <div style="font-size:14px;opacity:0.8;margin-top:4px;">{date_str}</div>
      </td>
      <td align="right" style="color:#6ee7b7;font-size:32px;">⛳</td>
    </tr></table>
  </td></tr>

  <!-- ALERT BANNERS -->
  {frost_banner}
  {disease_banner}

  <!-- CONDITIONS SUMMARY -->
  <tr><td style="background:white;padding:24px 32px;">
    <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;
        text-transform:uppercase;color:#64748b;margin-bottom:14px;">
        Yesterday at a Glance</div>
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="50%" style="padding:0 8px 12px 0;vertical-align:top;">
          <table width="100%" style="background:#f8fafc;border:1px solid #e2e8f0;
              border-radius:8px;padding:12px;" cellpadding="8">
            <tr><td style="font-size:11px;color:#64748b;font-weight:700;
                text-transform:uppercase;letter-spacing:1px;">Temperature</td></tr>
            <tr><td style="font-size:22px;font-weight:700;color:#1c1c1e;">
                {row['temp_max']}° / {row['temp_min']}°C</td></tr>
            <tr><td style="font-size:12px;color:#64748b;">
                Mean: {row['temp_mean']}°C &nbsp;·&nbsp; RH: {row['rh_mean']}%</td></tr>
          </table>
        </td>
        <td width="50%" style="padding:0 0 12px 8px;vertical-align:top;">
          <table width="100%" style="background:#f8fafc;border:1px solid #e2e8f0;
              border-radius:8px;padding:12px;" cellpadding="8">
            <tr><td style="font-size:11px;color:#64748b;font-weight:700;
                text-transform:uppercase;letter-spacing:1px;">Rainfall & ET</td></tr>
            <tr><td style="font-size:22px;font-weight:700;color:#1c1c1e;">
                {row['rain_mm']} mm rain</td></tr>
            <tr><td style="font-size:12px;color:#64748b;">
                ET: {row['et_mm']} mm &nbsp;·&nbsp;
                Net: {round(float(row['et_mm'] or 0) - float(row['rain_mm'] or 0), 1):+.1f} mm</td></tr>
          </table>
        </td>
      </tr>
      <tr>
        <td style="padding:0 8px 12px 0;vertical-align:top;">
          <table width="100%" style="background:#f8fafc;border:1px solid #e2e8f0;
              border-radius:8px;padding:12px;" cellpadding="8">
            <tr><td style="font-size:11px;color:#64748b;font-weight:700;
                text-transform:uppercase;letter-spacing:1px;">Wind</td></tr>
            <tr><td style="font-size:22px;font-weight:700;color:#1c1c1e;">
                Max {row['wind_max_kmh']} km/h</td></tr>
            <tr><td style="font-size:12px;color:#64748b;">
                Mean: {row['wind_mean_kmh']} km/h &nbsp;·&nbsp;
                Delta T: {row['delta_t_mean']}°C</td></tr>
          </table>
        </td>
        <td style="padding:0 0 12px 8px;vertical-align:top;">
          <table width="100%" style="background:#f8fafc;border:1px solid #e2e8f0;
              border-radius:8px;padding:12px;" cellpadding="8">
            <tr><td style="font-size:11px;color:#64748b;font-weight:700;
                text-transform:uppercase;letter-spacing:1px;">Soil Moisture</td></tr>
            <tr><td style="font-size:22px;font-weight:700;color:#1c1c1e;">
                {row['soil_zone']}</td></tr>
            <tr><td style="font-size:12px;color:#64748b;">
                7-day balance: {float(row['soil_balance_7d']):+.1f} mm</td></tr>
          </table>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- DISEASE RISK -->
  <tr><td style="background:white;padding:0 32px 24px;">
    <div style="border-top:1px solid #e2e8f0;padding-top:20px;">
    <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;
        text-transform:uppercase;color:#64748b;margin-bottom:14px;">Disease Risk</div>
    <table width="100%" cellpadding="0" cellspacing="6">
      <tr>
        <td style="padding:8px 12px;background:#f8fafc;border:1px solid #e2e8f0;
            border-radius:6px;font-size:13px;font-weight:600;">Dollar Spot</td>
        <td style="padding:8px 12px;text-align:right;">{risk_badge(row['dollar_spot_risk'])}
            &nbsp;<span style="font-size:12px;color:#64748b;">{row['dollar_spot_pct']}% probability</span></td>
      </tr>
      <tr>
        <td style="padding:8px 12px;background:#f8fafc;border:1px solid #e2e8f0;
            border-radius:6px;font-size:13px;font-weight:600;">Fusarium Patch</td>
        <td style="padding:8px 12px;text-align:right;">{risk_badge(row['fusarium_risk'])}
            &nbsp;<span style="font-size:12px;color:#64748b;">Score: {row['fusarium_score']}</span></td>
      </tr>
      <tr>
        <td style="padding:8px 12px;background:#f8fafc;border:1px solid #e2e8f0;
            border-radius:6px;font-size:13px;font-weight:600;">Brown Patch</td>
        <td style="padding:8px 12px;text-align:right;">{risk_badge(row['brown_patch_risk'])}</td>
      </tr>
      <tr>
        <td style="padding:8px 12px;background:#f8fafc;border:1px solid #e2e8f0;
            border-radius:6px;font-size:13px;font-weight:600;">Pythium Blight</td>
        <td style="padding:8px 12px;text-align:right;">{risk_badge(row['pythium_risk'])}</td>
      </tr>
    </table>
    <p style="font-size:12px;color:#64748b;margin:10px 0 0;">
        Leaf wetness hours: <strong>{row['leaf_wet_hours']}</strong>
    </p>
    </div>
  </td></tr>

  <!-- GDD -->
  <tr><td style="background:white;padding:0 32px 24px;">
    <div style="border-top:1px solid #e2e8f0;padding-top:20px;">
    <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;
        text-transform:uppercase;color:#64748b;margin-bottom:14px;">
        Growing Degree Days (7-day running)</div>
    <table width="100%" cellpadding="8" cellspacing="6">
      <tr>
        <td style="background:#e8f5ee;border:1px solid #a7f3d0;border-radius:6px;
            font-size:13px;font-weight:600;">Bentgrass (base 10°C)</td>
        <td style="background:#e8f5ee;border:1px solid #a7f3d0;border-radius:6px;
            font-size:20px;font-weight:700;color:#1a4a2e;text-align:right;">
            {row['gdd_bent_7d']} GDD</td>
      </tr>
      <tr>
        <td style="background:#fdf8ec;border:1px solid #fde68a;border-radius:6px;
            font-size:13px;font-weight:600;">Kikuyu (base 15°C)</td>
        <td style="background:#fdf8ec;border:1px solid #fde68a;border-radius:6px;
            font-size:20px;font-weight:700;color:#713f12;text-align:right;">
            {row['gdd_kik_7d']} GDD</td>
      </tr>
    </table>
    </div>
  </td></tr>

  <!-- SPRAY SUMMARY -->
  <tr><td style="background:white;padding:0 32px 24px;border-radius:0 0 12px 12px;">
    <div style="border-top:1px solid #e2e8f0;padding-top:20px;">
    <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;
        text-transform:uppercase;color:#64748b;margin-bottom:14px;">
        Spray Conditions (General Threshold)</div>
    <table width="100%" cellpadding="6" cellspacing="4">
      <tr>
        <td style="background:#d1fae5;border-radius:6px;text-align:center;
            font-size:13px;font-weight:700;color:#065f46;">
            GO<br><span style="font-size:20px;">{row['spray_go_hours']}</span> hrs</td>
        <td style="background:#fef3c7;border-radius:6px;text-align:center;
            font-size:13px;font-weight:700;color:#92400e;">
            CAUTION<br><span style="font-size:20px;">{row['spray_caution_hours']}</span> hrs</td>
        <td style="background:#fee2e2;border-radius:6px;text-align:center;
            font-size:13px;font-weight:700;color:#991b1b;">
            NO-GO<br><span style="font-size:20px;">{row['spray_nogo_hours']}</span> hrs</td>
      </tr>
    </table>
    </div>
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="padding:16px 0;text-align:center;font-size:11px;color:#94a3b8;">
    Wagga Wagga Country Club Weather Dashboard &nbsp;·&nbsp; Automated Daily Report<br>
    Data sourced from Davis WeatherLink station and Open-Meteo
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""
    return html


def build_weekly_html(history, week_end_date):
    """Generate HTML for the weekly summary email."""
    date_str = week_end_date.strftime('Week ending %A, %-d %B %Y')
    rows_html = ''
    totals    = {'rain': 0.0, 'et': 0.0, 'gdd_bent': 0.0, 'gdd_kik': 0.0}

    for row in history:
        d = row.get('date', '')
        rows_html += f"""
        <tr style="background:#f8fafc;">
          <td style="padding:8px 10px;font-size:12px;border-bottom:1px solid #e2e8f0;">{d}</td>
          <td style="padding:8px 10px;font-size:12px;border-bottom:1px solid #e2e8f0;text-align:center;">
              {row.get('temp_max','--')}/{row.get('temp_min','--')}°C</td>
          <td style="padding:8px 10px;font-size:12px;border-bottom:1px solid #e2e8f0;text-align:center;">
              {row.get('rain_mm','--')}</td>
          <td style="padding:8px 10px;font-size:12px;border-bottom:1px solid #e2e8f0;text-align:center;">
              {row.get('et_mm','--')}</td>
          <td style="padding:8px 10px;font-size:12px;border-bottom:1px solid #e2e8f0;text-align:center;">
              {row.get('gdd_bent','--')}</td>
          <td style="padding:8px 10px;font-size:12px;border-bottom:1px solid #e2e8f0;text-align:center;">
              {row.get('dollar_spot_risk','--')}</td>
          <td style="padding:8px 10px;font-size:12px;border-bottom:1px solid #e2e8f0;text-align:center;">
              {row.get('fusarium_risk','--')}</td>
          <td style="padding:8px 10px;font-size:12px;border-bottom:1px solid #e2e8f0;text-align:center;">
              {row.get('spray_go_hours','--')}h GO</td>
        </tr>"""
        totals['rain']     += safe_float(row.get('rain_mm'), 0)
        totals['et']       += safe_float(row.get('et_mm'), 0)
        totals['gdd_bent'] += safe_float(row.get('gdd_bent'), 0)
        totals['gdd_kik']  += safe_float(row.get('gdd_kik'), 0)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="padding:24px 0;">
<tr><td align="center">
<table width="660" cellpadding="0" cellspacing="0" style="max-width:660px;width:100%;">

  <tr><td style="background:linear-gradient(135deg,#1a4a2e,#2d7a4e);
      padding:28px 32px;border-radius:12px 12px 0 0;color:white;">
    <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;
        color:#6ee7b7;margin-bottom:6px;">Wagga Wagga Country Club</div>
    <div style="font-size:22px;font-weight:700;">Weekly Weather Summary</div>
    <div style="font-size:14px;opacity:0.8;margin-top:4px;">{date_str}</div>
  </td></tr>

  <tr><td style="background:white;padding:24px 32px;border-radius:0 0 12px 12px;">

    <table width="100%" cellpadding="0" cellspacing="0"
        style="border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;font-size:12px;">
      <tr style="background:#1a4a2e;color:white;">
        <th style="padding:10px;text-align:left;">Date</th>
        <th style="padding:10px;text-align:center;">Hi/Lo</th>
        <th style="padding:10px;text-align:center;">Rain mm</th>
        <th style="padding:10px;text-align:center;">ET mm</th>
        <th style="padding:10px;text-align:center;">GDD Bent</th>
        <th style="padding:10px;text-align:center;">Dollar Spot</th>
        <th style="padding:10px;text-align:center;">Fusarium</th>
        <th style="padding:10px;text-align:center;">Spray</th>
      </tr>
      {rows_html}
      <tr style="background:#e8f5ee;font-weight:700;">
        <td style="padding:10px;font-size:12px;">7-Day Total</td>
        <td></td>
        <td style="padding:10px;text-align:center;font-size:12px;">{totals['rain']:.1f} mm</td>
        <td style="padding:10px;text-align:center;font-size:12px;">{totals['et']:.1f} mm</td>
        <td style="padding:10px;text-align:center;font-size:12px;">{totals['gdd_bent']:.0f}</td>
        <td></td><td></td><td></td>
      </tr>
    </table>

    <table width="100%" cellpadding="8" cellspacing="6" style="margin-top:16px;">
      <tr>
        <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;">
            <div style="font-size:11px;color:#64748b;font-weight:700;">Total Rainfall</div>
            <div style="font-size:22px;font-weight:700;">{totals['rain']:.1f} mm</div></td>
        <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;">
            <div style="font-size:11px;color:#64748b;font-weight:700;">Total ET</div>
            <div style="font-size:22px;font-weight:700;">{totals['et']:.1f} mm</div></td>
        <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;">
            <div style="font-size:11px;color:#64748b;font-weight:700;">Water Balance</div>
            <div style="font-size:22px;font-weight:700;">{totals['rain']-totals['et']:+.1f} mm</div></td>
        <td style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:6px;">
            <div style="font-size:11px;color:#64748b;font-weight:700;">GDD Bent 7d</div>
            <div style="font-size:22px;font-weight:700;">{totals['gdd_bent']:.0f}</div></td>
      </tr>
    </table>
  </td></tr>

  <tr><td style="padding:16px 0;text-align:center;font-size:11px;color:#94a3b8;">
    Wagga Wagga Country Club Weather Dashboard &nbsp;·&nbsp; Automated Weekly Report
  </td></tr>
</table>
</td></tr>
</table>
</body></html>"""
    return html


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL SENDING
# ─────────────────────────────────────────────────────────────────────────────

def send_email(subject, html_body, recipients):
    """Send HTML email via SendGrid."""
    if not SENDGRID_API_KEY:
        print('No SendGrid API key — skipping email send.')
        return
    if not recipients or recipients == ['']:
        print('No email recipients configured.')
        return
    message = Mail(
        from_email=Email(EMAIL_FROM, 'WWCC Weather'),
        subject=subject,
        html_content=html_body,
    )
    for addr in recipients:
        addr = addr.strip()
        if addr:
            message.add_to(To(addr))
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        print(f'Email sent: {response.status_code} to {recipients}')
    except Exception as e:
        print(f'Email send error: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Yesterday in Sydney time
    now_sydney = datetime.now(tz=TZ)
    yesterday  = (now_sydney - timedelta(days=1)).date()
    print(f'Running daily report for {yesterday} (Sydney time)')

    # ── 1. Fetch data ──────────────────────────────────────────────────────
    print('Fetching Davis v2 historic data...')
    davis_records = fetch_davis_historic(yesterday)
    print(f'  {len(davis_records)} records returned')

    print('Fetching Open-Meteo archive...')
    om_data = fetch_openmeteo_archive(yesterday)

    # ── 2. Process Davis data ──────────────────────────────────────────────
    if davis_records:
        d = process_davis_records(davis_records)
    else:
        print('  Warning: No Davis records — using Open-Meteo fallback for basic stats')
        d = {k: None for k in ['temp_max','temp_min','temp_mean','rh_mean',
                                'wind_max_kmh','wind_mean_kmh','rain_mm','et_mm',
                                'pressure_mean','delta_t_mean','wet_hours',
                                'night_wet_hours','night_min','consec_rh90',
                                'spray_go','spray_caution','spray_nogo','rain_day']}
        d['rain_mm'] = 0.0
        d['et_mm']   = 0.0
        d['wet_hours'] = 0
        d['night_wet_hours'] = 0
        d['consec_rh90'] = 0
        d['spray_go'] = d['spray_caution'] = d['spray_nogo'] = 0

    # Fill gaps with Open-Meteo archive if Davis data incomplete
    if om_data and om_data.get('daily'):
        daily = om_data['daily']
        if d['temp_max'] is None and daily.get('temperature_2m_max'):
            d['temp_max'] = daily['temperature_2m_max'][0]
        if d['temp_min'] is None and daily.get('temperature_2m_min'):
            d['temp_min'] = daily['temperature_2m_min'][0]
        if d['rain_mm'] == 0 and daily.get('precipitation_sum'):
            d['rain_mm'] = daily['precipitation_sum'][0] or 0.0
        if d['et_mm'] == 0 and daily.get('et0_fao_evapotranspiration'):
            d['et_mm'] = daily['et0_fao_evapotranspiration'][0] or 0.0
        uv_max = daily.get('uv_index_max', [None])[0]
    else:
        uv_max = None

    # ── 3. Read CSV history for running totals ─────────────────────────────
    history_7 = read_csv_history(7)
    history_5 = history_7[-5:]

    # 7-day soil balance (include today)
    et_7  = sum(safe_float(r.get('et_mm'), 0) for r in history_7) + (d['et_mm'] or 0)
    rain_7 = sum(safe_float(r.get('rain_mm'), 0) for r in history_7) + (d['rain_mm'] or 0)
    balance_7 = round(et_7 - rain_7, 1)

    # 7-day GDD
    gdd_bent_today = round(gdd(d['temp_max'], d['temp_min'], 10), 1)
    gdd_kik_today  = round(gdd(d['temp_max'], d['temp_min'], 15), 1)
    gdd_bent_7d = round(sum(safe_float(r.get('gdd_bent'), 0) for r in history_7) + gdd_bent_today, 1)
    gdd_kik_7d  = round(sum(safe_float(r.get('gdd_kik'), 0) for r in history_7) + gdd_kik_today, 1)

    # Rain days in last 6 days (for Fusarium)
    rain_days_6 = sum(1 for r in history_7[-6:] if r.get('rain_day') in ('True', True, '1'))
    if d['rain_day']:
        rain_days_6 += 1

    # Smith-Kerns 5-day average
    mean_temps_5 = [safe_float(r.get('temp_mean')) for r in history_5 if r.get('temp_mean')]
    mean_rh_5    = [safe_float(r.get('rh_mean')) for r in history_5 if r.get('rh_mean')]
    if d['temp_mean']: mean_temps_5.append(d['temp_mean'])
    if d['rh_mean']:   mean_rh_5.append(d['rh_mean'])
    sk_mean_t  = sum(mean_temps_5) / len(mean_temps_5) if mean_temps_5 else None
    sk_mean_rh = sum(mean_rh_5) / len(mean_rh_5) if mean_rh_5 else None

    # ── 4. Calculate disease risk ──────────────────────────────────────────
    sk_pct    = smith_kerns(sk_mean_rh, sk_mean_t)
    ds_risk   = dollar_spot_risk(d['wet_hours'], d['temp_mean'], sk_pct)
    fus_score, fus_risk = fusarium_risk(
        d['wet_hours'], d['consec_rh90'], rain_days_6, d['temp_mean'])
    bp_risk   = brown_patch_risk(d['night_wet_hours'], d['night_min'], d['temp_max'])
    pyt_risk  = pythium_risk(d['night_wet_hours'], d['night_min'], d['temp_max'])

    high_risks   = ['HIGH', 'SEVERE']
    disease_alert = any(r in high_risks for r in [ds_risk, fus_risk, bp_risk, pyt_risk])
    frost_flag    = (d['temp_min'] is not None and d['temp_min'] < 2)

    # ── 5. Build CSV row ───────────────────────────────────────────────────
    row = {
        'date':              yesterday.isoformat(),
        'temp_max':          d['temp_max'],
        'temp_min':          d['temp_min'],
        'temp_mean':         d['temp_mean'],
        'rh_mean':           d['rh_mean'],
        'wind_max_kmh':      d['wind_max_kmh'],
        'wind_mean_kmh':     d['wind_mean_kmh'],
        'rain_mm':           d['rain_mm'],
        'et_mm':             d['et_mm'],
        'pressure_mean_hpa': d['pressure_mean'],
        'delta_t_mean':      d['delta_t_mean'],
        'uv_max':            uv_max,
        'gdd_bent':          gdd_bent_today,
        'gdd_kik':           gdd_kik_today,
        'gdd_bent_7d':       gdd_bent_7d,
        'gdd_kik_7d':        gdd_kik_7d,
        'leaf_wet_hours':    d['wet_hours'],
        'dollar_spot_pct':   sk_pct,
        'dollar_spot_risk':  ds_risk,
        'fusarium_score':    fus_score,
        'fusarium_risk':     fus_risk,
        'brown_patch_risk':  bp_risk,
        'pythium_risk':      pyt_risk,
        'soil_balance_7d':   balance_7,
        'soil_zone':         soil_zone(balance_7),
        'spray_go_hours':    d['spray_go'],
        'spray_caution_hours': d['spray_caution'],
        'spray_nogo_hours':  d['spray_nogo'],
        'rain_day':          d['rain_day'],
        'frost_flag':        frost_flag,
        'disease_alert':     disease_alert,
    }

    # ── 6. Save CSV row ────────────────────────────────────────────────────
    print('Appending row to daily_log.csv...')
    append_csv_row(row)

    # ── 7. Save HTML report to archive ────────────────────────────────────
    report_dir = REPORTS_ROOT / str(yesterday.year) / f'{yesterday.month:02d}'
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f'{yesterday.isoformat()}.html'
    history_for_report = read_csv_history(7)
    daily_html = build_daily_html(row, yesterday, history_for_report)
    report_path.write_text(daily_html, encoding='utf-8')
    print(f'Report saved: {report_path}')

    # ── 8. Send daily email to greenkeeper ────────────────────────────────
    subject = f'WWCC Morning Briefing — {yesterday.strftime("%-d %B %Y")}'
    print(f'Sending daily email: {subject}')
    send_email(subject, daily_html, EMAIL_RECIPIENTS_GK_ONLY)

    # ── 9. Weekly summary (Monday only) ───────────────────────────────────
    if now_sydney.weekday() == 0:  # Monday
        print('Monday detected — sending weekly summary...')
        week_history = read_csv_history(7)
        weekly_html  = build_weekly_html(week_history, yesterday)
        week_subject = f'WWCC Weekly Weather Summary — {yesterday.strftime("%-d %B %Y")}'
        send_email(week_subject, weekly_html, EMAIL_RECIPIENTS_ALL)

        # Save weekly report
        weekly_path = report_dir / f'{yesterday.isoformat()}-weekly.html'
        weekly_path.write_text(weekly_html, encoding='utf-8')

    # ── 10. Monthly summary (1st of month only) ───────────────────────────
    if yesterday.day == 1:
        print('1st of month — sending monthly summary...')
        month_history = read_csv_history(31)
        month_name    = (yesterday - timedelta(days=1)).strftime('%B %Y')
        monthly_html  = build_weekly_html(month_history,
                                          yesterday - timedelta(days=1))
        month_subject = f'WWCC Monthly Weather Summary — {month_name}'
        send_email(month_subject, monthly_html, EMAIL_RECIPIENTS_ALL)

    print('Done.')


if __name__ == '__main__':
    main()
