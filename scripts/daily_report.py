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
import math
import json
import time
import logging
import calendar
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from pathlib import Path
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail, To, Email

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION - loaded from GitHub Secrets (environment variables)
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
# Greenkeeper addresses (daily + weekly + monthly + annual)
EMAIL_GK_RECIPIENTS = [
    a.strip() for a in os.environ.get('EMAIL_GK_RECIPIENTS', '').split(',') if a.strip()
]
# Committee addresses (weekly + monthly + annual only)
EMAIL_COMMITTEE_RECIPIENTS = [
    a.strip() for a in os.environ.get('EMAIL_COMMITTEE_RECIPIENTS', '').split(',') if a.strip()
]
EMAIL_RECIPIENTS_ALL     = EMAIL_GK_RECIPIENTS + EMAIL_COMMITTEE_RECIPIENTS
EMAIL_RECIPIENTS_GK_ONLY = EMAIL_GK_RECIPIENTS

CLUB_LAT = -35.1082
CLUB_LON =  147.3598
TZ       = ZoneInfo('Australia/Sydney')

def _fmt_d(dt, fmt):
    """Cross-platform strftime: %-d → no-pad day, %-I → no-pad 12-hour (Linux % modifier not supported on Windows)."""
    s = fmt.replace('%-d', str(dt.day))
    if '%-I' in s and hasattr(dt, 'hour'):
        s = s.replace('%-I', str(dt.hour % 12 or 12))
    return dt.strftime(s)

# Paths (relative to repo root, where the script runs from)
CSV_PATH          = Path('data/daily_log.csv')
REPORTS_ROOT      = Path('data/reports')
ZONE_HISTORY_CSV  = Path('data/lake_zone_history.csv')

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
    'fog_forecast',
    'lightning_forecast',
    'tank_pct',
    'tank_volume_l',
    'tank_used_l',
    'tank_refill',
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


def _calc_air_density(temp_c, rh_pct, sea_level_hpa, elev_m=0.0):
    """Moist air density (kg/m³) from temperature, RH and pressure.

    Partial-pressure form of the ideal gas law (Rd = 287.05,
    Rv = 461.495 J/kg.K); saturation vapour pressure via the Magnus formula
    (Alduchov & Eskridge 1996). elev_m defaults to 0 because the retired
    Davis air_density field used the sea-level-corrected barometer reading
    directly - validated against all 3,674 pre-Sep-2025 rows (mean error
    -0.5%, stdev 0.13%), so derived values remain comparable with history.
    """
    t_k = temp_c + 273.15
    station_hpa = sea_level_hpa * math.exp(-elev_m / (29.263 * t_k)) if elev_m else sea_level_hpa
    es_hpa = 6.112 * math.exp(17.62 * temp_c / (243.12 + temp_c))
    e_pa   = (rh_pct / 100.0) * es_hpa * 100.0
    pd_pa  = station_hpa * 100.0 - e_pa
    return pd_pa / (287.05 * t_k) + e_pa / (461.495 * t_k)


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
    """CART leaf wetness model - matches dashboard logic exactly."""
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
    """Fusarium Patch scoring - matches dashboard exactly."""
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
    if night_wet_hours >= 12 and (night_min or 0) > 20 and (day_temp or 0) > 30:
        return 'SEVERE'
    elif night_wet_hours >= 10 and (night_min or 0) > 20:
        return 'HIGH'
    elif night_wet_hours >= 6 and (night_min or 0) > 20:
        return 'MODERATE'
    return 'LOW'


# ─────────────────────────────────────────────────────────────────────────────
# DAVIS WEATHERLINK v2 API
# ─────────────────────────────────────────────────────────────────────────────

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

    url = f'https://api.weatherlink.com/v2/historic/{DAVIS_V2_STATION}'
    params = {
        'api-key':         DAVIS_V2_KEY,
        'start-timestamp': str(start_ts),
        'end-timestamp':   str(end_ts),
    }
    headers = {'X-Api-Secret': DAVIS_V2_SECRET}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f'Davis v2 API error: {e}')
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
    temps_c, night_temps, rh_vals, wind_kmh_vals, rain_total, et_total = [], [], [], [], 0.0, 0.0
    temps_ct = []  # (temp_c, rec_dt) pairs for tracking time of max/min
    pressures   = []
    uv_vals     = []   # uv_index_hi (daily max)
    uv_avg_vals = []   # uv_index_avg
    uv_dose_total = 0.0
    uv_series   = []   # (rec_dt, uv) pairs for dose integration fallback
    wind_dirs   = []   # wind_dir_of_prevail (degrees) for circular mean
    wind_run_total = 0.0   # miles, will convert to km
    rain_rate_hi_vals = []  # rain_rate values in mm/hr
    solar_rad_vals  = []   # solar_rad_avg (W/m²)
    solar_rad_hi_vals = [] # solar_rad_hi (W/m²)
    solar_energy_total = 0.0  # Langleys per interval, summed
    dew_point_vals  = []   # dew_point_out (°F → °C)
    wet_bulb_vals   = []   # wet_bulb (°F → °C)
    heat_index_vals = []   # heat_index_out (°F → °C)
    wind_chill_vals = []   # wind_chill (°F → °C)
    thsw_vals       = []   # thsw_index (°F → °C)
    emc_vals        = []   # equilibrium moisture content (%)
    air_density_vals = []  # air_density (lb/ft³ → kg/m³)
    cloud_cover_vals = []  # night_cloud_cover (0–1)
    reception_vals   = []  # iss_reception (%)
    hourly = {}  # hour_int -> {'wet': bool, 'night': bool, 'rh': float}

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

        # Wind speed (mph -> m/s for leaf-wetness model; mph -> km/h for reporting)
        ws_mph  = rec.get('wind_speed_avg') or rec.get('wind_speed_last')
        ws_ms   = float(ws_mph) * 0.44704 if ws_mph is not None else None
        ws_kmh  = float(ws_mph) * 1.60934 if ws_mph is not None else None

        # Wind direction (degrees)
        wd = rec.get('wind_dir_of_prevail') or rec.get('wind_dir_of_hi')
        if wd is not None:
            wind_dirs.append(float(wd))

        # Wind run (miles per archive interval)
        wr = rec.get('wind_run')
        if wr is not None:
            wind_run_total += float(wr)

        # Rain
        rain_mm = float(rec.get('rainfall_mm') or rec.get('rain_mm') or 0)

        # Peak rain rate - Davis returns mm/hr directly as rain_rate_hi_mm
        rr_mm = rec.get('rain_rate_hi_mm')
        if rr_mm is not None and float(rr_mm) > 0:
            rain_rate_hi_vals.append(float(rr_mm))
        else:
            rr_in = rec.get('rain_rate_hi_in') or rec.get('rain_rate_hi')
            if rr_in is not None and float(rr_in) > 0:
                rain_rate_hi_vals.append(float(rr_in) * 25.4)

        # ET (inches -> mm)
        et_in = rec.get('et') or 0
        et_mm = float(et_in) * 25.4

        # Solar radiation (W/m²)
        sr_avg = rec.get('solar_rad_avg') or rec.get('solar_rad')
        if sr_avg is not None:
            solar_rad_vals.append(float(sr_avg))
        sr_hi = rec.get('solar_rad_hi')
        if sr_hi is not None:
            solar_rad_hi_vals.append(float(sr_hi))

        # Solar energy (Langleys per interval, cumulative daily)
        se = rec.get('solar_energy')
        if se is not None:
            solar_energy_total += float(se)

        # Dew point (°F -> °C)
        dp_f = rec.get('dew_point_out') or rec.get('dew_point')
        if dp_f is not None:
            dew_point_vals.append(f_to_c(float(dp_f)))

        # Wet bulb (°F -> °C)
        wb_f = rec.get('wet_bulb')
        if wb_f is not None:
            wet_bulb_vals.append(f_to_c(float(wb_f)))

        # Heat index (°F -> °C)
        hi_f = rec.get('heat_index_out') or rec.get('heat_index')
        if hi_f is not None:
            heat_index_vals.append(f_to_c(float(hi_f)))

        # Wind chill (°F -> °C)
        wc_f = rec.get('wind_chill') or rec.get('wind_chill_last')
        if wc_f is not None:
            wind_chill_vals.append(f_to_c(float(wc_f)))

        # THSW index (°F -> °C)
        thsw_f = rec.get('thsw_index')
        if thsw_f is not None:
            thsw_vals.append(f_to_c(float(thsw_f)))

        # UV Index (historic records use uv_index_hi; current conditions use uv_index)
        uv_hi = rec.get('uv_index_hi') or rec.get('uv_index')
        if uv_hi is not None:
            uv_vals.append(float(uv_hi))
        uv_avg = rec.get('uv_index_avg')
        if uv_avg is not None:
            uv_avg_vals.append(float(uv_avg))
        # Keep a timestamped series so UV dose can be integrated below
        # (prefer the interval average; fall back to the interval high)
        if uv_avg is not None or uv_hi is not None:
            uv_series.append((rec_dt, float(uv_avg if uv_avg is not None else uv_hi)))

        # UV dose (MEDs per archive interval, cumulative daily)
        uv_d = rec.get('uv_dose')
        if uv_d is not None:
            uv_dose_total += float(uv_d)

        # Equilibrium moisture content (%)
        emc_v = rec.get('emc')
        if emc_v is not None:
            emc_vals.append(float(emc_v))

        # Air density (lb/ft³ -> kg/m³)
        ad = rec.get('air_density')
        if ad is not None:
            air_density_vals.append(float(ad) * 16.0185)

        # Night cloud cover (fraction 0–1)
        cc = rec.get('night_cloud_cover')
        if cc is not None:
            cloud_cover_vals.append(float(cc))

        # ISS reception (%)
        rx = rec.get('iss_reception')
        if rx is not None:
            reception_vals.append(float(rx))

        # Pressure (inHg -> hPa)
        bar_in = rec.get('bar_sea_level_in') or rec.get('bar_in') or rec.get('bar')
        if bar_in:
            pressures.append(float(bar_in) * 33.8639)

        if t_c is not None:
            temps_c.append(t_c)
            temps_ct.append((t_c, rec_dt))
        if rh   is not None: rh_vals.append(rh)
        if ws_kmh is not None: wind_kmh_vals.append(ws_kmh)
        rain_total += rain_mm
        et_total   += et_mm

        # Leaf wetness per hour (CART model)
        wet   = is_cart_wet(t_c, rh, ws_ms, rain_mm)
        night = hour >= 20 or hour < 8

        # Track overnight temperatures separately for accurate night_min
        if night and t_c is not None:
            night_temps.append(t_c)
        if hour not in hourly:
            hourly[hour] = {'wet': wet, 'night': night, 'rh': rh or 0}
        else:
            hourly[hour]['wet']   = hourly[hour]['wet'] or wet
            hourly[hour]['night'] = night
            hourly[hour]['rh']    = max(hourly[hour]['rh'], rh or 0)

    # ── Daily summaries ────────────────────────────────────────────────────
    temp_max  = round(max(temps_c), 1)               if temps_c        else None
    temp_min  = round(min(temps_c), 1)               if temps_c        else None
    if temps_ct:
        temp_max_time = _fmt_d(max(temps_ct, key=lambda x: x[0])[1], '%-I:%M %p')
        temp_min_time = _fmt_d(min(temps_ct, key=lambda x: x[0])[1], '%-I:%M %p')
    else:
        temp_max_time = None
        temp_min_time = None
    temp_mean = round(sum(temps_c)/len(temps_c), 1)  if temps_c        else None
    rh_mean   = round(sum(rh_vals)/len(rh_vals), 1)  if rh_vals        else None
    wind_max  = round(max(wind_kmh_vals), 1)          if wind_kmh_vals  else None
    wind_mean = round(sum(wind_kmh_vals)/len(wind_kmh_vals), 1) if wind_kmh_vals else None
    pres_mean = round(sum(pressures)/len(pressures), 1) if pressures   else None
    uv_max_davis  = round(max(uv_vals), 1)           if uv_vals        else None
    uv_avg_daily  = round(sum(uv_avg_vals)/len(uv_avg_vals), 2) if uv_avg_vals else None
    # The API almost never supplies uv_index_avg / uv_dose directly, so derive
    # them from the interval readings when absent:
    #   avg  = mean of the interval values
    #   dose = integral of erythemal irradiance (1 UV index = 25 mW/m²),
    #          expressed in MEDs (1 MED = 210 J/m² erythemal)
    if uv_avg_daily is None and uv_vals:
        uv_avg_daily = round(sum(uv_vals) / len(uv_vals), 2)
    uv_dose_daily = round(uv_dose_total, 2) if uv_dose_total > 0 else None
    if uv_dose_daily is None and len(uv_series) >= 2:
        uv_series.sort(key=lambda p: p[0])
        gaps = [(uv_series[i + 1][0] - uv_series[i][0]).total_seconds()
                for i in range(len(uv_series) - 1)]
        gaps = [g for g in gaps if 0 < g <= 3600]
        interval_s = sorted(gaps)[len(gaps) // 2] if gaps else 300.0  # median, default 5 min
        dose_j_m2 = sum(uv * 0.025 * interval_s for _, uv in uv_series)
        if dose_j_m2 > 0:
            uv_dose_daily = round(dose_j_m2 / 210.0, 2)  # J/m² -> MEDs

    # Wind direction: circular mean to handle 360/0 wrap-around.
    # On calm days the vane reading is meaningless (it idles at 0 = north and
    # would skew monthly prevailing-direction stats), so record no direction
    # when the daily mean wind is under 2 km/h.
    if wind_dirs and wind_mean is not None and wind_mean >= 2:
        sin_sum = sum(math.sin(math.radians(d)) for d in wind_dirs)
        cos_sum = sum(math.cos(math.radians(d)) for d in wind_dirs)
        wind_dir_mean = round((math.degrees(math.atan2(sin_sum, cos_sum)) + 360) % 360)
    else:
        wind_dir_mean = None

    wind_run_km_daily   = round(wind_run_total * 1.60934, 1) if wind_run_total > 0 else None
    rain_rate_max_mmhr  = round(max(rain_rate_hi_vals), 1) if rain_rate_hi_vals else None
    solar_rad_avg_daily = round(sum(solar_rad_vals)/len(solar_rad_vals), 1) if solar_rad_vals else None
    solar_rad_hi_daily  = round(max(solar_rad_hi_vals), 0)  if solar_rad_hi_vals  else None
    solar_energy_daily  = round(solar_energy_total, 2)      if solar_energy_total > 0 else None
    dew_point_mean      = round(sum(dew_point_vals)/len(dew_point_vals), 1) if dew_point_vals else None
    wet_bulb_mean       = round(sum(wet_bulb_vals)/len(wet_bulb_vals), 1)   if wet_bulb_vals  else None
    heat_index_mean     = round(sum(heat_index_vals)/len(heat_index_vals), 1) if heat_index_vals else None
    wind_chill_min      = round(min(wind_chill_vals), 1)    if wind_chill_vals    else None
    thsw_max            = round(max(thsw_vals), 1)          if thsw_vals          else None
    emc_mean            = round(sum(emc_vals)/len(emc_vals), 1) if emc_vals       else None
    air_density_mean    = round(sum(air_density_vals)/len(air_density_vals), 4) if air_density_vals else None
    # The API stopped supplying air_density on 2025-08-31 - compute moist air
    # density from station temp / RH / pressure instead (partial-pressure form
    # of the ideal gas law; matches the Davis field to ~0.1%).
    if air_density_mean is None and temp_mean is not None and rh_mean is not None and pres_mean is not None:
        air_density_mean = round(_calc_air_density(temp_mean, rh_mean, pres_mean), 4)
    cloud_cover_mean    = round(sum(cloud_cover_vals)/len(cloud_cover_vals), 2) if cloud_cover_vals else None
    reception_mean      = round(sum(reception_vals)/len(reception_vals), 1)     if reception_vals  else None

    # Delta T: use real wet bulb from Davis if available; otherwise Stull (2011) estimate
    if wet_bulb_mean is not None and temp_mean is not None:
        dt_mean = round(temp_mean - wet_bulb_mean, 1)
    else:
        dt_vals = [delta_t(t, r) for t, r in zip(temps_c, rh_vals) if t and r]
        dt_mean = round(sum(dt_vals)/len(dt_vals), 1) if dt_vals else None

    wet_hours       = sum(1 for h in hourly.values() if h['wet'])
    night_wet_hours = sum(1 for h in hourly.values() if h['wet'] and h['night'])
    # True overnight minimum (8 PM–8 AM only); falls back to daily minimum if no night data
    night_min       = round(min(night_temps), 1) if night_temps else temp_min
    rh90_hours      = sum(1 for h in hourly.values() if h['rh'] >= 90)
    consec_rh90     = rh90_hours  # conservative - full consecutive calc needs ordering

    # Spray condition hours
    spray_counts = {'GO': 0, 'CAUTION': 0, 'NO-GO': 0}
    for h_idx, hdata in hourly.items():
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
        'temp_max':          temp_max,
        'temp_min':          temp_min,
        'temp_max_time':     temp_max_time,
        'temp_min_time':     temp_min_time,
        'temp_mean':         temp_mean,
        'rh_mean':           rh_mean,
        'wind_max_kmh':      wind_max,
        'wind_mean_kmh':     wind_mean,
        'wind_dir_deg':      wind_dir_mean,
        'wind_run_km':       wind_run_km_daily,
        'rain_mm':           round(rain_total, 1),
        'et_mm':             round(et_total, 2),
        'rain_rate_max_mmhr': rain_rate_max_mmhr,
        'pressure_mean':     pres_mean,
        'solar_rad_avg':     solar_rad_avg_daily,
        'solar_rad_hi':      solar_rad_hi_daily,
        'solar_energy_ly':   solar_energy_daily,
        'dew_point_c':       dew_point_mean,
        'wet_bulb_c':        wet_bulb_mean,
        'heat_index_c':      heat_index_mean,
        'wind_chill_c':      wind_chill_min,
        'thsw_index_c':      thsw_max,
        'delta_t_mean':      dt_mean,
        'uv_max':            uv_max_davis,
        'uv_index_avg':      uv_avg_daily,
        'uv_dose':           uv_dose_daily,
        'emc':               emc_mean,
        'air_density_kgm3':  air_density_mean,
        'night_cloud_cover': cloud_cover_mean,
        'iss_reception':     reception_mean,
        'wet_hours':         wet_hours,
        'night_wet_hours':   night_wet_hours,
        'night_min':         night_min,
        'consec_rh90':       consec_rh90,
        'spray_go':          spray_counts['GO'],
        'spray_caution':     spray_counts['CAUTION'],
        'spray_nogo':        spray_counts['NO-GO'],
        'rain_day':          rain_total > 0.2,
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
        'daily':      'temperature_2m_max,temperature_2m_min,precipitation_sum,et0_fao_evapotranspiration',
        'hourly':     'temperature_2m,relative_humidity_2m,windspeed_10m,precipitation',
        'timezone':   'Australia/Sydney',
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f'Open-Meteo archive error: {e}')
        return None


def fetch_openmeteo_uv(target_date, want_detail=False):
    """Open-Meteo UV for a date (used because the Davis archive endpoint
    carries no UV fields).

    Returns uv_index_max, or with want_detail=True a dict:
      { 'max': float, 'avg': float, 'dose_med': float }
    avg  = mean UV index across the sunlit hours (UV > 0.1)
    dose = hourly UV integrated as erythemal irradiance
           (1 UV index = 25 mW/m²; 1 MED = 210 J/m²)
    """
    date_str = target_date.strftime('%Y-%m-%d')
    # historical-forecast-api has UV data; archive-api (ERA5) does not
    url = 'https://historical-forecast-api.open-meteo.com/v1/forecast'
    params = {
        'latitude':   CLUB_LAT,
        'longitude':  CLUB_LON,
        'start_date': date_str,
        'end_date':   date_str,
        'daily':      'uv_index_max',
        'hourly':     'uv_index',
        'timezone':   'Australia/Sydney',
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        uv_max = (data.get('daily') or {}).get('uv_index_max', [None])[0]
        if not want_detail:
            return uv_max
        hourly = [v for v in (data.get('hourly') or {}).get('uv_index', []) if v is not None]
        sunlit = [v for v in hourly if v > 0.1]
        avg  = round(sum(sunlit) / len(sunlit), 2) if sunlit else None
        dose = round(sum(v * 0.025 * 3600 for v in hourly) / 210.0, 2) if hourly else None
        return {'max': uv_max, 'avg': avg, 'dose_med': dose}
    except Exception as e:
        log.warning(f'Open-Meteo UV fetch error: {e}')
        return None if not want_detail else {'max': None, 'avg': None, 'dose_med': None}


# WMO weather code sets used for alert detection
_FOG_CODES   = {45, 48}
_STORM_CODES = {95, 96, 99}


def fetch_openmeteo_forecast(target_date):
    """
    Fetch today's hourly weather_code forecast from Open-Meteo.
    Returns (fog_flag, lightning_flag):
      fog_flag       - fog forecast during morning hours (5–10 AM)
      lightning_flag - thunderstorm forecast during daytime hours (6 AM–8 PM)
    """
    date_str = target_date.strftime('%Y-%m-%d')
    url = 'https://api.open-meteo.com/v1/forecast'
    params = {
        'latitude':   CLUB_LAT,
        'longitude':  CLUB_LON,
        'hourly':     'weather_code',
        'start_date': date_str,
        'end_date':   date_str,
        'timezone':   'Australia/Sydney',
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f'Open-Meteo forecast error: {e}')
        return False, False

    codes = data.get('hourly', {}).get('weather_code', [])
    fog_flag       = any(codes[h] in _FOG_CODES   for h in range(5, 11)  if h < len(codes))
    lightning_flag = any(codes[h] in _STORM_CODES for h in range(6, 21)  if h < len(codes))
    return fog_flag, lightning_flag


# WMO weather-code → icon and description (used in 4-day forecast strip)
_WMO_ICON = {
    0:'&#9728;', 1:'&#9728;', 2:'&#9925;', 3:'&#9925;',
    45:'&#127787;', 48:'&#127787;',
    51:'&#127783;', 53:'&#127783;', 55:'&#127783;',
    61:'&#127783;', 63:'&#127783;', 65:'&#127783;',
    71:'&#10052;', 73:'&#10052;', 75:'&#10052;',
    80:'&#127783;', 81:'&#127783;', 82:'&#127783;',
    95:'&#9928;',  96:'&#9928;',  99:'&#9928;',
}
_WMO_DESC = {
    0:'Clear', 1:'Mostly clear', 2:'Partly cloudy', 3:'Overcast',
    45:'Foggy', 48:'Foggy',
    51:'Drizzle', 53:'Drizzle', 55:'Heavy drizzle',
    61:'Shower', 63:'Rain', 65:'Heavy rain',
    71:'Snow', 73:'Snow', 75:'Heavy snow',
    80:'Shower', 81:'Showers', 82:'Heavy showers',
    95:'Thunderstorm', 96:'Thunderstorm', 99:'Thunderstorm',
}


def fetch_5day_forecast(today_date):
    """
    Fetch daily max/min temp, precipitation sum and dominant weather code
    for today + 4 days from Open-Meteo forecast API.
    Returns a list of up to 5 dicts; empty list on failure.
    """
    end_date = today_date + timedelta(days=4)
    url = 'https://api.open-meteo.com/v1/forecast'
    params = {
        'latitude':   CLUB_LAT,
        'longitude':  CLUB_LON,
        'daily':      'temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code',
        'start_date': today_date.strftime('%Y-%m-%d'),
        'end_date':   end_date.strftime('%Y-%m-%d'),
        'timezone':   'Australia/Sydney',
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f'4-day forecast error: {e}')
        return []

    daily  = data.get('daily', {})
    dates  = daily.get('time', [])
    maxes  = daily.get('temperature_2m_max', [])
    mins   = daily.get('temperature_2m_min', [])
    precip = daily.get('precipitation_sum', [])
    codes  = daily.get('weather_code', [])

    days = []
    for i, d_str in enumerate(dates):
        code = int(codes[i])  if i < len(codes)  and codes[i]  is not None else 0
        mn   = float(mins[i]) if i < len(mins)   and mins[i]   is not None else None
        mx   = float(maxes[i])if i < len(maxes)  and maxes[i]  is not None else None
        pr   = float(precip[i])if i < len(precip) and precip[i] is not None else 0.0
        dt   = datetime.strptime(d_str, '%Y-%m-%d').date()
        days.append({
            'date':       dt,
            'label':      _fmt_d(dt, '%a %-d %b'),
            'max_c':      round(mx, 0) if mx is not None else None,
            'min_c':      round(mn, 0) if mn is not None else None,
            'precip_mm':  round(pr, 1),
            'icon':       _WMO_ICON.get(code, '&#9925;'),
            'desc':       _WMO_DESC.get(code, 'Variable'),
            'frost_risk': mn is not None and mn <= 2.0,
        })
    return days


def fetch_hourly_forecast(today_date):
    """
    Fetch hourly weather for today from Open-Meteo forecast API.
    Returns a list of dicts for hours 6–18 (6 AM to 6 PM), each:
      {'hour': int, 'label': '6 AM', 'temp_c': float, 'precip_pct': int,
       'wind_kmh': float, 'icon': str, 'desc': str}
    Returns empty list on failure.
    """
    date_str = today_date.strftime('%Y-%m-%d')
    url = 'https://api.open-meteo.com/v1/forecast'
    params = {
        'latitude':   CLUB_LAT,
        'longitude':  CLUB_LON,
        'hourly':     'temperature_2m,relative_humidity_2m,precipitation_probability,weather_code,wind_speed_10m',
        'start_date': date_str,
        'end_date':   date_str,
        'timezone':   'Australia/Sydney',
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f'Hourly forecast error: {e}')
        return []

    hourly = data.get('hourly', {})
    times     = hourly.get('time', [])
    temps     = hourly.get('temperature_2m', [])
    rhs       = hourly.get('relative_humidity_2m', [])
    precip    = hourly.get('precipitation_probability', [])
    codes     = hourly.get('weather_code', [])
    winds     = hourly.get('wind_speed_10m', [])

    results = []
    for i, t_str in enumerate(times):
        # t_str is like "2026-07-08T06:00"
        try:
            hour = int(t_str[11:13])
        except Exception:
            continue
        if hour < 6 or hour > 18:
            continue
        code = int(codes[i]) if i < len(codes) and codes[i] is not None else 0
        label_hour = hour % 12 or 12
        am_pm = 'AM' if hour < 12 else 'PM'
        results.append({
            'hour':       hour,
            'label':      f'{label_hour} {am_pm}',
            'temp_c':     float(temps[i]) if i < len(temps) and temps[i] is not None else None,
            'rh':         float(rhs[i])   if i < len(rhs) and rhs[i] is not None else None,
            'precip_pct': int(precip[i])  if i < len(precip) and precip[i] is not None else 0,
            'wind_kmh':   float(winds[i]) if i < len(winds) and winds[i] is not None else None,
            'icon':       _WMO_ICON.get(code, '&#9925;'),
            'desc':       _WMO_DESC.get(code, 'Variable'),
        })
    return results


def _disease_outlook(max_c, min_c, precip_mm):
    """
    Return categorical disease risk estimates from forecast temperature and
    precipitation. Used to populate the today/tomorrow columns of the disease
    table in build_daily_html().
    """
    mean_c = ((max_c or 0) + (min_c or 0)) / 2 if max_c is not None and min_c is not None else None

    # Dollar Spot: warm mean temps with moisture
    if mean_c is None:
        ds = '--'
    elif mean_c < 12:
        ds = 'LOW'
    elif mean_c < 18:
        ds = 'LOW-MOD' if precip_mm > 0 else 'LOW'
    elif mean_c < 24:
        ds = 'MODERATE'
    else:
        ds = 'HIGH'

    # Fusarium Patch: cool moist conditions 4-22 C
    if min_c is None or max_c is None:
        fs = '--'
    elif 4 <= (min_c or 0) <= 18 and (max_c or 0) <= 22:
        fs = 'MODERATE' if (precip_mm > 0 or (min_c or 0) < 10) else 'LOW-MOD'
    elif (min_c or 0) < 4 or (max_c or 0) < 8:
        fs = 'LOW-MOD'
    else:
        fs = 'LOW'

    # Brown Patch: warm humid nights above 20 C
    bp = 'MODERATE' if min_c is not None and min_c >= 20 else 'LOW'

    # Pythium Blight: hot days above 30 C with moisture
    py = 'MODERATE' if max_c is not None and max_c >= 30 and precip_mm > 0 else 'LOW'

    return {'dollar_spot': ds, 'fusarium': fs, 'brown_patch': bp, 'pythium': py}


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
        return float(val) if val is not None and str(val).strip() not in ('', 'None') else default
    except (ValueError, TypeError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# FARMBOT TANK HELPERS
# ─────────────────────────────────────────────────────────────────────────────

FARMBOT_LATEST_JSON   = Path('data/farmbot_latest.json')
FARMBOT_READINGS_JSON = Path('data/farmbot_readings.json')

def load_farmbot_snapshot():
    """Return the latest farmbot_latest.json dict, or {} if unavailable."""
    try:
        if FARMBOT_LATEST_JSON.exists():
            return json.loads(FARMBOT_LATEST_JSON.read_text())
    except Exception as e:
        log.warning(f'Could not load farmbot_latest.json: {e}')
    return {}

def load_farmbot_history_day(target_date_iso):
    """Derive daily tank summary for target_date_iso (YYYY-MM-DD) from farmbot_readings.json."""
    target = date.fromisoformat(target_date_iso)
    try:
        if not FARMBOT_READINGS_JSON.exists():
            log.warning('farmbot_readings.json not found')
            return {}
        readings = json.loads(FARMBOT_READINGS_JSON.read_text())
        day_readings = []
        for r in readings:
            ts = datetime.fromisoformat(r['date'].replace('Z', '+00:00')).astimezone(TZ)
            if ts.date() == target and r.get('pct') is not None:
                day_readings.append((ts, r['pct']))
        if not day_readings:
            log.warning(f'No farmbot readings found for {target_date_iso}')
            return {}
        day_readings.sort(key=lambda x: x[0])
        pcts        = [p for _, p in day_readings]
        morning_pct = pcts[0]
        evening_pct = pcts[-1]
        used_pct    = max(0.0, morning_pct - evening_pct)
        used_l      = round(used_pct / 100 * 250000)
        refill      = evening_pct > morning_pct + 2
        return {
            'date':        target_date_iso,
            'morning_pct': morning_pct,
            'evening_pct': evening_pct,
            'min_pct':     round(min(pcts), 1),
            'max_pct':     round(max(pcts), 1),
            'used_l':      used_l,
            'used_pct':    round(used_pct, 1),
            'refill':      refill,
            'readings':    len(day_readings),
        }
    except Exception as e:
        log.warning(f'Could not compute farmbot day summary: {e}')
        return {}


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
    return (f'<span style="background:{bg};color:{fg};padding:3px 10px;'
            f'border-radius:20px;font-size:11px;font-weight:700;display:inline-block;">{risk}</span>')


RISK_ROW_BG = {
    'LOW':      '#f0fdf4',
    'LOW-MOD':  '#fefce8',
    'MODERATE': '#fff7ed',
    'HIGH':     '#fef2f2',
    'SEVERE':   '#f5f3ff',
}

# Shared inline style fragments
# Note: linear-gradient is stripped by most email clients - use solid background-color
# and the bgcolor HTML attribute (Outlook reads the attribute, not the CSS property).
_EMAIL_MOBILE_STYLE = """<style type="text/css">
@media only screen and (max-width:620px) {
  table[width="600"] { width:100% !important; max-width:100% !important; }
  td[style*="padding:36px 28px"], td[style*="padding:36px 28px 30px"] { padding:20px 16px !important; }
  .mob-block { display:block !important; width:100% !important; padding:0 0 8px 0 !important; }
  .mob-full  { width:100% !important; }
  .mob-logo-cell { display:block !important; width:100% !important; text-align:center !important; padding-right:0 !important; padding-bottom:14px !important; }
  .mob-logo-cell img { height:44px !important; width:auto !important; max-width:100% !important; }
  .mob-text-cell { display:block !important; width:100% !important; }
}
</style>"""

_HDR = ('background-color:#1a4a2e;'
        'padding:18px 24px;')
_CIRCLE = ('display:inline-block;width:32px;height:32px;line-height:32px;'
           'text-align:center;border-radius:50%;'
           'background-color:rgba(255,255,255,0.2);'
           'color:white;font-weight:700;font-size:14px;'
           'margin-right:10px;vertical-align:middle;')
_HDR_TXT = 'color:white;font-size:16px;font-weight:700;vertical-align:middle;'
_HDR_SUB = 'color:rgba(255,255,255,0.8);font-size:12px;margin-top:3px;'
def sec_header(num, title, subtitle=''):
    """
    Dark-green section header, fully Outlook-compatible.

    The numbered circle is a nested <table> cell with HTML width/height/bgcolor
    attributes so Outlook renders it as a coloured box (no rounding in Outlook,
    rounded in Gmail/Apple Mail via border-radius on the <td>).
    rgba() is replaced with a solid mid-green (#2d7a4e) so Outlook doesn't
    ignore it. The number and title sit in a 2-column inner table so they
    align vertically without relying on inline-block or line-height tricks.
    """
    sub_html = (f'<tr><td colspan="2" style="padding:4px 0 0 0;'
                f'color:#a8d8bc;font-size:12px;">{subtitle}</td></tr>'
                if subtitle else '')
    return f"""<tr><td height="16" style="height:16px;font-size:0;line-height:0;background:white;mso-line-height-rule:exactly;">&nbsp;</td></tr>
<tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:16px 24px;
    border-radius:10px 10px 0 0;mso-border-top-left-radius:0;mso-border-top-right-radius:0;">
      <table cellpadding="0" cellspacing="0" width="100%">
        <tr>
          <!--[if mso]><td width="44" valign="middle" style="width:44px;"><v:oval xmlns:v="urn:schemas-microsoft-com:vml" style="width:32px;height:32px;" fillcolor="#2d7a4e" strokecolor="#2d7a4e"><v:textbox inset="0px,7px,0px,0px"><center style="font-family:Arial,sans-serif;font-size:16px;font-weight:bold;color:white;">{num}</center></v:textbox></v:oval></td><![endif]-->
          <!--[if !mso]><!-->
          <td width="44" valign="middle" style="width:44px;">
            <table cellpadding="0" cellspacing="0">
              <tr><td width="32" height="32" bgcolor="#2d7a4e" align="center" valign="middle"
                  style="width:32px;height:32px;background-color:#2d7a4e;border-radius:50%;
                         color:white;font-size:16px;font-weight:700;text-align:center;
                         line-height:32px;mso-line-height-rule:exactly;">
                  {num}
              </td></tr>
            </table>
          </td>
          <!--<![endif]-->
          <td valign="middle"
              style="color:white;font-size:16px;font-weight:700;padding-left:2px;">{title}</td>
        </tr>
        {sub_html}
      </table>
    </td></tr>"""


# ── GK email kv-table helper ─────────────────────────────────────────────────
_GK_ROW_A      = '#f0f7f2'   # very light green
_GK_ROW_B      = '#ffffff'   # white
_GK_BORDER     = '#d1e7d8'   # light green border
_GK_LABEL_COL  = '#1a4a2e'   # dark green label
_GK_VAL_COL    = '#111827'   # near-black value


def _gk_kv_table(rows):
    """rows = [(label, value_html), ...]. Alternating green-tinted rows."""
    cells = ''
    for i, (k, v) in enumerate(rows):
        bg = _GK_ROW_A if i % 2 == 0 else _GK_ROW_B
        cells += f"""
  <tr>
    <td bgcolor="{bg}" style="background-color:{bg};padding:9px 16px;width:200px;
        font-family:Arial,sans-serif;font-size:13px;font-weight:700;color:{_GK_LABEL_COL};
        border-bottom:1px solid {_GK_BORDER};">{k}</td>
    <td bgcolor="{bg}" style="background-color:{bg};padding:9px 16px;
        font-family:Arial,sans-serif;font-size:13px;color:{_GK_VAL_COL};
        border-bottom:1px solid {_GK_BORDER};">{v}</td>
  </tr>"""
    return f"""<table width="100%" cellpadding="0" cellspacing="0"
    style="border-collapse:collapse;border:1px solid {_GK_BORDER};border-radius:8px;overflow:hidden;">
{cells}
</table>"""


# ─────────────────────────── LAKE ALBERT HELPERS ────────────────────────────

LAKE_SURFACE_M2 = 1_202_046      # m² - matches lake-albert.html
LAKE_VOL_BOTTOM = 188.1          # physical lake bed AHD - matches lake-albert.html
LAKE_FULL_AHD_V = 191.551        # full supply level AHD - matches lake-albert.html
LAKE_FULL_ML    = 4148.3         # full capacity in ML (pre-computed)


def _ahd_to_ml(ahd):
    """Convert AHD to volume in ML. Identical to ahdToML() in lake-albert.html."""
    return max(0.0, LAKE_SURFACE_M2 * (ahd - LAKE_VOL_BOTTOM) / 1000)


_WHITE_LOGO_URL = 'https://bidgee182.github.io/wwcc-weather-page/assets/images/logo-white.png'


def _white_logo_html():
    return (f'<img src="{_WHITE_LOGO_URL}" width="194" height="44" alt="Wagga Wagga Country Club"'
            f' style="display:block;border:0;">')


_LAKE_LEVELS = [
    # (min_ahd, level_num, name, rate_str, bg_hex, fg_hex, row_bg, accent)
    (190.250, 1, 'Normal Operations',           '1.50 ML/day', '#00762A', 'white',  '#f0fdf4', '#00762A'),
    (190.050, 2, 'Increased Monitoring',        '1.00 ML/day', '#8AC63F', '#111111','#f7fee7', '#8AC63F'),
    (189.850, 3, 'Water Conservation Planning', '0.75 ML/day', '#FFDD00', '#111111','#fefce8', '#FFDD00'),
    (189.650, 4, 'Critical Warning',            '0.50 ML/day', '#F58E1E', '#111111','#fff7ed', '#F58E1E'),
    (0,       5, 'Cease to Pump',               '0 ML/day',    '#EB1E23', 'white',  '#fef2f2', '#EB1E23'),
]


def _lake_level_info(ahd):
    """Return (num, name, rate_str, bg, fg, row_bg, accent) for given AHD."""
    for min_ahd, num, name, rate, bg, fg, row_bg, accent in _LAKE_LEVELS:
        if ahd >= min_ahd:
            return num, name, rate, bg, fg, row_bg, accent
    return _LAKE_LEVELS[-1][1:]


def _load_lake_data():
    """Load all lake-related JSON files. Returns dict or None on error."""
    base = Path(__file__).parent.parent / 'data'
    try:
        with open(base / 'farmbot_lake_latest.json') as f:
            latest = json.load(f)
        with open(base / 'farmbot_lake_readings.json') as f:
            readings = json.load(f)
        with open(base / 'pumping_usage.json') as f:
            pumping = json.load(f)
        return {'latest': latest, 'readings': readings, 'pumping': pumping}
    except Exception as e:
        logging.warning(f'Lake data load failed: {e}')
        return None


def _readings_in_range(readings, start_date, end_date):
    """Filter readings to [start_date, end_date] inclusive. Attaches _date attr."""
    result = []
    for r in readings:
        try:
            dt = datetime.fromisoformat(r['date'].replace('Z', '+00:00'))
            d = dt.astimezone(ZoneInfo('Australia/Sydney')).date()
            if start_date <= d <= end_date:
                result.append({**r, '_date': d, '_dt': dt})
        except Exception:
            pass
    return sorted(result, key=lambda x: x['_dt'])


def _pumping_in_range(pumping, start_date, end_date):
    """Filter pumping records to [start_date, end_date] inclusive."""
    result = []
    for p in pumping:
        try:
            d = date.fromisoformat(p['date'])
            if start_date <= d <= end_date:
                result.append({**p, '_date': d})
        except Exception:
            pass
    return sorted(result, key=lambda x: x['_date'])


def _lake_chart_html(readings, start_date, end_date):
    """Return an <img> pointing to a QuickChart.io PNG - renders on iOS Mail, Outlook, Gmail."""
    import json as _json
    import urllib.parse as _up
    from collections import defaultdict as _dd

    day_ahd = {}
    for r in readings:
        d = r["_date"]
        v = float(r.get("ahd", 0) or 0)
        if v > 0:
            day_ahd[d] = v

    total_days = max((end_date - start_date).days + 1, 1)
    n_days     = (end_date - start_date).days

    if n_days > 60:
        # Annual: downsample to monthly averages
        month_vals = _dd(list)
        for i in range(total_days):
            d = start_date + timedelta(days=i)
            if d in day_ahd:
                month_vals[(d.year, d.month)].append(day_ahd[d])
        dates, vals = [], []
        cur = start_date.replace(day=1)
        while cur <= end_date:
            k = (cur.year, cur.month)
            if k in month_vals:
                dates.append(cur)
                vals.append(round(sum(month_vals[k]) / len(month_vals[k]), 3))
            cur = cur.replace(year=cur.year + 1, month=1) if cur.month == 12 else cur.replace(month=cur.month + 1)
        date_fmt = "%b"
    elif n_days > 10:
        # Monthly: ~12 evenly-spaced samples
        step = max(1, total_days // 12)
        dates, vals = [], []
        for i in range(0, total_days, step):
            d = start_date + timedelta(days=i)
            if d in day_ahd:
                dates.append(d)
                vals.append(day_ahd[d])
        if end_date in day_ahd and (not dates or dates[-1] != end_date):
            dates.append(end_date)
            vals.append(day_ahd[end_date])
        date_fmt = "%-d %b"
    else:
        # Weekly: every data point
        dates, vals = [], []
        for i in range(total_days):
            d = start_date + timedelta(days=i)
            if d in day_ahd:
                dates.append(d)
                vals.append(day_ahd[d])
        date_fmt = "%-d %b"

    if not vals:
        return ""

    labels = [_fmt_d(d, date_fmt) for d in dates]
    ymin   = round(min(vals) - 0.05, 3)
    ymax   = round(max(vals) + 0.05, 3)

    THRESH = [
        (190.250, "#00762A"),
        (190.050, "#8AC63F"),
        (189.850, "#FFDD00"),
        (189.650, "#EB1E23"),
    ]
    datasets = [{
        "label":           "Lake Level",
        "data":            vals,
        "borderColor":     "#1abc9c",
        "backgroundColor": "rgba(26,188,156,0.12)",
        "fill":            True,
        "lineTension":     0.3,
        "pointRadius":     0,
        "borderWidth":     2,
    }]
    for thr, col in THRESH:
        if ymin - 0.1 <= thr <= ymax + 0.1:
            datasets.append({
                "data":        [thr] * len(dates),
                "borderColor": col,
                "borderWidth": 1,
                "borderDash":  [5, 4],
                "fill":        False,
                "pointRadius": 0,
                "lineTension": 0,
            })

    config = {
        "type": "line",
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "legend": {"display": False},
            "scales": {
                "xAxes": [{"gridLines": {"color": "#1e3040"},
                           "ticks": {"fontColor": "#4a6070", "maxRotation": 0}}],
                "yAxes": [{"gridLines": {"color": "#1e3040"},
                           "ticks": {"fontColor": "#4a6070", "min": ymin, "max": ymax}}],
            },
        },
    }

    cfg_json = _json.dumps(config, separators=(",", ":"))
    url = f"https://quickchart.io/chart?bkg=%230d1b2a&w=560&h=190&c={_up.quote(cfg_json)}"
    return (f'<img src="{url}" width="100%" alt="Lake level chart"'
            f' style="display:block;border-radius:6px;max-width:100%;">')



def _lake_threshold_table(current_ahd):
    """Build the water licence threshold reference table HTML."""
    level_num, _, _, _, _, _, _ = _lake_level_info(current_ahd)

    level_data = [
        (1, '&ge; 190.250 m AHD', 'Normal Operations',           '1.50 ML/day', '#00762A', 'white',  '#f0fdf4', '#374151', '#065f46', '#065f46'),
        (2, '&ge; 190.050 m AHD', 'Increased Monitoring',        '1.00 ML/day', '#8AC63F', '#111',   '#f7fee7', '#374151', '#3a6b10', '#3a6b10'),
        (3, '&ge; 189.850 m AHD', 'Water Conservation Planning', '0.75 ML/day', '#FFDD00', '#111',   '#fefce8', '#374151', '#854d0e', '#854d0e'),
        (4, '&ge; 189.650 m AHD', 'Critical Warning',            '0.50 ML/day', '#F58E1E', '#111',   '#fff7ed', '#374151', '#9a3412', '#9a3412'),
        (5, '&lt; 189.650 m AHD', 'Cease to Pump',               '0 ML/day',    '#EB1E23', 'white',  '#fef2f2', '#374151', '#991b1b', '#991b1b'),
    ]

    rows = ''
    for lnum, thresh, status, rate, badge_bg, badge_fg, row_bg, thr_col, stat_col, rate_col in level_data:
        is_current = lnum == level_num
        bl = f'border-left:3px solid {badge_bg};' if is_current else ''
        now_tag = f'<span style="font-size:10px;color:{stat_col};font-weight:700;margin-left:4px;">&#9654; NOW</span>' if is_current else ''
        rows += f"""
        <tr>
          <td bgcolor="{row_bg}" style="background:{row_bg};padding:8px 12px;{bl}">
            <span style="display:inline-block;background:{badge_bg};color:{badge_fg};padding:2px 8px;
                border-radius:4px;font-size:11px;font-weight:700;">L{lnum}</span>{now_tag}</td>
          <td bgcolor="{row_bg}" style="background:{row_bg};padding:8px 12px;color:{thr_col};{bl}">{thresh}</td>
          <td bgcolor="{row_bg}" style="background:{row_bg};padding:8px 12px;color:{stat_col};font-weight:600;{bl}">{status}</td>
          <td bgcolor="{row_bg}" style="background:{row_bg};padding:8px 12px;text-align:right;font-weight:700;color:{rate_col};{bl}">{rate}</td>
        </tr>"""

    return f"""<div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
        color:#94a3b8;margin-bottom:8px;">WATER LICENCE - OPERATING LEVELS</div>
      <table width="100%" cellpadding="0" cellspacing="0"
          style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;font-size:12px;">
        <tr bgcolor="#1a4a2e" style="background-color:#1a4a2e;">
          <th style="padding:9px 12px;text-align:left;color:white;font-size:11px;font-weight:700;">Level</th>
          <th style="padding:9px 12px;text-align:left;color:white;font-size:11px;font-weight:700;">Threshold</th>
          <th style="padding:9px 12px;text-align:left;color:white;font-size:11px;font-weight:700;">Status</th>
          <th style="padding:9px 12px;text-align:right;color:white;font-size:11px;font-weight:700;">Max Rate</th>
        </tr>{rows}
      </table>"""


def _build_lake_section_daily(lake_data, target_date, section_num=7):
    """Build the lake section HTML for the daily GK email."""
    if lake_data is None:
        return ''

    latest   = lake_data['latest']
    readings = lake_data['readings']
    pumping  = lake_data['pumping']

    ahd = float(latest.get('lake_ahd', 0) or 0)
    if ahd <= 0:
        return ''

    level_num, level_name, rate, bg, fg, row_bg, accent = _lake_level_info(ahd)

    # Drop / rise distances
    l3_thresh  = 189.850
    l1_thresh  = 190.250
    drop_to_l3 = ahd - l3_thresh    # positive = above L3, negative = below L3
    rise_to_l1 = l1_thresh - ahd    # positive = below L1, negative = at/above L1

    # 7-day window
    chart_end      = target_date
    chart_start    = target_date - timedelta(days=6)
    chart_readings = _readings_in_range(readings, chart_start, chart_end)

    # Daily level change (target_date vs target_date-1)
    prev_day      = target_date - timedelta(days=1)
    today_rdgs    = [r for r in chart_readings if r['_date'] == target_date]
    prev_rdgs     = [r for r in chart_readings if r['_date'] == prev_day]
    today_ahd_r   = float(today_rdgs[-1].get('ahd', 0)) if today_rdgs else ahd
    prev_ahd_r    = float(prev_rdgs[-1].get('ahd', 0))  if prev_rdgs  else None
    daily_chg_ahd = (today_ahd_r - prev_ahd_r) if (prev_ahd_r and prev_ahd_r > 0 and today_ahd_r > 0) else None
    daily_chg_ml  = (daily_chg_ahd * LAKE_SURFACE_M2 / 1000) if daily_chg_ahd is not None else None

    # Volume
    vol_ml  = _ahd_to_ml(ahd)
    vol_pct = min(100.0, vol_ml / LAKE_FULL_ML * 100)

    # Yesterday's pumping
    yest_pump = next((p for p in pumping if p['date'] == target_date.strftime('%Y-%m-%d')), None)
    pump_ml   = float(yest_pump['ml']) if yest_pump else 0.0
    pump_note = yest_pump.get('note', '') if yest_pump else ''

    # Evaporation = volume lost - extraction (positive when lake lost water to evaporation)
    if daily_chg_ml is not None:
        level_drop_ml = -daily_chg_ml        # positive when lake dropped
        evap_ml = level_drop_ml - pump_ml    # may be negative if rainfall caused net inflow
    else:
        evap_ml = None

    # Chart
    chart_html = _lake_chart_html(chart_readings, chart_start, chart_end)

    banner_text = '#111' if fg == '#111111' else 'white'
    drop_text = f'{drop_to_l3:+.3f} m from Level 3 threshold' if drop_to_l3 >= 0 else f'{abs(drop_to_l3):.3f} m BELOW Level 3'
    rise_text = f'{rise_to_l1:.3f} m needed for Level 1' if rise_to_l1 > 0 else 'At or above Level 1'

    # Daily change display
    if daily_chg_ahd is not None:
        chg_sign   = '+' if daily_chg_ahd >= 0 else ''
        chg_str    = f'{chg_sign}{daily_chg_ahd:.3f} m'
        chg_ml_str = f'{chg_sign}{daily_chg_ml:.1f} ML'
        chg_sub    = f'{"rise" if daily_chg_ahd >= 0 else "fall"} &bull; {chg_ml_str}'
    else:
        chg_str = '--'
        chg_sub = 'no prior reading'

    # Evaporation display
    if evap_ml is not None:
        if evap_ml >= 0:
            evap_str = f'{evap_ml:.1f} ML'
            evap_sub = 'lost to evaporation'
        else:
            evap_str = '0.0 ML'
            evap_sub = f'net inflow ({abs(evap_ml):.1f} ML gain)'
    else:
        evap_str = '--'
        evap_sub = 'insufficient data'

    section_header = sec_header(section_num, 'Lake Albert - Current Level &amp; Licence Status',
                                 'Live FarmBot sensor &bull; Water licence level &bull; Current max pump rate')

    threshold_tbl = _lake_threshold_table(ahd)

    lake_kv = _gk_kv_table([
        ('Lake Level (AHD)',        f'{ahd:.3f} m AHD'),
        ('Drop to Level 3',         drop_text),
        ('Rise to Level 1',         rise_text),
        ('Volume',                  f'{vol_ml:.0f} ML ({vol_pct:.1f}% of capacity)'),
        ('Full Capacity',           f'{LAKE_FULL_ML:.0f} ML at {LAKE_FULL_AHD_V} m AHD'),
        ('Daily Level Change',      f'{chg_str} &nbsp;&bull;&nbsp; {chg_sub}'),
        ("Yesterday's Extraction",  f'{pump_ml:.1f} ML{(" - " + pump_note) if pump_note else ""}'),
        ('Evaporation (est.)',       f'{evap_str} &nbsp;&bull;&nbsp; {evap_sub}'),
    ])

    body = f"""
      <!-- Licence level banner -->
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
        <tr>
          <td bgcolor="{bg}" style="background-color:{bg};padding:12px 18px;
              border-radius:8px;border-left:5px solid {accent};">
            <table cellpadding="0" cellspacing="0" width="100%"><tr>
              <td valign="middle">
                <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;color:{banner_text};
                    text-transform:uppercase;opacity:0.7;margin-bottom:3px;">CURRENT LICENCE LEVEL</div>
                <div style="font-size:17px;font-weight:700;color:{banner_text};line-height:1.2;">
                  Level {level_num} - {level_name}</div>
                <div style="font-size:12px;color:{banner_text};opacity:0.75;margin-top:3px;">
                  {drop_text} &bull; {rise_text}
                </div>
              </td>
              <td valign="middle" align="right" style="padding-left:16px;white-space:nowrap;">
                <div style="font-size:26px;font-weight:700;color:{banner_text};line-height:1;">{rate}</div>
                <div style="font-size:11px;color:{banner_text};opacity:0.75;text-align:right;margin-top:2px;">max pump rate</div>
              </td>
            </tr></table>
          </td>
        </tr>
      </table>

      <div style="margin-bottom:16px;">{lake_kv}</div>

      <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
          color:#94a3b8;margin-bottom:8px;">LAKE LEVEL - PAST 7 DAYS</div>
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:18px;">
        <tr><td bgcolor="#0d1b2a" style="background-color:#0d1b2a;padding:16px;border-radius:10px;">
          {chart_html}
        </td></tr>
      </table>

      {threshold_tbl}"""

    return f"""
  {section_header}
  <tr>
    <td style="background:white;padding:20px 24px 28px;border-radius:0 0 10px 10px;">
      {body}
    </td>
  </tr>"""


def _build_lake_section_weekly(lake_data, week_end_date, section_num=4):
    """Build the lake section HTML for the weekly GK email."""
    if lake_data is None:
        return ''

    latest   = lake_data['latest']
    readings = lake_data['readings']
    pumping  = lake_data['pumping']

    ahd = float(latest.get('lake_ahd', 0) or 0)
    if ahd <= 0:
        return ''

    level_num, level_name, rate, bg, fg, row_bg, accent = _lake_level_info(ahd)

    # 7-day window
    chart_end      = week_end_date
    chart_start    = week_end_date - timedelta(days=6)
    chart_readings = _readings_in_range(readings, chart_start, chart_end)
    week_pumping   = _pumping_in_range(pumping, chart_start, chart_end)

    # First and last readings this week for change calc
    week_ahd_vals = [float(r.get('ahd', 0)) for r in chart_readings if float(r.get('ahd', 0)) > 0]
    first_ahd    = week_ahd_vals[0]  if week_ahd_vals else ahd
    last_ahd     = week_ahd_vals[-1] if week_ahd_vals else ahd
    week_change  = last_ahd - first_ahd
    week_chg_ml  = week_change * LAKE_SURFACE_M2 / 1000

    total_pump_ml = sum(float(p.get('ml', 0)) for p in week_pumping)

    # Volume
    vol_ml  = _ahd_to_ml(ahd)
    vol_pct = min(100.0, vol_ml / LAKE_FULL_ML * 100)

    # Evaporation for the week
    level_drop_ml  = -week_chg_ml    # positive when lake dropped overall
    week_evap_ml   = level_drop_ml - total_pump_ml

    chart_html = _lake_chart_html(chart_readings, chart_start, chart_end)

    banner_text = '#111' if fg == '#111111' else 'white'
    change_sign = '+' if week_change >= 0 else ''
    chg_ml_sign = '+' if week_chg_ml >= 0 else ''

    evap_str = f'{week_evap_ml:.1f} ML' if week_evap_ml >= 0 else '0.0 ML'
    evap_sub = 'lost to evaporation' if week_evap_ml >= 0 else f'net inflow ({abs(week_evap_ml):.1f} ML gain)'

    # Daily breakdown table
    day_rows = ''
    for i in range(7):
        d = chart_start + timedelta(days=i)
        day_readings = [r for r in chart_readings if r['_date'] == d]
        day_ahd = float(day_readings[-1].get('ahd', 0)) if day_readings else None
        day_pump = next((p for p in week_pumping if p['_date'] == d), None)
        pump_ml_d  = float(day_pump['ml']) if day_pump else 0.0
        pump_note  = day_pump.get('note', '-') if day_pump else '-'
        bg_row     = '#f8fafc' if i % 2 == 0 else 'white'
        ahd_str    = f'{day_ahd:.3f} m' if day_ahd else '-'
        day_rows += f"""<tr>
          <td bgcolor="{bg_row}" style="background:{bg_row};padding:7px 10px;font-size:12px;color:#374151;">{_fmt_d(d, '%a %-d %b')}</td>
          <td bgcolor="{bg_row}" style="background:{bg_row};padding:7px 10px;font-size:12px;color:#111827;font-weight:600;">{ahd_str}</td>
          <td bgcolor="{bg_row}" style="background:{bg_row};padding:7px 10px;font-size:12px;color:#374151;">{pump_ml_d:.1f} ML</td>
          <td bgcolor="{bg_row}" style="background:{bg_row};padding:7px 10px;font-size:12px;color:#64748b;font-style:italic;">{pump_note}</td>
        </tr>"""

    section_header = sec_header(section_num, 'Lake Albert - Weekly Summary',
                                 'Lake level trend &bull; Weekly extraction &bull; Licence status')

    threshold_tbl = _lake_threshold_table(ahd)

    lake_kv = _gk_kv_table([
        ('End Level (AHD)',     f'{ahd:.3f} m AHD'),
        ('Week Change',         f'{change_sign}{week_change:.3f} m &nbsp;&bull;&nbsp; {"rise" if week_change >= 0 else "fall"} &nbsp;&bull;&nbsp; {chg_ml_sign}{week_chg_ml:.1f} ML'),
        ('Current Volume',      f'{vol_ml:.0f} ML ({vol_pct:.1f}% of {LAKE_FULL_ML:.0f} ML)'),
        ("Week's Extraction",   f'{total_pump_ml:.1f} ML'),
        ('Evaporation (est.)',  f'{evap_str} &nbsp;&bull;&nbsp; {evap_sub}'),
    ])

    body = f"""
      <!-- Licence banner -->
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
        <tr>
          <td bgcolor="{bg}" style="background-color:{bg};padding:12px 18px;
              border-radius:8px;border-left:5px solid {accent};">
            <table cellpadding="0" cellspacing="0" width="100%"><tr>
              <td valign="middle">
                <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;
                    color:{banner_text};text-transform:uppercase;opacity:0.7;margin-bottom:3px;">CURRENT LICENCE LEVEL</div>
                <div style="font-size:17px;font-weight:700;color:{banner_text};line-height:1.2;">
                  Level {level_num} - {level_name}</div>
              </td>
              <td valign="middle" align="right" style="padding-left:16px;white-space:nowrap;">
                <div style="font-size:26px;font-weight:700;color:{banner_text};line-height:1;">{rate}</div>
                <div style="font-size:11px;color:{banner_text};opacity:0.75;text-align:right;margin-top:2px;">max pump rate</div>
              </td>
            </tr></table>
          </td>
        </tr>
      </table>

      <div style="margin-bottom:16px;">{lake_kv}</div>

      <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
          color:#94a3b8;margin-bottom:8px;">LAKE LEVEL - PAST 7 DAYS</div>
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:18px;">
        <tr><td bgcolor="#0d1b2a" style="background-color:#0d1b2a;padding:16px;border-radius:10px;">
          {chart_html}
        </td></tr>
      </table>

      <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
          color:#94a3b8;margin-bottom:8px;">DAILY BREAKDOWN</div>
      <table width="100%" cellpadding="0" cellspacing="0"
          style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;font-size:12px;margin-bottom:16px;">
        <tr bgcolor="#1a4a2e" style="background-color:#1a4a2e;">
          <th style="padding:8px 10px;text-align:left;color:white;font-size:11px;">Day</th>
          <th style="padding:8px 10px;text-align:left;color:white;font-size:11px;">Level (AHD)</th>
          <th style="padding:8px 10px;text-align:left;color:white;font-size:11px;">Extraction</th>
          <th style="padding:8px 10px;text-align:left;color:white;font-size:11px;">Note</th>
        </tr>
        {day_rows}
      </table>

      {threshold_tbl}"""

    return f"""
  {section_header}
  <tr>
    <td style="background:white;padding:20px 24px 28px;border-radius:0 0 10px 10px;">
      {body}
    </td>
  </tr>"""


def _zone_history_section_gk(month_start, month_end):
    """Read lake_zone_history.csv and build a zone change log for the monthly GK email."""
    _ZONE_COLOURS = {
        1: ('#00762A', 'white'),
        2: ('#8AC63F', '#111111'),
        3: ('#FFDD00', '#111111'),
        4: ('#F58E1E', '#111111'),
        5: ('#EB1E23', 'white'),
    }
    _ZONE_NAMES = {
        1: 'Level 1 - Normal',
        2: 'Level 2 - Monitoring',
        3: 'Level 3 - Conservation',
        4: 'Level 4 - Critical',
        5: 'Level 5 - Cease to Pump',
    }
    _ZONE_RATES = {
        1: '1.50 ML/day',
        2: '1.00 ML/day',
        3: '0.75 ML/day',
        4: '0.50 ML/day',
        5: '0 ML/day',
    }
    try:
        if not ZONE_HISTORY_CSV.exists():
            return ''
        rows = []
        with ZONE_HISTORY_CSV.open(newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                event = row.get('event', '')
                if event in ('initialised', 'suppressed'):
                    continue
                try:
                    ts = datetime.strptime(row['timestamp_aest'], '%Y-%m-%d %H:%M:%S').date()
                except Exception:
                    continue
                if month_start <= ts <= month_end:
                    rows.append(row)
        if not rows:
            return ''

        # Build summary: time spent in each zone
        zone_durations = {}
        all_rows_sorted = []
        with ZONE_HISTORY_CSV.open(newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get('event') in ('initialised', 'suppressed'):
                    continue
                try:
                    ts = datetime.strptime(row['timestamp_aest'], '%Y-%m-%d %H:%M:%S')
                    all_rows_sorted.append((ts, int(row['new_zone']) if row.get('new_zone') else None))
                except Exception:
                    continue
        all_rows_sorted.sort(key=lambda t: t[0])

        # Find zone at month start
        zone_at_month_start = None
        for ts, z in reversed(all_rows_sorted):
            if ts.date() < month_start and z is not None:
                zone_at_month_start = z
                break
        if zone_at_month_start is None and all_rows_sorted:
            zone_at_month_start = all_rows_sorted[0][1]

        # Build time-in-zone for the month
        month_start_dt = datetime(month_start.year, month_start.month, month_start.day)
        month_end_dt   = datetime(month_end.year, month_end.month, month_end.day, 23, 59, 59)
        periods = [(month_start_dt, zone_at_month_start)]
        for ts, z in all_rows_sorted:
            if month_start_dt <= ts <= month_end_dt and z is not None:
                periods.append((ts, z))
        periods.append((month_end_dt, None))

        zone_days = {}
        for i in range(len(periods) - 1):
            ts_from, zone = periods[i]
            ts_to, _     = periods[i + 1]
            if zone is None:
                continue
            days = (ts_to - ts_from).total_seconds() / 86400
            zone_days[zone] = zone_days.get(zone, 0) + days

        # Duration summary pills
        total_days = (month_end - month_start).days + 1
        summary_cells = ''
        for z in sorted(zone_days):
            bg, fg = _ZONE_COLOURS.get(z, ('#888', 'white'))
            d = zone_days[z]
            pct = d / total_days * 100
            summary_cells += f'''
              <td style="padding:4px 8px;text-align:center;vertical-align:middle;">
                <span style="display:inline-block;background:{bg};color:{fg};
                    font-size:11px;font-weight:700;padding:3px 10px;border-radius:12px;
                    white-space:nowrap;">Level {z}</span><br>
                <span style="font-size:11px;color:#555;font-family:Arial,sans-serif;">
                  {d:.1f}d ({pct:.0f}%)</span>
              </td>'''

        summary_row = f'<table cellpadding="0" cellspacing="0"><tr>{summary_cells}</tr></table>'

        # Change log table
        header_style = (
            'background:#1a4a2e;color:white;font-size:11px;font-weight:700;'
            'font-family:Arial,sans-serif;padding:6px 10px;text-align:left;letter-spacing:0.5px;'
        )
        def _zone_pill(z):
            if not z:
                return '-'
            try:
                zi = int(z)
            except Exception:
                return z
            bg, fg = _ZONE_COLOURS.get(zi, ('#888', 'white'))
            name = _ZONE_NAMES.get(zi, f'Level {zi}')
            return (f'<span style="display:inline-block;background:{bg};color:{fg};'
                    f'font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px;">'
                    f'{name}</span>')

        change_rows_html = ''
        for i, row in enumerate(rows):
            bg = _GK_ROW_A if i % 2 == 0 else _GK_ROW_B
            cell = (f'font-family:Arial,sans-serif;font-size:12px;padding:6px 10px;'
                    f'border-bottom:1px solid {_GK_BORDER};background:{bg};')
            try:
                ts_str = row.get('timestamp_aest', '')
                ts_fmt = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S').strftime('%d %b %H:%M')
            except Exception:
                ts_fmt = row.get('timestamp_aest', '')
            event_label = {
                'zone_change':    'Zone change',
                'cease_pumping':  'CEASE pumping',
                'resume_pumping': 'Resume pumping',
                'email_failed':   'Alert failed',
            }.get(row.get('event', ''), row.get('event', ''))
            old_zone_pill = _zone_pill(row.get('old_zone'))
            new_zone_pill = _zone_pill(row.get('new_zone'))
            new_rate_str  = row.get('new_rate') or _ZONE_RATES.get(
                int(row['new_zone']) if row.get('new_zone') else 0, '-')
            ahd_str = f"{float(row['ahd']):.3f} m" if row.get('ahd') else '-'
            sent_str = 'Yes' if row.get('email_sent') == 'true' else 'No'
            change_rows_html += f'''<tr>
              <td style="{cell}">{ts_fmt}</td>
              <td style="{cell}">{event_label}</td>
              <td style="{cell}">{old_zone_pill}</td>
              <td style="{cell}">{new_zone_pill}</td>
              <td style="{cell}">{new_rate_str}</td>
              <td style="{cell}">{ahd_str}</td>
              <td style="{cell}">{sent_str}</td>
            </tr>'''

        change_table = f'''
          <div style="overflow-x:auto;">
          <table width="100%" cellpadding="0" cellspacing="0"
              style="border-collapse:collapse;border:1px solid {_GK_BORDER};border-radius:8px;overflow:hidden;min-width:500px;">
            <thead><tr>
              <th style="{header_style}">Date/Time</th>
              <th style="{header_style}">Event</th>
              <th style="{header_style}">From</th>
              <th style="{header_style}">To</th>
              <th style="{header_style}">New Rate</th>
              <th style="{header_style}">AHD</th>
              <th style="{header_style}">Alert sent</th>
            </tr></thead>
            <tbody>{change_rows_html}</tbody>
          </table></div>'''

        return f'''
  <tr>
    <td style="background:white;padding:16px 24px 24px;border-top:1px solid {_GK_BORDER};">
      <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
          color:#1a4a2e;margin-bottom:10px;">PUMPING LICENCE - ZONE HISTORY</div>
      <div style="margin-bottom:12px;">{summary_row}</div>
      <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
          color:#94a3b8;margin-bottom:6px;">ZONE CHANGES THIS MONTH</div>
      {change_table}
    </td>
  </tr>'''

    except Exception as e:
        log.warning(f'Could not build zone history section: {e}')
        return ''


def _build_lake_section_monthly(lake_data, month_label, section_num=5):
    """Build the lake section HTML for the monthly GK email."""
    if lake_data is None:
        return ''

    latest   = lake_data['latest']
    readings = lake_data['readings']
    pumping  = lake_data['pumping']

    ahd = float(latest.get('lake_ahd', 0) or 0)
    if ahd <= 0:
        return ''

    level_num, level_name, rate, bg, fg, row_bg, accent = _lake_level_info(ahd)

    # Parse month_label e.g. "June 2026"
    try:
        month_start = datetime.strptime(month_label, '%B %Y').replace(tzinfo=ZoneInfo('Australia/Sydney')).date()
        if month_start.month == 12:
            month_end = month_start.replace(year=month_start.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            month_end = month_start.replace(month=month_start.month + 1, day=1) - timedelta(days=1)
    except Exception:
        return ''

    chart_readings = _readings_in_range(readings, month_start, month_end)
    month_pumping  = _pumping_in_range(pumping, month_start, month_end)

    ahd_vals = [float(r.get('ahd', 0)) for r in chart_readings if float(r.get('ahd', 0)) > 0]
    high_ahd      = max(ahd_vals) if ahd_vals else ahd
    low_ahd       = min(ahd_vals) if ahd_vals else ahd
    avg_ahd       = sum(ahd_vals) / len(ahd_vals) if ahd_vals else ahd
    first_ahd_m   = ahd_vals[0]  if ahd_vals else ahd
    last_ahd_m    = ahd_vals[-1] if ahd_vals else ahd
    month_change  = last_ahd_m - first_ahd_m
    month_chg_ml  = month_change * LAKE_SURFACE_M2 / 1000

    total_pump_ml = sum(float(p.get('ml', 0)) for p in month_pumping)

    # Volume
    vol_ml  = _ahd_to_ml(ahd)
    vol_pct = min(100.0, vol_ml / LAKE_FULL_ML * 100)

    # Evaporation for the month
    level_drop_ml  = -month_chg_ml
    month_evap_ml  = level_drop_ml - total_pump_ml

    chart_html = _lake_chart_html(chart_readings, month_start, month_end)

    banner_text  = '#111' if fg == '#111111' else 'white'
    chg_sign     = '+' if month_change >= 0 else ''
    chg_ml_sign  = '+' if month_chg_ml >= 0 else ''

    evap_str = f'{month_evap_ml:.1f} ML' if month_evap_ml >= 0 else '0.0 ML'
    evap_sub = 'lost to evaporation' if month_evap_ml >= 0 else f'net inflow ({abs(month_evap_ml):.1f} ML gain)'

    section_header = sec_header(section_num, 'Lake Albert - Monthly Summary',
                                 f'{month_label} &bull; Lake level range &bull; Extraction total')

    threshold_tbl = _lake_threshold_table(ahd)

    lake_kv = _gk_kv_table([
        ('Month High (AHD)',    f'{high_ahd:.3f} m AHD'),
        ('Month Low (AHD)',     f'{low_ahd:.3f} m AHD'),
        ('Month Average (AHD)', f'{avg_ahd:.3f} m AHD'),
        ('Current Volume',      f'{vol_ml:.0f} ML ({vol_pct:.1f}% of capacity)'),
        ('Month Change',        f'{chg_sign}{month_change:.3f} m &nbsp;&bull;&nbsp; {"rise" if month_change >= 0 else "fall"} &nbsp;&bull;&nbsp; {chg_ml_sign}{month_chg_ml:.1f} ML'),
        ('Total Extraction',    f'{total_pump_ml:.1f} ML &nbsp;&bull;&nbsp; Licence: {rate}'),
        ('Evaporation (est.)',  f'{evap_str} &nbsp;&bull;&nbsp; {evap_sub}'),
    ])

    body = f"""
      <!-- Licence banner -->
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
        <tr>
          <td bgcolor="{bg}" style="background-color:{bg};padding:12px 18px;
              border-radius:8px;border-left:5px solid {accent};">
            <table cellpadding="0" cellspacing="0" width="100%"><tr>
              <td valign="middle">
                <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;
                    color:{banner_text};text-transform:uppercase;opacity:0.7;margin-bottom:3px;">CURRENT LICENCE LEVEL</div>
                <div style="font-size:17px;font-weight:700;color:{banner_text};line-height:1.2;">
                  Level {level_num} - {level_name}</div>
              </td>
              <td valign="middle" align="right" style="padding-left:16px;white-space:nowrap;">
                <div style="font-size:26px;font-weight:700;color:{banner_text};line-height:1;">{rate}</div>
                <div style="font-size:11px;color:{banner_text};opacity:0.75;text-align:right;margin-top:2px;">max pump rate</div>
              </td>
            </tr></table>
          </td>
        </tr>
      </table>

      <div style="margin-bottom:16px;">{lake_kv}</div>

      <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
          color:#94a3b8;margin-bottom:8px;">LAKE LEVEL - {month_label.upper()}</div>
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:18px;">
        <tr><td bgcolor="#0d1b2a" style="background-color:#0d1b2a;padding:16px;border-radius:10px;">
          {chart_html}
        </td></tr>
      </table>

      {threshold_tbl}"""

    zone_history_html = _zone_history_section_gk(month_start, month_end)

    return f"""
  {section_header}
  <tr>
    <td style="background:white;padding:20px 24px 28px;border-radius:0 0 10px 10px;">
      {body}
    </td>
  </tr>{zone_history_html}"""


def _build_lake_section_yearly(lake_data, year_label, section_num=3):
    """Build the lake section HTML for the yearly GK email."""
    if lake_data is None:
        return ''

    latest   = lake_data['latest']
    readings = lake_data['readings']
    pumping  = lake_data['pumping']

    ahd = float(latest.get('lake_ahd', 0) or 0)
    if ahd <= 0:
        return ''

    level_num, level_name, rate, bg, fg, row_bg, accent = _lake_level_info(ahd)

    # Parse year_label e.g. "Full Year 2025"
    try:
        year = int(year_label.split()[-1])
    except Exception:
        return ''

    year_start = date(year, 1, 1)
    year_end   = date(year, 12, 31)

    chart_readings = _readings_in_range(readings, year_start, year_end)
    year_pumping   = _pumping_in_range(pumping, year_start, year_end)

    ahd_vals = [float(r.get('ahd', 0)) for r in chart_readings if float(r.get('ahd', 0)) > 0]
    high_ahd      = max(ahd_vals) if ahd_vals else ahd
    low_ahd       = min(ahd_vals) if ahd_vals else ahd
    avg_ahd       = sum(ahd_vals) / len(ahd_vals) if ahd_vals else ahd
    first_ahd_y   = ahd_vals[0]  if ahd_vals else ahd
    last_ahd_y    = ahd_vals[-1] if ahd_vals else ahd
    year_change   = last_ahd_y - first_ahd_y
    year_chg_ml   = year_change * LAKE_SURFACE_M2 / 1000

    total_pump_ml = sum(float(p.get('ml', 0)) for p in year_pumping)

    # Volume
    vol_ml  = _ahd_to_ml(ahd)
    vol_pct = min(100.0, vol_ml / LAKE_FULL_ML * 100)

    # Annual evaporation
    level_drop_ml = -year_chg_ml
    year_evap_ml  = level_drop_ml - total_pump_ml

    chart_html = _lake_chart_html(chart_readings, year_start, year_end)

    banner_text = '#111' if fg == '#111111' else 'white'
    chg_sign    = '+' if year_change >= 0 else ''
    chg_ml_sign = '+' if year_chg_ml >= 0 else ''

    evap_str = f'{year_evap_ml:.1f} ML' if year_evap_ml >= 0 else '0.0 ML'
    evap_sub = 'lost to evaporation' if year_evap_ml >= 0 else f'net inflow ({abs(year_evap_ml):.1f} ML gain)'

    # Monthly summary table
    from collections import defaultdict
    monthly = defaultdict(lambda: {'ahd_vals': [], 'pump_ml': 0.0})
    for r in chart_readings:
        key = r['_date'].strftime('%Y-%m')
        v = float(r.get('ahd', 0))
        if v > 0:
            monthly[key]['ahd_vals'].append(v)
    for p in year_pumping:
        key = p['_date'].strftime('%Y-%m')
        monthly[key]['pump_ml'] += float(p.get('ml', 0))

    month_rows = ''
    for mi in range(1, 13):
        key    = f'{year}-{mi:02d}'
        m_label = date(year, mi, 1).strftime('%B')
        m_data  = monthly.get(key, {'ahd_vals': [], 'pump_ml': 0.0})
        m_ahd_vals = m_data['ahd_vals']
        m_avg  = sum(m_ahd_vals) / len(m_ahd_vals) if m_ahd_vals else None
        m_high = max(m_ahd_vals) if m_ahd_vals else None
        m_low  = min(m_ahd_vals) if m_ahd_vals else None
        m_pump = m_data['pump_ml']
        bg_row = '#f8fafc' if mi % 2 == 0 else 'white'

        if m_avg:
            lnum2, lname2, lrate2, lbg2, lfg2, _, _ = _lake_level_info(m_avg)
            badge = f'<span style="display:inline-block;background:{lbg2};color:{lfg2};padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;">L{lnum2}</span>'
        else:
            badge = '-'

        avg_str  = f'{m_avg:.3f} m'  if m_avg  else '-'
        high_str = f'{m_high:.3f} m' if m_high else '-'
        low_str  = f'{m_low:.3f} m'  if m_low  else '-'
        pump_str = f'{m_pump:.1f} ML'

        month_rows += f"""<tr>
          <td bgcolor="{bg_row}" style="background:{bg_row};padding:7px 10px;font-size:12px;color:#374151;font-weight:600;">{m_label}</td>
          <td bgcolor="{bg_row}" style="background:{bg_row};padding:7px 10px;font-size:12px;color:#111827;">{avg_str}</td>
          <td bgcolor="{bg_row}" style="background:{bg_row};padding:7px 10px;font-size:12px;color:#065f46;">{high_str}</td>
          <td bgcolor="{bg_row}" style="background:{bg_row};padding:7px 10px;font-size:12px;color:#9a3412;">{low_str}</td>
          <td bgcolor="{bg_row}" style="background:{bg_row};padding:7px 10px;font-size:12px;color:#374151;">{pump_str}</td>
          <td bgcolor="{bg_row}" style="background:{bg_row};padding:7px 10px;">{badge}</td>
        </tr>"""

    section_header = sec_header(section_num, 'Lake Albert - Annual Summary',
                                 f'{year_label} &bull; Lake level range &bull; Annual extraction')

    threshold_tbl = _lake_threshold_table(ahd)

    lake_kv = _gk_kv_table([
        ('Year High (AHD)',     f'{high_ahd:.3f} m AHD'),
        ('Year Low (AHD)',      f'{low_ahd:.3f} m AHD'),
        ('Year Average (AHD)',  f'{avg_ahd:.3f} m AHD'),
        ('Current Volume',      f'{vol_ml:.0f} ML ({vol_pct:.1f}% of capacity)'),
        ('Year Change',         f'{chg_sign}{year_change:.3f} m &nbsp;&bull;&nbsp; {"rise" if year_change >= 0 else "fall"} &nbsp;&bull;&nbsp; {chg_ml_sign}{year_chg_ml:.1f} ML'),
        ('Annual Extraction',   f'{total_pump_ml:.1f} ML'),
        ('Evaporation (est.)',  f'{evap_str} &nbsp;&bull;&nbsp; {evap_sub}'),
    ])

    body = f"""
      <!-- Licence banner -->
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
        <tr>
          <td bgcolor="{bg}" style="background-color:{bg};padding:12px 18px;
              border-radius:8px;border-left:5px solid {accent};">
            <table cellpadding="0" cellspacing="0" width="100%"><tr>
              <td valign="middle">
                <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;
                    color:{banner_text};text-transform:uppercase;opacity:0.7;margin-bottom:3px;">CURRENT LICENCE LEVEL</div>
                <div style="font-size:17px;font-weight:700;color:{banner_text};line-height:1.2;">
                  Level {level_num} - {level_name}</div>
              </td>
              <td valign="middle" align="right" style="padding-left:16px;white-space:nowrap;">
                <div style="font-size:26px;font-weight:700;color:{banner_text};line-height:1;">{rate}</div>
                <div style="font-size:11px;color:{banner_text};opacity:0.75;text-align:right;margin-top:2px;">max pump rate</div>
              </td>
            </tr></table>
          </td>
        </tr>
      </table>

      <div style="margin-bottom:16px;">{lake_kv}</div>

      <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
          color:#94a3b8;margin-bottom:8px;">LAKE LEVEL - {year_label.upper()}</div>
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:18px;">
        <tr><td bgcolor="#0d1b2a" style="background-color:#0d1b2a;padding:16px;border-radius:10px;">
          {chart_html}
        </td></tr>
      </table>

      <div style="font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;
          color:#94a3b8;margin-bottom:8px;">MONTHLY BREAKDOWN</div>
      <table width="100%" cellpadding="0" cellspacing="0"
          style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;font-size:12px;margin-bottom:16px;">
        <tr bgcolor="#1a4a2e" style="background-color:#1a4a2e;">
          <th style="padding:8px 10px;text-align:left;color:white;font-size:11px;">Month</th>
          <th style="padding:8px 10px;text-align:left;color:white;font-size:11px;">Avg AHD</th>
          <th style="padding:8px 10px;text-align:left;color:white;font-size:11px;">High</th>
          <th style="padding:8px 10px;text-align:left;color:white;font-size:11px;">Low</th>
          <th style="padding:8px 10px;text-align:left;color:white;font-size:11px;">Extraction</th>
          <th style="padding:8px 10px;text-align:left;color:white;font-size:11px;">Licence</th>
        </tr>
        {month_rows}
      </table>

      {threshold_tbl}"""

    return f"""
  {section_header}
  <tr>
    <td style="background:white;padding:20px 24px 28px;border-radius:0 0 10px 10px;">
      {body}
    </td>
  </tr>"""

# ─────────────────────────── END LAKE ALBERT HELPERS ────────────────────────


def build_daily_html(row, target_date, history, forecast_days=None, hourly_forecast=None):
    """Generate the HTML email body for the daily morning report (Demo 1 layout)."""
    yesterday_str = _fmt_d(target_date, '%A, %-d %B %Y')
    frost_flag = row['frost_flag'] == 'True' or row['frost_flag'] is True
    da_flag    = row['disease_alert'] == 'True' or row['disease_alert'] is True

    # Pre-compute display values
    et_val        = safe_float(row.get('et_mm'), 0)
    rain_val      = safe_float(row.get('rain_mm'), 0)
    net_water     = round(et_val - rain_val, 1)
    net_str       = f'{net_water:+.1f}'
    soil_bal      = safe_float(row.get('soil_balance_7d'), 0)
    uv_str        = row.get('uv_max') if row.get('uv_max') not in (None, '', 'None') else '--'
    pres_str      = row.get('pressure_mean_hpa') if row.get('pressure_mean_hpa') not in (None, '', 'None') else '--'
    night_min_str = row.get('night_min') if row.get('night_min') not in (None, '', 'None') else '--'

    # Soil card colour scheme
    soil_zone_val = row.get('soil_zone', 'Unknown')
    if soil_zone_val == 'Optimal':
        soil_bg, soil_bdr = '#f0fdf4', '#bbf7d0'
        soil_lbl_c, soil_val_c, soil_sub_c = '#065f46', '#1a4a2e', '#166534'
    else:
        soil_bg, soil_bdr = '#f8fafc', '#e2e8f0'
        soil_lbl_c, soil_val_c, soil_sub_c = '#94a3b8', '#111827', '#64748b'

    # Disease row background colours
    ds_bg = RISK_ROW_BG.get(row.get('dollar_spot_risk', ''), '#f8fafc')
    fs_bg = RISK_ROW_BG.get(row.get('fusarium_risk', ''), '#f8fafc')
    bp_bg = RISK_ROW_BG.get(row.get('brown_patch_risk', ''), '#f8fafc')
    py_bg = RISK_ROW_BG.get(row.get('pythium_risk', ''), '#f8fafc')

    # Disease outlook for today (forecast_days[0]) and tomorrow (forecast_days[1])
    fd0  = forecast_days[0] if forecast_days and len(forecast_days) > 0 else None
    fd1  = forecast_days[1] if forecast_days and len(forecast_days) > 1 else None
    out0 = _disease_outlook(fd0['max_c'], fd0['min_c'], fd0['precip_mm']) if fd0 else None
    out1 = _disease_outlook(fd1['max_c'], fd1['min_c'], fd1['precip_mm']) if fd1 else None

    def _no_badge():
        return ('<span style="background:#f1f5f9;color:#94a3b8;padding:3px 10px;'
                'border-radius:20px;font-size:11px;font-weight:700;display:inline-block;">--</span>')

    def outlook_badge(risk):
        return risk_badge(risk) if risk and risk != '--' else _no_badge()

    # Alert banners
    frost_banner = ''
    if frost_flag:
        _fmin  = safe_float(row.get('temp_min'))
        _ftime = row.get('temp_min_time') or ''
        _fdetail = ''
        if _fmin is not None:
            _fdetail = f' Minimum {_fmin:.1f}&deg;C' + (f' at {_ftime}' if _ftime else '') + '.'
        frost_banner = (
            '<tr><td style="background:#0c1a2e;border-left:4px solid #60a5fa;'
            'padding:12px 24px;color:#93c5fd;font-size:13px;">'
            '&#10052; &nbsp;<strong style="color:#bfdbfe;">Frost recorded overnight</strong>'
            f' - greens should be checked before early morning play.{_fdetail}</td></tr>')

    disease_banner = ''
    if da_flag:
        triggered = []
        if row.get('dollar_spot_risk')  in ('HIGH', 'SEVERE'): triggered.append('Dollar Spot')
        if row.get('fusarium_risk')     in ('HIGH', 'SEVERE'): triggered.append('Fusarium Patch')
        if row.get('brown_patch_risk')  in ('HIGH', 'SEVERE'): triggered.append('Brown Patch')
        if row.get('pythium_risk')      in ('HIGH', 'SEVERE'): triggered.append('Pythium Blight')
        triggered_str = ', '.join(triggered) if triggered else 'one or more diseases'
        disease_banner = (
            '<tr><td style="background:#1c0a0a;border-left:4px solid #ef4444;'
            'padding:12px 24px;color:#fca5a5;font-size:13px;">'
            f'&#129440; &nbsp;<strong style="color:#fecaca;">Disease alert</strong>'
            f' - {triggered_str} reached HIGH or SEVERE risk yesterday.</td></tr>')

    fog_banner = ''
    if row.get('fog_forecast') in (True, 'True'):
        fog_banner = (
            '<tr><td style="background:#1c1c2e;border-left:4px solid #94a3b8;'
            'padding:12px 24px;color:#cbd5e1;font-size:13px;">'
            '&#127787; &nbsp;<strong style="color:#e2e8f0;">Fog forecast this morning</strong>'
            ' - check visibility before early tee times.</td></tr>')

    lightning_banner = ''
    if row.get('lightning_forecast') in (True, 'True'):
        lightning_banner = (
            '<tr><td style="background:#1a0a00;border-left:4px solid #f97316;'
            'padding:12px 24px;color:#fdba74;font-size:13px;">'
            '&#9889; &nbsp;<strong style="color:#fed7aa;">Thunderstorm forecast today</strong>'
            ' - monitor conditions and suspend play if lightning is detected.</td></tr>')

    # 5-day forecast strip (Section 5)
    forecast_html = ''
    if forecast_days:
        cols = []
        for i, fd in enumerate(forecast_days[:5]):
            is_today  = (i == 0)
            bg_col    = '#eff6ff' if is_today else '#f8fafc'
            bdr_style = '2px solid #93c5fd' if is_today else '1px solid #e2e8f0'
            lbl_color = '#1d4ed8' if is_today else '#64748b'
            heading   = 'Today' if is_today else fd['label'].split(' ')[0]
            subdate   = fd['label']
            max_c     = f"{fd['max_c']:.0f}" if fd['max_c'] is not None else '--'
            min_c     = f"{fd['min_c']:.0f}" if fd['min_c'] is not None else '--'
            precip    = f"{fd['precip_mm']:.0f}" if fd['precip_mm'] is not None else '0'
            if fd['frost_risk']:
                badge = ('<span style="background:#172554;color:#93c5fd;padding:3px 8px;'
                         'border-radius:12px;font-size:10px;font-weight:700;">&#10052; Frost</span>')
            elif fd.get('desc') not in ('Clear', 'Mostly clear', 'Partly cloudy', 'Overcast', 'Variable'):
                badge = (f'<span style="background:#dbeafe;color:#1e3a8a;padding:3px 8px;'
                         f'border-radius:12px;font-size:10px;font-weight:700;">{fd["desc"]}</span>')
            else:
                badge = ('<span style="background:#f1f5f9;color:#475569;padding:3px 8px;'
                         'border-radius:12px;font-size:10px;font-weight:700;">Fine</span>')
            r_pad = 'padding-right:4px;' if i < 4 else ''
            l_pad = 'padding-left:4px;'  if i > 0 else ''
            cols.append(f"""<td width="20%" style="{r_pad}{l_pad}vertical-align:top;">
          <table width="100%" cellpadding="12" cellspacing="0"
              bgcolor="{bg_col}"
              style="background-color:{bg_col};border:{bdr_style};border-radius:10px;text-align:center;">
            <tr><td>
              <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
                  color:{lbl_color};margin-bottom:2px;">{heading}</div>
              <div style="font-size:11px;color:#374151;margin-bottom:8px;">{subdate}</div>
              <div style="font-size:28px;margin-bottom:6px;">{fd['icon']}</div>
              <div style="font-size:15px;font-weight:700;color:#111827;">{max_c}° / {min_c}°</div>
              <div style="font-size:11px;color:#64748b;margin-top:4px;">{precip} mm</div>
              <div style="margin-top:8px;">{badge}</div>
            </td></tr>
          </table>
        </td>""")

        forecast_html = f"""
  {sec_header('5', '5-Day Forecast', 'Open-Meteo forecast for Wagga Wagga')}
  <tr><td style="background:white;padding:20px 24px 28px;border-radius:0 0 10px 10px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>{''.join(cols)}</tr>
    </table>
  </td></tr>"""

    # Section 4: 12-hour hourly forecast strip
    hourly_html = ''
    if hourly_forecast:
        _SPRAY_CHIP = {
            'GO':      ('#15803d', 'GO'),
            'CAUTION': ('#d97706', 'CAUT'),
            'NO-GO':   ('#dc2626', 'NO'),
        }
        hour_cols = []
        hour_spray = []   # (hour_label, status) for the windows summary
        for hf in hourly_forecast:
            temp_str  = f"{hf['temp_c']:.0f}°" if hf['temp_c'] is not None else '--°'
            wind_str  = f"{hf['wind_kmh']:.0f}" if hf['wind_kmh'] is not None else '--'
            precip_pct = hf['precip_pct']
            precip_col = '#1d4ed8' if precip_pct >= 60 else ('#6b7280' if precip_pct >= 30 else '#94a3b8')
            # Forecast spray status for this hour (Delta T + temp + wind rules)
            spray_chip = ''
            if hf['temp_c'] is not None and hf.get('rh') is not None:
                dt_v   = delta_t(hf['temp_c'], hf['rh'])
                status = spray_status(dt_v, hf['temp_c'], hf['wind_kmh'])
                hour_spray.append((hf['label'], status))
                s_col, s_lbl = _SPRAY_CHIP[status]
                spray_chip = (f'<div style="font-size:9px;font-weight:700;color:white;'
                              f'background:{s_col};border-radius:8px;padding:2px 0;'
                              f'margin-top:4px;">{s_lbl}</div>')
            hour_cols.append(f"""<td style="text-align:center;padding:8px 4px;vertical-align:top;border-right:1px solid #e2e8f0;">
              <div style="font-size:10px;font-weight:700;color:#64748b;letter-spacing:0.5px;margin-bottom:4px;">{hf['label']}</div>
              <div style="font-size:20px;margin-bottom:4px;">{hf['icon']}</div>
              <div style="font-size:13px;font-weight:700;color:#111827;margin-bottom:3px;">{temp_str}</div>
              <div style="font-size:11px;color:{precip_col};font-weight:600;margin-bottom:3px;">{precip_pct}%</div>
              <div style="font-size:10px;color:#64748b;">{wind_str} km/h</div>
              {spray_chip}
            </td>""")

        # Contiguous GO ranges -> "Spray windows today: 6 AM - 9 AM, 4 PM - 6 PM"
        spray_windows_note = ''
        if hour_spray:
            ranges, start, prev = [], None, None
            for lbl, status in hour_spray:
                if status == 'GO':
                    if start is None:
                        start = lbl
                    prev = lbl
                else:
                    if start is not None:
                        ranges.append((start, prev))
                        start = None
            if start is not None:
                ranges.append((start, prev))
            if ranges:
                win_str = ', '.join(a if a == b else f'{a} - {b}' for a, b in ranges)
                spray_windows_note = (f'<div style="font-size:12px;color:#15803d;font-weight:700;'
                                      f'margin-top:8px;padding-left:2px;">'
                                      f'Spray windows today: {win_str}</div>')
            else:
                spray_windows_note = ('<div style="font-size:12px;color:#dc2626;font-weight:700;'
                                      'margin-top:8px;padding-left:2px;">'
                                      'No spray-safe windows forecast between 6 AM and 6 PM today.</div>')

        hourly_html = f"""
  {sec_header('4', "Today's 12-Hour Forecast", 'Hourly conditions 6 AM – 6 PM &bull; Open-Meteo')}
  <tr><td style="background:white;padding:16px 24px 24px;border-radius:0 0 10px 10px;">
    <table width="100%" cellpadding="0" cellspacing="0"
        style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;">
      <tr>{''.join(hour_cols)}</tr>
    </table>
    {spray_windows_note}
    <div style="font-size:11px;color:#94a3b8;margin-top:6px;padding-left:2px;">
      Precipitation % is chance of rain. Wind in km/h. GO / CAUT / NO = forecast spray condition (Delta T and wind).
    </div>
  </td></tr>"""

    # Section 3 (now renumbered 6): Water Tank (from FarmBot, if data available for yesterday)
    fb_snapshot = load_farmbot_snapshot()
    tank_pct    = safe_float(row.get('tank_pct'))
    tank_vol_l  = safe_float(row.get('tank_volume_l'))
    tank_used_l = safe_float(row.get('tank_used_l'))
    tank_refill = row.get('tank_refill') in (True, 'True')
    current_pct = safe_float(fb_snapshot.get('tank_pct'))  # live reading for context
    tank_section_html = ''
    if tank_pct is not None:
        tank_vol_kl  = (tank_vol_l or 0) / 1000
        tank_used_kl = (tank_used_l or 0) / 1000
        bar_pct      = min(100, max(0, tank_pct))
        bar_color    = '#dc2626' if tank_pct < 20 else ('#d97706' if tank_pct < 40 else '#1a4a2e')
        status_label = 'Critical' if tank_pct < 20 else ('Low' if tank_pct < 40 else ('Good' if tank_pct < 80 else 'Full'))
        refill_note  = '<span style="color:#15803d;font-weight:700;">&#x2713; Refill detected</span>' if tank_refill else ''
        live_note    = f' (live: {current_pct:.0f}%)' if current_pct is not None else ''
        tank_section_html = f"""
  {sec_header('3', 'Water Tank', f'End-of-day level for yesterday{live_note}')}
  <tr><td style="background:white;padding:20px 24px 28px;border-radius:0 0 10px 10px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="40%" style="padding-right:12px;vertical-align:middle;text-align:center;">
          <div style="font-size:56px;font-weight:700;color:{bar_color};line-height:1;">{tank_pct:.0f}%</div>
          <div style="font-size:12px;color:#64748b;margin-top:4px;">{tank_vol_kl:.1f} kL of 250 kL</div>
          <div style="margin-top:10px;background:#e2e8f0;border-radius:6px;height:10px;overflow:hidden;">
            <div style="width:{bar_pct:.0f}%;height:100%;background:{bar_color};border-radius:6px;"></div>
          </div>
          <div style="font-size:11px;font-weight:700;color:{bar_color};margin-top:6px;">{status_label}</div>
        </td>
        <td width="60%" style="vertical-align:middle;">
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr>
              <td style="padding:8px 0;border-bottom:1px solid #f1f5f9;">
                <span style="font-size:11px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:1px;">Used Yesterday</span>
                <div style="font-size:18px;font-weight:700;color:#111827;">{tank_used_kl:.1f} kL</div>
              </td>
            </tr>
            <tr>
              <td style="padding:8px 0;">
                <span style="font-size:11px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:1px;">Status</span>
                <div style="font-size:14px;font-weight:600;color:{bar_color};margin-top:2px;">{status_label} {refill_note}</div>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </td></tr>"""

    # GDD season totals from history (season starts 1 Sep)
    gdd_bent_season = 0.0
    gdd_kik_season  = 0.0
    season_year = target_date.year if target_date.month >= 9 else target_date.year - 1
    season_start_str = f'{season_year}-09-01'
    for h in history:
        h_date = h.get('date', '')
        if h_date >= season_start_str:
            gdd_bent_season += safe_float(h.get('gdd_bent'), 0)
            gdd_kik_season  += safe_float(h.get('gdd_kik'), 0)
    gdd_bent_season += safe_float(row.get('gdd_bent'), 0)
    gdd_kik_season  += safe_float(row.get('gdd_kik'), 0)

    lake_data = _load_lake_data()
    lake_section_html = _build_lake_section_daily(lake_data, target_date, section_num=2)

    # Build disease+spray section as a variable (placed after forecast in the output)
    disease_spray_html = f"""
  {sec_header('6', 'Disease Risk', 'Yesterday actuals + estimated outlook from forecast')}

  <tr><td style="background:white;padding:20px 24px;border-radius:0 0 10px 10px;">
    <div style="margin-bottom:14px;">
      <span style="background:#d1fae5;border:1px solid #6ee7b7;color:#065f46;
          padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;display:inline-block;">
          &#127807; Leaf wetness yesterday: {row['leaf_wet_hours']} hrs</span>
    </div>
    <table width="100%" cellpadding="0" cellspacing="0"
        style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;">
      <tr bgcolor="#1a4a2e" style="background-color:#1a4a2e;">
        <th style="padding:10px 14px;text-align:left;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;width:34%;">Disease</th>
        <th style="padding:10px 14px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;width:22%;">Yesterday</th>
        <th style="padding:10px 14px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;width:22%;">Today est.</th>
        <th style="padding:10px 14px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">Tomorrow est.</th>
      </tr>
      <tr style="background:{ds_bg};">
        <td style="padding:11px 14px;font-size:13px;font-weight:600;color:#1f2937;
            border-bottom:1px solid #e2e8f0;">Dollar Spot<br>
            <span style="font-size:11px;color:#64748b;font-weight:400;">{row['dollar_spot_pct']}% probability</span></td>
        <td style="padding:11px 14px;border-bottom:1px solid #e2e8f0;text-align:center;">
            {risk_badge(row['dollar_spot_risk'])}</td>
        <td style="padding:11px 14px;border-bottom:1px solid #e2e8f0;text-align:center;">
            {outlook_badge(out0['dollar_spot'] if out0 else None)}</td>
        <td style="padding:11px 14px;border-bottom:1px solid #e2e8f0;text-align:center;">
            {outlook_badge(out1['dollar_spot'] if out1 else None)}</td>
      </tr>
      <tr style="background:{fs_bg};">
        <td style="padding:11px 14px;font-size:13px;font-weight:600;color:#1f2937;
            border-bottom:1px solid #e2e8f0;">Fusarium Patch<br>
            <span style="font-size:11px;color:#64748b;font-weight:400;">Score: {row['fusarium_score']}</span></td>
        <td style="padding:11px 14px;border-bottom:1px solid #e2e8f0;text-align:center;">
            {risk_badge(row['fusarium_risk'])}</td>
        <td style="padding:11px 14px;border-bottom:1px solid #e2e8f0;text-align:center;">
            {outlook_badge(out0['fusarium'] if out0 else None)}</td>
        <td style="padding:11px 14px;border-bottom:1px solid #e2e8f0;text-align:center;">
            {outlook_badge(out1['fusarium'] if out1 else None)}</td>
      </tr>
      <tr style="background:{bp_bg};">
        <td style="padding:11px 14px;font-size:13px;font-weight:600;color:#1f2937;
            border-bottom:1px solid #e2e8f0;">Brown Patch<br>
            <span style="font-size:11px;color:#64748b;font-weight:400;">Night humidity model</span></td>
        <td style="padding:11px 14px;border-bottom:1px solid #e2e8f0;text-align:center;">
            {risk_badge(row['brown_patch_risk'])}</td>
        <td style="padding:11px 14px;border-bottom:1px solid #e2e8f0;text-align:center;">
            {outlook_badge(out0['brown_patch'] if out0 else None)}</td>
        <td style="padding:11px 14px;border-bottom:1px solid #e2e8f0;text-align:center;">
            {outlook_badge(out1['brown_patch'] if out1 else None)}</td>
      </tr>
      <tr style="background:{py_bg};">
        <td style="padding:11px 14px;font-size:13px;font-weight:600;color:#1f2937;">
            Pythium Blight<br>
            <span style="font-size:11px;color:#64748b;font-weight:400;">Night temp and wetness</span></td>
        <td style="padding:11px 14px;text-align:center;">
            {risk_badge(row['pythium_risk'])}</td>
        <td style="padding:11px 14px;text-align:center;">
            {outlook_badge(out0['pythium'] if out0 else None)}</td>
        <td style="padding:11px 14px;text-align:center;">
            {outlook_badge(out1['pythium'] if out1 else None)}</td>
      </tr>
    </table>
    <div style="font-size:11px;color:#94a3b8;margin-top:8px;padding-left:2px;">
        Today and tomorrow estimates are model projections based on forecast temperature and rainfall.
    </div>
  </td></tr>

  {sec_header('7', 'Spray Conditions', 'Hours yesterday classified by Delta T and wind thresholds')}

  <tr><td style="background:white;padding:20px 24px 28px;border-radius:0 0 10px 10px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td width="33%" style="padding-right:6px;text-align:center;">
          <table width="100%" cellpadding="0" cellspacing="0"
              style="background:#dcfce7;border:2px solid #86efac;border-radius:12px;
              text-align:center;padding:20px 8px;">
            <tr><td style="padding-bottom:8px;">
              <span style="background:#15803d;color:white;padding:3px 10px;
                  border-radius:20px;font-size:11px;font-weight:700;display:inline-block;">
                  GO</span></td></tr>
            <tr><td style="font-size:44px;font-weight:700;color:#15803d;
                line-height:1;padding:8px 0;">{row['spray_go_hours']}</td></tr>
            <tr><td style="font-size:12px;color:#166534;font-weight:600;">hours</td></tr>
          </table>
        </td>
        <td width="33%" style="padding:0 3px;text-align:center;">
          <table width="100%" cellpadding="0" cellspacing="0"
              style="background:#fef9c3;border:2px solid #fde047;border-radius:12px;
              text-align:center;padding:20px 8px;">
            <tr><td style="padding-bottom:8px;">
              <span style="background:#a16207;color:white;padding:3px 10px;
                  border-radius:20px;font-size:11px;font-weight:700;display:inline-block;">
                  CAUTION</span></td></tr>
            <tr><td style="font-size:44px;font-weight:700;color:#a16207;
                line-height:1;padding:8px 0;">{row['spray_caution_hours']}</td></tr>
            <tr><td style="font-size:12px;color:#92400e;font-weight:600;">hours</td></tr>
          </table>
        </td>
        <td width="33%" style="padding-left:6px;text-align:center;">
          <table width="100%" cellpadding="0" cellspacing="0"
              style="background:#fee2e2;border:2px solid #fca5a5;border-radius:12px;
              text-align:center;padding:20px 8px;">
            <tr><td style="padding-bottom:8px;">
              <span style="background:#dc2626;color:white;padding:3px 10px;
                  border-radius:20px;font-size:11px;font-weight:700;display:inline-block;">
                  NO-GO</span></td></tr>
            <tr><td style="font-size:44px;font-weight:700;color:#dc2626;
                line-height:1;padding:8px 0;">{row['spray_nogo_hours']}</td></tr>
            <tr><td style="font-size:12px;color:#991b1b;font-weight:600;">hours</td></tr>
          </table>
        </td>
      </tr>
    </table>
  </td></tr>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{_EMAIL_MOBILE_STYLE}
</head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;">
{_EMAIL_MOBILE_STYLE}
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;padding:28px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
    style="max-width:600px;width:100%;background:white;border-radius:14px;overflow:hidden;
    box-shadow:0 4px 20px rgba(0,0,0,0.08);">

  <!-- HEADER -->
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:22px 20px 16px 20px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td valign="middle" class="mob-logo-cell" style="padding-right:16px;white-space:nowrap;width:1%;">
          {_white_logo_html()}
        </td>
        <td valign="top" class="mob-text-cell">
          <p style="margin:0;font-size:10px;color:#a8d8bc;letter-spacing:1.5px;
              text-transform:uppercase;font-family:Arial,sans-serif;">
            WAGGA WAGGA COUNTRY CLUB
          </p>
          <h1 style="margin:8px 0 0 0;font-size:22px;color:#ffffff;font-weight:bold;
              font-family:Arial,sans-serif;">Daily Weather &amp; Course Snapshot</h1>
          <p style="margin:6px 0 0 0;font-size:13px;color:#a8d8bc;
              font-family:Arial,sans-serif;">{yesterday_str}</p>
        </td>
      </tr>
    </table>
  </td></tr>

  {frost_banner}
  {disease_banner}
  {fog_banner}
  {lightning_banner}

  {sec_header('1', "Yesterday's Weather", f'Key weather measurements for {yesterday_str}')}

  <tr><td style="background:white;padding:20px 24px 20px;border-radius:0 0 10px 10px;">
    {_gk_kv_table([r for r in [
        ('Max Temp',  f"{row['temp_max']}°C" + (f" <span style='color:#64748b;font-size:11px;'>({row['temp_max_time']})</span>" if row.get('temp_max_time') and str(row.get('temp_max_time','')) not in ('','None') else '')) if row.get('temp_max') is not None and row['temp_max'] not in ('', 'None') else None,
        ('Min Temp',  f"{row['temp_min']}°C" + (f" <span style='color:#64748b;font-size:11px;'>({row['temp_min_time']})</span>" if row.get('temp_min_time') and str(row.get('temp_min_time','')) not in ('','None') else '')) if row.get('temp_min') is not None and row['temp_min'] not in ('', 'None') else None,
        ('Mean Temp',        f"{row['temp_mean']}°C") if row.get('temp_mean') is not None and row['temp_mean'] not in ('', 'None') else None,
        ('Relative Humidity', f"{row['rh_mean']}%") if row.get('rh_mean') is not None and row['rh_mean'] not in ('', 'None') else None,
        ('Overnight Low',    f"{night_min_str}°C") if night_min_str != '--' else None,
        ('Rainfall',         f"{row['rain_mm']} mm") if row.get('rain_mm') is not None and row['rain_mm'] not in ('', 'None') else None,
        ('ET',               f"{row['et_mm']} mm &nbsp;&bull;&nbsp; Net {net_str} mm") if row.get('et_mm') is not None and row['et_mm'] not in ('', 'None') else None,
        ('Wind Max',         f"{row['wind_max_kmh']} km/h") if row.get('wind_max_kmh') is not None and row['wind_max_kmh'] not in ('', 'None') else None,
        ('Wind Mean',        f"{row['wind_mean_kmh']} km/h") if row.get('wind_mean_kmh') is not None and row['wind_mean_kmh'] not in ('', 'None') else None,
        ('Delta T',          f"{row['delta_t_mean']}°C") if row.get('delta_t_mean') is not None and row['delta_t_mean'] not in ('', 'None') else None,
        ('Soil Moisture',    soil_zone_val) if soil_zone_val and soil_zone_val not in ('', 'None') else None,
        ('7-Day Balance',    f"{soil_bal:+.1f} mm"),
        ('UV Index',         str(uv_str)) if uv_str != '--' else None,
        ('Pressure',         f"{pres_str} hPa") if pres_str != '--' else None,
        ('Leaf Wet Hours',   f"{row['leaf_wet_hours']} hrs") if row.get('leaf_wet_hours') is not None and row['leaf_wet_hours'] not in ('', 'None') else None,
        ('Dew Point',        f"{row['dew_point_c']}°C") if row.get('dew_point_c') is not None and str(row.get('dew_point_c', '')).strip() not in ('', 'None') else None,
        ('Solar Radiation',  f"{row['solar_rad_avg']} W/m²") if row.get('solar_rad_avg') is not None and str(row.get('solar_rad_avg', '')).strip() not in ('', 'None') else None,
        ('Station Reception', f"{row['iss_reception']}%") if row.get('iss_reception') is not None and str(row.get('iss_reception', '')).strip() not in ('', 'None') else None,
    ] if r is not None])}
  </td></tr>

  {lake_section_html}

  {tank_section_html}

  {hourly_html}

  {forecast_html}

  {disease_spray_html}

  {sec_header('8', 'Growing Degree Days', 'Heat accumulation for grass growth &bull; Season from 1 September')}

  <tr><td style="background:white;padding:20px 24px 28px;border-radius:0 0 10px 10px;">
    <p style="margin:0 0 14px 0;font-size:13px;color:#374151;font-family:Arial,sans-serif;">
      Growing Degree Days (GDD) measure heat accumulation - higher values mean faster
      grass growth and increased disease pressure.
    </p>
    {_gk_kv_table([
        ('Bentgrass (base 10°C)', f"{row['gdd_bent']} GDD today &nbsp;&middot;&nbsp; {gdd_bent_season:.0f} GDD this season"),
        ('Warmseason (base 15°C)', f"{row['gdd_kik']} GDD today &nbsp;&middot;&nbsp; {gdd_kik_season:.0f} GDD this season"),
    ])}
  </td></tr>

  <!-- FOOTER -->
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:22px 28px;text-align:center;">
    <div style="font-size:10px;color:#6ee7b7;letter-spacing:2px;text-transform:uppercase;
        margin-bottom:14px;">Wagga Wagga Country Club - Automated Daily Report</div>
    <!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="https://bidgee182.github.io/wwcc-weather-page/?gk=1" style="height:38px;v-text-anchor:middle;width:244px;" arcsize="50%" stroke="f" fillcolor="#4caf7d"><w:anchorlock/><center style="color:white;font-family:Arial,sans-serif;font-size:12px;font-weight:bold;letter-spacing:0.5px;">&#9971; Open Greenkeeper Dashboard</center></v:roundrect><![endif]--><!--[if !mso]><!-->
    <a href="https://bidgee182.github.io/wwcc-weather-page/?gk=1"
        style="display:inline-block;background:#4caf7d;color:white;text-decoration:none;
        font-size:12px;font-weight:700;padding:10px 26px;border-radius:20px;
        letter-spacing:0.5px;">&#9971; &nbsp;Open Greenkeeper Dashboard</a>
    <!--<![endif]-->
    <div style="font-size:11px;color:rgba(255,255,255,0.35);margin-top:12px;">
        Davis WeatherLink - Open-Meteo archive and forecast</div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""
    return html


def build_weekly_html(history, week_end_date):
    """Generate styled HTML for the weekly summary email."""
    date_str    = _fmt_d(week_end_date, 'Week ending %A, %-d %B %Y')
    totals      = {'rain': 0.0, 'et': 0.0, 'gdd_bent': 0.0, 'gdd_kik': 0.0, 'tank_used_l': 0.0}
    alert_days  = 0
    frost_days  = 0
    rows_html   = ''

    for i, row in enumerate(history):
        bg = '#f8fafc' if i % 2 == 0 else 'white'
        ds  = row.get('dollar_spot_risk', '')
        fus = row.get('fusarium_risk', '')
        if row.get('disease_alert') in ('True', True): alert_days += 1
        if row.get('frost_flag')    in ('True', True): frost_days += 1
        totals['rain']         += safe_float(row.get('rain_mm'), 0)
        totals['et']           += safe_float(row.get('et_mm'), 0)
        totals['gdd_bent']     += safe_float(row.get('gdd_bent'), 0)
        totals['gdd_kik']      += safe_float(row.get('gdd_kik'), 0)
        totals['tank_used_l']  += safe_float(row.get('tank_used_l'), 0)
        ds_cell  = risk_badge(ds)  if ds  else '--'
        fus_cell = risk_badge(fus) if fus else '--'
        tank_pct = safe_float(row.get('tank_pct'))
        tank_cell = f'{tank_pct:.0f}%' if tank_pct is not None else '--'
        rows_html += f"""
      <tr style="background:{bg};">
        <td style="padding:9px 12px;font-size:12px;color:#374151;border-bottom:1px solid #f1f5f9;white-space:nowrap;">{row.get('date','')}</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;border-bottom:1px solid #f1f5f9;">{row.get('temp_max','--')}/{row.get('temp_min','--')}°C</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;border-bottom:1px solid #f1f5f9;">{row.get('rain_mm','--')}</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;border-bottom:1px solid #f1f5f9;">{row.get('et_mm','--')}</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;border-bottom:1px solid #f1f5f9;">{row.get('gdd_bent','--')}</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;border-bottom:1px solid #f1f5f9;">{ds_cell}</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;border-bottom:1px solid #f1f5f9;">{fus_cell}</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;border-bottom:1px solid #f1f5f9;">{row.get('spray_go_hours','--')} hrs</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;border-bottom:1px solid #f1f5f9;">{tank_cell}</td>
      </tr>"""

    water_bal = totals['rain'] - totals['et']
    w_sec1 = sec_header('1', 'Daily Breakdown',   'Weather, GDD and disease risk for each day this week')
    w_sec2 = sec_header('2', 'Weekly Totals',     'Cumulative water, ET and heat accumulation for the week')
    w_sec3 = sec_header('3', 'Disease &amp; Frost Alerts', 'Days this week where conditions triggered alerts')

    lake_data = _load_lake_data()
    lake_section_html = _build_lake_section_weekly(lake_data, week_end_date)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{_EMAIL_MOBILE_STYLE}
</head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;">
{_EMAIL_MOBILE_STYLE}
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;padding:28px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
    style="max-width:600px;width:100%;background:white;border-radius:14px;overflow:hidden;">

  <!-- COVER HEADER -->
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:22px 20px 16px 20px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td valign="middle" class="mob-logo-cell" style="padding-right:16px;white-space:nowrap;width:1%;">
          {_white_logo_html()}
        </td>
        <td valign="top" class="mob-text-cell">
          <p style="margin:0;font-size:10px;color:#a8d8bc;letter-spacing:1.5px;
              text-transform:uppercase;font-family:Arial,sans-serif;">
            WAGGA WAGGA COUNTRY CLUB
          </p>
          <h1 style="margin:8px 0 0 0;font-size:22px;color:#ffffff;font-weight:bold;
              font-family:Arial,sans-serif;">Weekly Weather &amp; Course Summary</h1>
          <p style="margin:6px 0 0 0;font-size:13px;color:#a8d8bc;
              font-family:Arial,sans-serif;">{date_str}</p>
        </td>
      </tr>
    </table>
  </td></tr>

  {w_sec1}

  <tr><td style="background:white;padding:20px 24px;border-radius:0 0 10px 10px;">
    <table width="100%" cellpadding="0" cellspacing="0"
        style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;font-size:12px;">
      <tr bgcolor="#1a4a2e" style="background-color:#1a4a2e;">
        <th style="padding:10px 12px;text-align:left;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">Date</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">Hi/Lo</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">Rain mm</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">ET mm</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">GDD Bent</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">Dollar Spot</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">Fusarium</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">Spray GO</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">Tank</th>
      </tr>
      {rows_html}
      <tr style="background:#e8f5ee;">
        <td style="padding:10px 12px;font-size:12px;font-weight:700;color:#1a4a2e;">
            7-Day Total</td>
        <td></td>
        <td style="padding:10px 12px;font-size:12px;font-weight:700;color:#1a4a2e;
            text-align:center;">{totals['rain']:.1f} mm</td>
        <td style="padding:10px 12px;font-size:12px;font-weight:700;color:#1a4a2e;
            text-align:center;">{totals['et']:.1f} mm</td>
        <td style="padding:10px 12px;font-size:12px;font-weight:700;color:#1a4a2e;
            text-align:center;">{totals['gdd_bent']:.1f}</td>
        <td></td><td></td><td></td>
        <td style="padding:10px 12px;font-size:12px;font-weight:700;color:#1a4a2e;
            text-align:center;">{totals['tank_used_l']/1000:.1f} kL used</td>
      </tr>
    </table>
  </td></tr>

  {w_sec2}

  <tr><td style="background:white;padding:20px 24px;border-radius:0 0 10px 10px;">
    {_gk_kv_table([
        ('Rainfall',         f"{totals['rain']:.1f} mm"),
        ('Evapotranspiration', f"{totals['et']:.1f} mm"),
        ('Water Balance',    f"{water_bal:+.1f} mm"),
        ('GDD Bentgrass',    f"{totals['gdd_bent']:.1f}"),
        ('Tank Water Used',  f"{totals['tank_used_l']/1000:.1f} kL"),
    ])}
  </td></tr>

  {w_sec3}

  <tr><td style="background:white;padding:20px 24px 28px;border-radius:0 0 10px 10px;">
    {_gk_kv_table([
        ('Disease Alert Days', f"{alert_days} {'HIGH or SEVERE risk days' if alert_days > 0 else '- no high-risk days'}"),
        ('Frost Days',         f"{frost_days} {'nights below 2°C' if frost_days > 0 else '- no frost this week'}"),
    ])}
  </td></tr>

  {lake_section_html}

  <!-- FOOTER -->
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:22px 28px;text-align:center;">
    <div style="font-size:10px;color:#6ee7b7;letter-spacing:2px;text-transform:uppercase;
        margin-bottom:14px;">Wagga Wagga Country Club &nbsp;&bull;&nbsp; Automated Weekly Report</div>
    <!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="https://bidgee182.github.io/wwcc-weather-page/?gk=1" style="height:38px;v-text-anchor:middle;width:244px;" arcsize="50%" stroke="f" fillcolor="#4caf7d"><w:anchorlock/><center style="color:white;font-family:Arial,sans-serif;font-size:12px;font-weight:bold;letter-spacing:0.5px;">&#9971; Open Greenkeeper Dashboard</center></v:roundrect><![endif]--><!--[if !mso]><!-->
    <a href="https://bidgee182.github.io/wwcc-weather-page/?gk=1"
        style="display:inline-block;background:#4caf7d;color:white;text-decoration:none;
        font-size:12px;font-weight:700;padding:10px 26px;border-radius:20px;letter-spacing:0.5px;">
        &#9971; &nbsp;Open Greenkeeper Dashboard</a>
    <!--<![endif]-->
    <div style="font-size:11px;color:rgba(255,255,255,0.35);margin-top:12px;">
        Davis WeatherLink &nbsp;&bull;&nbsp; Open-Meteo archive</div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""
    return html


def build_monthly_html(history, month_label):
    """Generate styled HTML for the monthly summary email."""
    totals     = {'rain': 0.0, 'et': 0.0, 'gdd_bent': 0.0, 'gdd_kik': 0.0, 'tank_used_l': 0.0}
    alert_days = 0
    frost_days = 0
    rows_html  = ''

    for i, row in enumerate(history):
        bg = '#f8fafc' if i % 2 == 0 else 'white'
        ds  = row.get('dollar_spot_risk', '')
        fus = row.get('fusarium_risk', '')
        if row.get('disease_alert') in ('True', True): alert_days += 1
        if row.get('frost_flag')    in ('True', True): frost_days += 1
        totals['rain']         += safe_float(row.get('rain_mm'), 0)
        totals['et']           += safe_float(row.get('et_mm'), 0)
        totals['gdd_bent']     += safe_float(row.get('gdd_bent'), 0)
        totals['gdd_kik']      += safe_float(row.get('gdd_kik'), 0)
        totals['tank_used_l']  += safe_float(row.get('tank_used_l'), 0)
        ds_cell  = risk_badge(ds)  if ds  else '--'
        fus_cell = risk_badge(fus) if fus else '--'
        rows_html += f"""
      <tr style="background:{bg};">
        <td style="padding:7px 10px;font-size:11px;color:#374151;border-bottom:1px solid #f1f5f9;white-space:nowrap;">{row.get('date','')}</td>
        <td style="padding:7px 10px;font-size:11px;text-align:center;border-bottom:1px solid #f1f5f9;">{row.get('temp_max','--')}/{row.get('temp_min','--')}°C</td>
        <td style="padding:7px 10px;font-size:11px;text-align:center;border-bottom:1px solid #f1f5f9;">{row.get('rain_mm','--')}</td>
        <td style="padding:7px 10px;font-size:11px;text-align:center;border-bottom:1px solid #f1f5f9;">{row.get('et_mm','--')}</td>
        <td style="padding:7px 10px;font-size:11px;text-align:center;border-bottom:1px solid #f1f5f9;">{row.get('gdd_bent','--')}</td>
        <td style="padding:7px 10px;font-size:11px;text-align:center;border-bottom:1px solid #f1f5f9;">{ds_cell}</td>
        <td style="padding:7px 10px;font-size:11px;text-align:center;border-bottom:1px solid #f1f5f9;">{fus_cell}</td>
      </tr>"""

    water_bal = totals['rain'] - totals['et']
    m_sec1 = sec_header('1', 'Daily Records',          'Complete daily log for the month')
    m_sec2 = sec_header('2', 'Monthly Totals',         'Cumulative water, evapotranspiration and heat for the month')
    m_sec3 = sec_header('3', 'Disease &amp; Frost Summary', 'Alert days recorded during the month')
    m_sec4 = sec_header('4', 'Water Tank',             'FarmBot tank usage for the month')

    lake_data = _load_lake_data()
    lake_section_html = _build_lake_section_monthly(lake_data, month_label)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{_EMAIL_MOBILE_STYLE}
</head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;">
{_EMAIL_MOBILE_STYLE}
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;padding:28px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
    style="max-width:600px;width:100%;background:white;border-radius:14px;overflow:hidden;">

  <!-- COVER HEADER -->
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:22px 20px 16px 20px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td valign="middle" class="mob-logo-cell" style="padding-right:16px;white-space:nowrap;width:1%;">
          {_white_logo_html()}
        </td>
        <td valign="top" class="mob-text-cell">
          <p style="margin:0;font-size:10px;color:#a8d8bc;letter-spacing:1.5px;
              text-transform:uppercase;font-family:Arial,sans-serif;">
            WAGGA WAGGA COUNTRY CLUB
          </p>
          <h1 style="margin:8px 0 0 0;font-size:22px;color:#ffffff;font-weight:bold;
              font-family:Arial,sans-serif;">Monthly Weather &amp; Course Report</h1>
          <p style="margin:6px 0 0 0;font-size:13px;color:#a8d8bc;
              font-family:Arial,sans-serif;">{month_label}</p>
        </td>
      </tr>
    </table>
  </td></tr>

  {m_sec1}

  <tr><td style="background:white;padding:20px 24px;border-radius:0 0 10px 10px;">
    <table width="100%" cellpadding="0" cellspacing="0"
        style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;font-size:11px;">
      <tr style="background:#1a4a2e;">
        <th style="padding:9px 10px;text-align:left;color:white;font-size:10px;
            font-weight:700;letter-spacing:0.5px;">Date</th>
        <th style="padding:9px 10px;text-align:center;color:white;font-size:10px;
            font-weight:700;letter-spacing:0.5px;">Hi/Lo</th>
        <th style="padding:9px 10px;text-align:center;color:white;font-size:10px;
            font-weight:700;letter-spacing:0.5px;">Rain mm</th>
        <th style="padding:9px 10px;text-align:center;color:white;font-size:10px;
            font-weight:700;letter-spacing:0.5px;">ET mm</th>
        <th style="padding:9px 10px;text-align:center;color:white;font-size:10px;
            font-weight:700;letter-spacing:0.5px;">GDD Bent</th>
        <th style="padding:9px 10px;text-align:center;color:white;font-size:10px;
            font-weight:700;letter-spacing:0.5px;">Dollar Spot</th>
        <th style="padding:9px 10px;text-align:center;color:white;font-size:10px;
            font-weight:700;letter-spacing:0.5px;">Fusarium</th>
      </tr>
      {rows_html}
      <tr style="background:#e8f5ee;">
        <td style="padding:9px 10px;font-size:11px;font-weight:700;color:#1a4a2e;">
            Monthly Total</td>
        <td></td>
        <td style="padding:9px 10px;font-size:11px;font-weight:700;color:#1a4a2e;
            text-align:center;">{totals['rain']:.1f} mm</td>
        <td style="padding:9px 10px;font-size:11px;font-weight:700;color:#1a4a2e;
            text-align:center;">{totals['et']:.1f} mm</td>
        <td style="padding:9px 10px;font-size:11px;font-weight:700;color:#1a4a2e;
            text-align:center;">{totals['gdd_bent']:.1f}</td>
        <td></td><td></td>
      </tr>
    </table>
  </td></tr>

  {m_sec2}

  <tr><td style="background:white;padding:20px 24px;border-radius:0 0 10px 10px;">
    {_gk_kv_table([
        ('Rainfall',           f"{totals['rain']:.0f} mm"),
        ('Evapotranspiration', f"{totals['et']:.0f} mm"),
        ('Water Balance',      f"{water_bal:+.0f} mm"),
        ('GDD Bentgrass',      f"{totals['gdd_bent']:.0f} (base 10°C)"),
        ('GDD Kikuyu',         f"{totals['gdd_kik']:.0f} (base 15°C)"),
    ])}
  </td></tr>

  {m_sec3}

  <tr><td style="background:white;padding:20px 24px 28px;border-radius:0 0 10px 10px;">
    {_gk_kv_table([
        ('Disease Alert Days', f"{alert_days} {'days with HIGH or SEVERE risk' if alert_days > 0 else '- no high-risk days'}"),
        ('Frost Days',         f"{frost_days} {'nights below 2°C' if frost_days > 0 else '- no frost this month'}"),
    ])}
  </td></tr>

  {m_sec4}

  <tr><td style="background:white;padding:20px 24px 28px;border-radius:0 0 10px 10px;">
    {_gk_kv_table([
        ('Total Used',    f"{totals['tank_used_l']/1000:.0f} kL this month"),
        ('Daily Average', f"{(totals['tank_used_l']/1000/max(len(history),1)):.1f} kL per day"),
    ])}
  </td></tr>

  {lake_section_html}

  <!-- FOOTER -->
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:22px 28px;text-align:center;">
    <div style="font-size:10px;color:#6ee7b7;letter-spacing:2px;text-transform:uppercase;
        margin-bottom:14px;">Wagga Wagga Country Club &nbsp;&bull;&nbsp; Automated Monthly Report</div>
    <!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="https://bidgee182.github.io/wwcc-weather-page/?gk=1" style="height:38px;v-text-anchor:middle;width:244px;" arcsize="50%" stroke="f" fillcolor="#4caf7d"><w:anchorlock/><center style="color:white;font-family:Arial,sans-serif;font-size:12px;font-weight:bold;letter-spacing:0.5px;">&#9971; Open Greenkeeper Dashboard</center></v:roundrect><![endif]--><!--[if !mso]><!-->
    <a href="https://bidgee182.github.io/wwcc-weather-page/?gk=1"
        style="display:inline-block;background:#4caf7d;color:white;text-decoration:none;
        font-size:12px;font-weight:700;padding:10px 26px;border-radius:20px;letter-spacing:0.5px;">
        &#9971; &nbsp;Open Greenkeeper Dashboard</a>
    <!--<![endif]-->
    <div style="font-size:11px;color:rgba(255,255,255,0.35);margin-top:12px;">
        Davis WeatherLink &nbsp;&bull;&nbsp; Open-Meteo archive</div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""
    return html


def build_yearly_html(history, year_label):
    """Generate styled HTML for the annual summary email - monthly aggregates."""
    from collections import defaultdict
    months = defaultdict(lambda: {'rain': 0.0, 'et': 0.0, 'gdd_bent': 0.0,
                                   'gdd_kik': 0.0, 'alert_days': 0, 'frost_days': 0,
                                   'days': 0})
    for row in history:
        d = row.get('date', '')
        if not d or len(d) < 7:
            continue
        month_key = d[:7]  # YYYY-MM
        m = months[month_key]
        m['rain']      += safe_float(row.get('rain_mm'), 0)
        m['et']        += safe_float(row.get('et_mm'), 0)
        m['gdd_bent']  += safe_float(row.get('gdd_bent'), 0)
        m['gdd_kik']   += safe_float(row.get('gdd_kik'), 0)
        m['days']      += 1
        if row.get('disease_alert') in ('True', True): m['alert_days'] += 1
        if row.get('frost_flag')    in ('True', True): m['frost_days'] += 1

    year_totals = {'rain': 0.0, 'et': 0.0, 'gdd_bent': 0.0, 'gdd_kik': 0.0,
                   'alert_days': 0, 'frost_days': 0}
    rows_html = ''
    month_names = {
        '01':'January','02':'February','03':'March','04':'April',
        '05':'May','06':'June','07':'July','08':'August',
        '09':'September','10':'October','11':'November','12':'December'
    }

    for i, (mk, m) in enumerate(sorted(months.items())):
        bg = '#f8fafc' if i % 2 == 0 else 'white'
        mn = month_names.get(mk[5:7], mk)
        bal = m['rain'] - m['et']
        bal_col = '#065f46' if bal >= 0 else '#dc2626'
        year_totals['rain']       += m['rain']
        year_totals['et']         += m['et']
        year_totals['gdd_bent']   += m['gdd_bent']
        year_totals['gdd_kik']    += m['gdd_kik']
        year_totals['alert_days'] += m['alert_days']
        year_totals['frost_days'] += m['frost_days']
        ad_cell = (f'<span style="background:#fee2e2;color:#991b1b;padding:2px 8px;'
                   f'border-radius:4px;font-size:11px;font-weight:700;">{m["alert_days"]}</span>'
                   if m['alert_days'] > 0 else
                   f'<span style="color:#94a3b8;font-size:11px;">0</span>')
        rows_html += f"""
      <tr style="background:{bg};">
        <td style="padding:9px 12px;font-size:12px;font-weight:600;color:#374151;
            border-bottom:1px solid #f1f5f9;">{mn}</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;
            border-bottom:1px solid #f1f5f9;">{m['rain']:.0f} mm</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;
            border-bottom:1px solid #f1f5f9;">{m['et']:.0f} mm</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;
            color:{bal_col};font-weight:600;border-bottom:1px solid #f1f5f9;">{bal:+.0f} mm</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;
            border-bottom:1px solid #f1f5f9;">{m['gdd_bent']:.0f}</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;
            border-bottom:1px solid #f1f5f9;">{m['gdd_kik']:.0f}</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;
            border-bottom:1px solid #f1f5f9;">{ad_cell}</td>
        <td style="padding:9px 12px;font-size:12px;text-align:center;
            border-bottom:1px solid #f1f5f9;color:#64748b;">{m['frost_days']}</td>
      </tr>"""

    annual_bal = year_totals['rain'] - year_totals['et']

    lake_data = _load_lake_data()
    lake_section_html = _build_lake_section_yearly(lake_data, year_label)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{_EMAIL_MOBILE_STYLE}
</head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;">
{_EMAIL_MOBILE_STYLE}
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;padding:28px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
    style="max-width:600px;width:100%;background:white;border-radius:14px;overflow:hidden;">

  <!-- COVER HEADER -->
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:22px 20px 16px 20px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td valign="middle" class="mob-logo-cell" style="padding-right:16px;white-space:nowrap;width:1%;">
          {_white_logo_html()}
        </td>
        <td valign="top" class="mob-text-cell">
          <p style="margin:0;font-size:10px;color:#a8d8bc;letter-spacing:1.5px;
              text-transform:uppercase;font-family:Arial,sans-serif;">
            WAGGA WAGGA COUNTRY CLUB
          </p>
          <h1 style="margin:8px 0 0 0;font-size:22px;color:#ffffff;font-weight:bold;
              font-family:Arial,sans-serif;">Annual Weather &amp; Course Report</h1>
          <p style="margin:6px 0 0 0;font-size:13px;color:#a8d8bc;
              font-family:Arial,sans-serif;">{year_label}</p>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- SECTION 1: MONTHLY BREAKDOWN TABLE -->
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;background:linear-gradient(135deg,#1a4a2e,#2d7a4e);padding:16px 24px;">
    <span style="display:inline-block;width:28px;height:28px;line-height:28px;
        text-align:center;border-radius:50%;background:rgba(255,255,255,0.2);
        color:white;font-weight:700;font-size:13px;margin-right:10px;
        vertical-align:middle;">1</span>
    <span style="color:white;font-size:15px;font-weight:700;vertical-align:middle;">
        Monthly Breakdown</span>
    <div style="font-size:12px;color:rgba(255,255,255,0.75);margin-top:4px;
        margin-left:38px;">Aggregated weather totals for each month of the year</div>
  </td></tr>

  <tr><td style="background:white;padding:20px 24px;border-radius:0 0 10px 10px;">
    <table width="100%" cellpadding="0" cellspacing="0"
        style="border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;font-size:12px;">
      <tr style="background:#1a4a2e;">
        <th style="padding:10px 12px;text-align:left;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">Month</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">Rain</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">ET</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">Balance</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">GDD Bent</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">GDD Kik</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">Alert Days</th>
        <th style="padding:10px 12px;text-align:center;color:white;font-size:11px;
            font-weight:700;letter-spacing:0.5px;">Frost</th>
      </tr>
      {rows_html}
      <tr style="background:#e8f5ee;">
        <td style="padding:10px 12px;font-size:12px;font-weight:700;color:#1a4a2e;">Annual Total</td>
        <td style="padding:10px 12px;font-size:12px;font-weight:700;color:#1a4a2e;text-align:center;">{year_totals['rain']:.0f} mm</td>
        <td style="padding:10px 12px;font-size:12px;font-weight:700;color:#1a4a2e;text-align:center;">{year_totals['et']:.0f} mm</td>
        <td style="padding:10px 12px;font-size:12px;font-weight:700;text-align:center;
            color:{'#065f46' if annual_bal >= 0 else '#dc2626'};">{annual_bal:+.0f} mm</td>
        <td style="padding:10px 12px;font-size:12px;font-weight:700;color:#1a4a2e;text-align:center;">{year_totals['gdd_bent']:.0f}</td>
        <td style="padding:10px 12px;font-size:12px;font-weight:700;color:#1a4a2e;text-align:center;">{year_totals['gdd_kik']:.0f}</td>
        <td style="padding:10px 12px;font-size:12px;font-weight:700;color:#dc2626;text-align:center;">{year_totals['alert_days']}</td>
        <td style="padding:10px 12px;font-size:12px;font-weight:700;color:#2563eb;text-align:center;">{year_totals['frost_days']}</td>
      </tr>
    </table>
  </td></tr>

  <!-- SECTION 2: ANNUAL HIGHLIGHTS -->
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;background:linear-gradient(135deg,#1a4a2e,#2d7a4e);padding:16px 24px;">
    <span style="display:inline-block;width:28px;height:28px;line-height:28px;
        text-align:center;border-radius:50%;background:rgba(255,255,255,0.2);
        color:white;font-weight:700;font-size:13px;margin-right:10px;
        vertical-align:middle;">2</span>
    <span style="color:white;font-size:15px;font-weight:700;vertical-align:middle;">
        Annual Highlights</span>
    <div style="font-size:12px;color:rgba(255,255,255,0.75);margin-top:4px;
        margin-left:38px;">Key totals for the full year</div>
  </td></tr>

  <tr><td style="background:white;padding:20px 24px 28px;border-radius:0 0 10px 10px;">
    {_gk_kv_table([
        ('Annual Rainfall',   f"{year_totals['rain']:.0f} mm"),
        ('Annual ET',         f"{year_totals['et']:.0f} mm"),
        ('Water Balance',     f"{(year_totals['rain'] - year_totals['et']):+.0f} mm"),
        ('GDD Bentgrass',     f"{year_totals['gdd_bent']:.0f} (base 10°C)"),
        ('GDD Kikuyu',        f"{year_totals['gdd_kik']:.0f} (base 15°C)"),
        ('Disease Alert Days',f"{year_totals['alert_days']} alert days"),
        ('Frost Days',        f"{year_totals['frost_days']} nights below 2°C"),
    ])}
  </td></tr>

  {lake_section_html}

  <!-- FOOTER -->
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:22px 28px;text-align:center;">
    <div style="font-size:10px;color:#6ee7b7;letter-spacing:2px;text-transform:uppercase;
        margin-bottom:14px;">Wagga Wagga Country Club &nbsp;&bull;&nbsp; Automated Annual Report</div>
    <!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="https://bidgee182.github.io/wwcc-weather-page/?gk=1" style="height:38px;v-text-anchor:middle;width:244px;" arcsize="50%" stroke="f" fillcolor="#4caf7d"><w:anchorlock/><center style="color:white;font-family:Arial,sans-serif;font-size:12px;font-weight:bold;letter-spacing:0.5px;">&#9971; Open Greenkeeper Dashboard</center></v:roundrect><![endif]--><!--[if !mso]><!-->
    <a href="https://bidgee182.github.io/wwcc-weather-page/?gk=1"
        style="display:inline-block;background:#4caf7d;color:white;text-decoration:none;
        font-size:12px;font-weight:700;padding:10px 26px;border-radius:20px;letter-spacing:0.5px;">
        &#9971; &nbsp;Open Greenkeeper Dashboard</a>
    <!--<![endif]-->
    <div style="font-size:11px;color:rgba(255,255,255,0.35);margin-top:12px;">
        Davis WeatherLink &nbsp;&bull;&nbsp; Open-Meteo archive</div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""
    return html


# ─────────────────────────────────────────────────────────────────────────────
# WATER METER REMINDER EMAILS
# ─────────────────────────────────────────────────────────────────────────────

def _gk_data_table(headers, rows):
    """GK-styled data table with green header row."""
    hcells = ''.join(
        f'<td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:8px 12px;'
        f'font-family:Arial,sans-serif;font-size:12px;color:#ffffff;font-weight:bold;'
        f'border-right:1px solid {_GK_BORDER};border-bottom:1px solid {_GK_BORDER};">{h}</td>'
        for h in headers
    )
    drows = ''
    for i, row in enumerate(rows):
        bg = _GK_ROW_A if i % 2 == 0 else _GK_ROW_B
        drows += '<tr>' + ''.join(
            f'<td bgcolor="{bg}" style="background-color:{bg};padding:7px 12px;'
            f'font-family:Arial,sans-serif;font-size:12px;color:{_GK_VAL_COL};'
            f'border-bottom:1px solid {_GK_BORDER};border-right:1px solid {_GK_BORDER};">{c}</td>'
            for c in row
        ) + '</tr>'
    return f"""<table width="100%" cellpadding="0" cellspacing="0"
    style="border-collapse:collapse;border:1px solid {_GK_BORDER};">
  <tr>{hcells}</tr>
  {drows}
</table>"""


def _gk_alert_banner(text, bg='#c0392b'):
    return f"""<tr><td bgcolor="{bg}" style="background-color:{bg};padding:12px 20px;text-align:center;">
  <p style="margin:0;font-size:14px;color:#ffffff;font-weight:bold;
      letter-spacing:1px;font-family:Arial,sans-serif;">&#9888;&nbsp; {text} &nbsp;&#9888;</p>
</td></tr>"""


def _gk_alert_section(text, bg='#c0392b'):
    return f"""<tr><td bgcolor="{bg}" style="background-color:{bg};padding:7px 20px;margin-top:14px;">
  <p style="margin:0;font-size:13px;color:#ffffff;font-weight:bold;
      letter-spacing:0.5px;font-family:Arial,sans-serif;">{text}</p>
</td></tr>"""


def _gk_meter_wrap(title, subtitle, body_rows, now_sydney):
    """Wrap meter email content in GK email shell."""
    now_str = _fmt_d(now_sydney, '%-d %b %Y %H:%M')
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{_EMAIL_MOBILE_STYLE}
</head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;">
{_EMAIL_MOBILE_STYLE}
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;padding:28px 0;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0"
    style="max-width:600px;width:100%;background:white;border-radius:14px;overflow:hidden;
    box-shadow:0 4px 20px rgba(0,0,0,0.08);">

  <!-- HEADER -->
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:22px 20px 16px 20px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td valign="middle" class="mob-logo-cell" style="padding-right:16px;white-space:nowrap;width:1%;">
          {_white_logo_html()}
        </td>
        <td valign="top" class="mob-text-cell">
          <p style="margin:0;font-size:10px;color:#a8d8bc;letter-spacing:1.5px;
              text-transform:uppercase;font-family:Arial,sans-serif;">
            WAGGA WAGGA COUNTRY CLUB
          </p>
          <h1 style="margin:8px 0 0 0;font-size:22px;color:#ffffff;font-weight:bold;
              font-family:Arial,sans-serif;">{title}</h1>
          <p style="margin:6px 0 0 0;font-size:13px;color:#a8d8bc;
              font-family:Arial,sans-serif;">{subtitle}</p>
        </td>
      </tr>
    </table>
  </td></tr>

  {body_rows}

  <!-- FOOTER -->
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:22px 28px;text-align:center;">
    <div style="font-size:10px;color:#6ee7b7;letter-spacing:2px;text-transform:uppercase;
        margin-bottom:8px;">Wagga Wagga Country Club - Automated Reminder</div>
    <div style="font-size:11px;color:rgba(255,255,255,0.35);">
        Generated {now_str} AEST - do not reply to this email.</div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


def build_meter_reading_html(now_sydney):
    """Build the end-of-month water meter reading reminder email (GK style)."""
    today       = now_sydney.date()
    month_label = today.strftime('%B %Y')
    if today.month == 12:
        deadline = date(today.year + 1, 1, 14)
    else:
        deadline = date(today.year, today.month + 1, 14)
    deadline_str = _fmt_d(deadline, '%-d %B %Y')

    # Live lake level for permitted pump rate
    lake_data = _load_lake_data()
    if lake_data and lake_data['latest'].get('lake_ahd'):
        ahd = float(lake_data['latest']['lake_ahd'])
        _, _, rate, _, _, _, _ = _lake_level_info(ahd)
        pump_rate_line = (f'The current permitted pumping rate is <strong>{rate}</strong> '
                          f'based on the current lake level of <strong>{ahd:.3f}m AHD</strong>.')
    else:
        pump_rate_line = 'Please refer to your licence conditions for the current permitted pumping rate.'

    body_rows = _gk_alert_banner('URGENT - METER READING MUST BE TAKEN TODAY', '#c0392b')
    body_rows += f"""<tr><td style="background:white;padding:20px 24px 16px;">
  <p style="margin:0 0 12px 0;font-family:Arial,sans-serif;font-size:14px;color:#111827;line-height:1.6;">
    Today is the last day of the month. The WaterNSW water meter is due to be read today.
    Please ensure the meter is photographed, the reading and pumping times are recorded in the
    logbook, and the reading is submitted in iWAS no later than <strong>{deadline_str}</strong>.
  </p>
  <p style="margin:0;font-family:Arial,sans-serif;font-size:14px;color:#111827;line-height:1.6;">
    {pump_rate_line}
  </p>
</td></tr>"""

    body_rows += f"""
  {sec_header('1', 'Logbook - What to Record at the Meter')}
  <tr><td style="background:white;padding:20px 24px 8px;border-radius:0 0 10px 10px;">
    {_gk_kv_table([
        ('Date',                    _fmt_d(today, '%-d %B %Y')),
        ('Meter reading',           'Total cumulative volume shown on meter display'),
        ('Volume taken this period','Calculate: current reading minus last reading'),
        ('Start &amp; end time',    'Record pumping start and end times for the period'),
        ('Meter serial number',     '210323056'),
        ('WAL number',              '40AL413687'),
        ('Approval number',         '40CA413688'),
    ])}
  </td></tr>

  {sec_header('2', 'Licence Details')}
  <tr><td style="background:white;padding:20px 24px 8px;border-radius:0 0 10px 10px;">
    {_gk_kv_table([
        ('Licence holder',       'Wagga Wagga Country Club Ltd (ACN 001 045 156)'),
        ('Water Access Licence', '40AL413687 (WAL 33232)'),
        ('Work Approval',        '40CA413688'),
        ('Meter serial number',  '210323056'),
        ('Water source',         'Murrumbidgee Central (Burrinjuck to Gogeldrie)'),
        ('Water sharing plan',   'Murrumbidgee Unregulated River Water Sources 2012'),
        ('Annual volume limit',  '193 ML per water year (1 Jul - 30 Jun)'),
        ('Approval expiry',      '22 June 2027'),
    ])}
  </td></tr>

  {sec_header('3', 'Current Pump Rate Limits')}
  <tr><td style="background:white;padding:20px 24px 8px;border-radius:0 0 10px 10px;">
    {_gk_data_table(
        ['Lake Level (AHD)', 'Maximum Pump Rate'],
        [
            ('Below 189.65m AHD',           'CEASE TO PUMP - do not extract'),
            ('189.65m to 189.85m AHD',      '0.50 ML/day'),
            ('189.85m to 190.05m AHD',      '0.75 ML/day'),
            ('190.05m to 190.25m AHD',      '1.00 ML/day'),
            ('Above 190.25m AHD (to full)', '1.50 ML/day'),
        ]
    )}
  </td></tr>

  {sec_header('4', 'Submit Your Reading to WaterNSW')}
  <tr><td style="background:white;padding:20px 24px 20px;border-radius:0 0 10px 10px;">
    {_gk_kv_table([
        ('Portal',              '<a href="https://iwas.waternsw.com.au" style="color:#1a4a2e;font-weight:bold;">iWAS - iwas.waternsw.com.au</a>'),
        ('Submission deadline', f'{deadline_str} (14th of next month)'),
        ('WaterNSW helpline',   '1300 662 077 (Mon-Fri 8am-5pm)'),
        ('Email',               'Customer.Helpdesk@waternsw.com.au'),
    ])}
  </td></tr>"""

    subject = f'URGENT - Water Meter Reading Required - {month_label}'
    html    = _gk_meter_wrap(
        'Water Meter Reading Required',
        f'Last day of {month_label} - Action required today',
        body_rows,
        now_sydney,
    )
    return html, subject


def build_meter_submission_html(now_sydney):
    """Build the 13th-of-month WaterNSW submission reminder email (GK style)."""
    today            = now_sydney.date()
    prev_month_end   = today.replace(day=1) - timedelta(days=1)
    prev_month_label = prev_month_end.strftime('%B %Y')
    deadline_str     = _fmt_d(today.replace(day=14), '%-d %B %Y')

    # Live lake level for permitted pump rate
    lake_data = _load_lake_data()
    if lake_data and lake_data['latest'].get('lake_ahd'):
        ahd = float(lake_data['latest']['lake_ahd'])
        _, _, rate, _, _, _, _ = _lake_level_info(ahd)
        pump_rate_line = (f'The current permitted pumping rate is <strong>{rate}</strong> '
                          f'based on the current lake level of <strong>{ahd:.3f}m AHD</strong>.')
    else:
        pump_rate_line = 'Please refer to your licence conditions for the current permitted pumping rate.'

    body_rows  = _gk_alert_banner(
        f'REMINDER - {prev_month_label.upper()} READING MUST BE SUBMITTED BY TOMORROW {deadline_str.upper()}',
        '#e67e22'
    )
    body_rows += f"""<tr><td style="background:white;padding:20px 24px 16px;">
  <p style="margin:0 0 12px 0;font-family:Arial,sans-serif;font-size:14px;color:#111827;line-height:1.6;">
    The monthly water meter reading for <strong>{prev_month_label}</strong> is due to be submitted
    to WaterNSW via iWAS by tomorrow, <strong>{deadline_str}</strong>. Please log in to iWAS and
    confirm the reading has been submitted. If the reading has not yet been submitted, please do
    so as soon as possible to avoid a compliance breach.
  </p>
  <p style="margin:0;font-family:Arial,sans-serif;font-size:14px;color:#111827;line-height:1.6;">
    {pump_rate_line}
  </p>
</td></tr>"""

    body_rows += f"""
  {sec_header('1', 'Details to Enter in iWAS')}
  <tr><td style="background:white;padding:20px 24px 8px;border-radius:0 0 10px 10px;">
    {_gk_kv_table([
        ('Water Access Licence', '40AL413687'),
        ('Work Approval number', '40CA413688'),
        ('Meter serial number',  '210323056'),
        ('Water type',           'Unregulated river'),
        ('Reading period',       prev_month_label),
        ('Volume taken',         'Total ML taken during the month (from logbook / meter reading)'),
        ('Declaration',          'Confirm accuracy and submit'),
    ])}
  </td></tr>

  {sec_header('2', 'Telemetry Status Reminder')}
  <tr><td style="background:white;padding:20px 24px 8px;border-radius:0 0 10px 10px;">
    {_gk_kv_table([
        ('S91i fault report', 'If telemetry is still not repaired, check your S91i extension is current'),
        ('Extension form',    '<a href="https://www.waternsw.com.au/customer-services/metering/s91i-extension" style="color:#1a4a2e;">waternsw.com.au - S91i Extension Form</a>'),
        ('Repair requirement','Meters must be repaired within 21 days of fault report (or extension granted)'),
        ('Reporting breach',  'Email: water.enquiries@dpi.nsw.gov.au or call 1800 353 104'),
    ])}
  </td></tr>

  {sec_header('3', 'Need Help?')}
  <tr><td style="background:white;padding:20px 24px 20px;border-radius:0 0 10px 10px;">
    {_gk_kv_table([
        ('WaterNSW helpline', '1300 662 077 (Mon-Fri 8am-5pm)'),
        ('Email',             'Customer.Helpdesk@waternsw.com.au'),
        ('iWAS portal',       '<a href="https://iwas.waternsw.com.au" style="color:#1a4a2e;">iwas.waternsw.com.au</a>'),
        ('Recording guide',   '<a href="https://www.waternsw.com.au/customer-services/metering/recording-and-reporting" style="color:#1a4a2e;">waternsw.com.au - Recording and Reporting</a>'),
    ])}
  </td></tr>"""

    subject = f'URGENT REMINDER - WaterNSW Meter Reading Due Tomorrow - {prev_month_label}'
    html    = _gk_meter_wrap(
        'WaterNSW Submission Due Tomorrow',
        f'Monthly meter reading for {prev_month_label}',
        body_rows,
        now_sydney,
    )
    return html, subject


# ─────────────────────────────────────────────────────────────────────────────
# EMAIL SENDING
# ─────────────────────────────────────────────────────────────────────────────

def send_email(subject, html_body, recipients):
    """Send HTML email via SendGrid."""
    if not SENDGRID_API_KEY:
        log.warning('No SendGrid API key - skipping email send.')
        return
    if not recipients or recipients == ['']:
        log.warning('No email recipients configured.')
        return
    from lake_utils import html_to_text
    message = Mail(
        from_email=Email(EMAIL_FROM, 'WWCC Weather'),
        subject=subject,
        plain_text_content=html_to_text(html_body),
        html_content=html_body,
    )
    for addr in recipients:
        addr = addr.strip()
        if addr:
            message.add_to(To(addr))
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        log.info(f'Email sent: {response.status_code} to {recipients}')
    except Exception as e:
        log.error(f'Email send error: {e}')


# ─────────────────────────────────────────────────────────────────────────────
# BACKFILL - populate missing historical dates in the CSV
# ─────────────────────────────────────────────────────────────────────────────

def backfill_history(from_date, to_date, force=False):
    """
    Backfill missing dates in daily_log.csv between from_date and to_date (inclusive).

    For each missing date:
      1. Fetch Davis WeatherLink v2 historic data (exact station readings).
      2. Fill any gaps (temp, ET, rain) from Open-Meteo archive API.
      3. Calculate GDD, disease models, soil balance, spray hours - identical
         logic to the nightly main() run.
      4. Append the row to the in-memory store.

    After all dates are processed the CSV is rewritten in date order with
    duplicates removed (last/most-complete row per date wins).
    """
    # ── Load existing CSV into memory ─────────────────────────────────────
    existing = {}          # date_str -> row_dict (last row per date wins)
    if CSV_PATH.exists():
        with open(CSV_PATH, newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                d_str = row.get('date', '').strip()
                if d_str:
                    existing[d_str] = row   # later rows overwrite earlier (deduplication)

    # ── Build list of missing dates ───────────────────────────────────────
    all_dates = []
    cur = from_date
    while cur <= to_date:
        all_dates.append(cur)
        cur += timedelta(days=1)
    missing = sorted(d for d in all_dates if force or d.isoformat() not in existing)

    if not missing:
        log.info('Backfill: no missing dates found - CSV is already complete.')
        return

    log.info(f'Backfill: {len(missing)} dates to process ({missing[0]} → {missing[-1]})')

    # ── Pre-fetch UV for entire date range in one Open-Meteo call ─────────
    # Much faster than one call per date - avoids rate-limit timeouts.
    uv_by_date = {}  # date_str -> uv_index_max float or None
    try:
        url = 'https://historical-forecast-api.open-meteo.com/v1/forecast'
        params = {
            'latitude':   CLUB_LAT,
            'longitude':  CLUB_LON,
            'start_date': missing[0].strftime('%Y-%m-%d'),
            'end_date':   missing[-1].strftime('%Y-%m-%d'),
            'daily':      'uv_index_max',
            'timezone':   'Australia/Sydney',
        }
        resp = requests.get(url, params=params, timeout=60)
        resp.raise_for_status()
        om_uv = resp.json()
        dates_list = om_uv.get('daily', {}).get('time', [])
        uv_list    = om_uv.get('daily', {}).get('uv_index_max', [])
        uv_by_date = {d: v for d, v in zip(dates_list, uv_list)}
        log.info(f'  Open-Meteo UV pre-fetch: {len(uv_by_date)} dates loaded')
    except Exception as e:
        log.warning(f'  Open-Meteo UV pre-fetch failed: {e} - UV will be None for this run')

    # ── Process each missing date in chronological order ──────────────────
    for i, target_date in enumerate(missing, start=1):
        log.info(f'[{i}/{len(missing)}] {target_date}')

        # 1. Davis v2 historic fetch
        davis_records = fetch_davis_historic(target_date)
        log.info(f'  Davis records: {len(davis_records)}')

        # 3. Process Davis data
        if davis_records:
            d = process_davis_records(davis_records)
        else:
            log.warning(f'  No Davis data - using Open-Meteo fallback for {target_date}')
            d = {k: None for k in [
                'temp_max', 'temp_min', 'temp_mean', 'rh_mean',
                'wind_max_kmh', 'wind_mean_kmh', 'rain_mm', 'et_mm',
                'pressure_mean', 'delta_t_mean', 'wet_hours',
                'night_wet_hours', 'night_min', 'consec_rh90',
                'spray_go', 'spray_caution', 'spray_nogo', 'rain_day',
            ]}
            d.update({'rain_mm': 0.0, 'et_mm': 0.0, 'wet_hours': 0,
                      'night_wet_hours': 0, 'consec_rh90': 0,
                      'spray_go': 0, 'spray_caution': 0, 'spray_nogo': 0})

        # 4. Fill gaps with Open-Meteo only when Davis data is missing/incomplete
        uv_max = d.get('uv_max')  # prefer Davis UV sensor; falls back to Open-Meteo below
        needs_gap_fill = (not davis_records or
                          d.get('temp_max') is None or
                          d.get('temp_min') is None)
        om_data = fetch_openmeteo_archive(target_date) if needs_gap_fill else None
        if om_data and om_data.get('daily'):
            daily_om = om_data['daily']
            if d['temp_max'] is None and daily_om.get('temperature_2m_max'):
                d['temp_max'] = daily_om['temperature_2m_max'][0]
            if d['temp_min'] is None and daily_om.get('temperature_2m_min'):
                d['temp_min'] = daily_om['temperature_2m_min'][0]
            if not d['rain_mm'] and daily_om.get('precipitation_sum'):
                d['rain_mm'] = daily_om['precipitation_sum'][0] or 0.0
            if not d['et_mm'] and daily_om.get('et0_fao_evapotranspiration'):
                d['et_mm'] = daily_om['et0_fao_evapotranspiration'][0] or 0.0
            if uv_max is None:
                uv_max = daily_om.get('uv_index_max', [None])[0]
        # UV fallback: use pre-fetched batch lookup (no extra HTTP call per date)
        if uv_max is None:
            uv_max = uv_by_date.get(target_date.isoformat())

        # 5. Rolling calculations from in-memory history
        #    Use the 7 dates immediately before target_date that are already stored.
        preceding = sorted(k for k in existing if k < target_date.isoformat())
        history_7_keys = preceding[-7:]
        history_5_keys = preceding[-5:]
        history_7 = [existing[k] for k in history_7_keys]
        history_5 = [existing[k] for k in history_5_keys]

        et_7   = sum(safe_float(r.get('et_mm'),  0) for r in history_7) + (d['et_mm']  or 0)
        rain_7 = sum(safe_float(r.get('rain_mm'), 0) for r in history_7) + (d['rain_mm'] or 0)
        balance_7 = round(et_7 - rain_7, 1)

        gdd_bent_today = round(gdd(d['temp_max'], d['temp_min'], 10), 1)
        gdd_kik_today  = round(gdd(d['temp_max'], d['temp_min'], 15), 1)
        gdd_bent_7d = round(
            sum(safe_float(r.get('gdd_bent'), 0) for r in history_7) + gdd_bent_today, 1)
        gdd_kik_7d  = round(
            sum(safe_float(r.get('gdd_kik'),  0) for r in history_7) + gdd_kik_today,  1)

        rain_days_6 = sum(
            1 for r in history_7[-6:] if r.get('rain_day') in ('True', True, '1'))
        if d['rain_day']:
            rain_days_6 += 1

        mean_temps_5 = [safe_float(r.get('temp_mean')) for r in history_5 if r.get('temp_mean')]
        mean_rh_5    = [safe_float(r.get('rh_mean'))   for r in history_5 if r.get('rh_mean')]
        if d['temp_mean']: mean_temps_5.append(d['temp_mean'])
        if d['rh_mean']:   mean_rh_5.append(d['rh_mean'])
        sk_mean_t  = sum(mean_temps_5) / len(mean_temps_5) if mean_temps_5 else None
        sk_mean_rh = sum(mean_rh_5)    / len(mean_rh_5)    if mean_rh_5    else None

        # 6. Disease models
        sk_pct             = smith_kerns(sk_mean_rh, sk_mean_t)
        ds_risk            = dollar_spot_risk(d['wet_hours'] or 0, d['temp_mean'], sk_pct)
        fus_score, fus_lvl = fusarium_risk(
            d['wet_hours'] or 0, d['consec_rh90'] or 0, rain_days_6, d['temp_mean'])
        bp_risk            = brown_patch_risk(
            d['night_wet_hours'] or 0, d['night_min'], d['temp_max'])
        pyt_risk           = pythium_risk(
            d['night_wet_hours'] or 0, d['night_min'], d['temp_max'])

        disease_alert = any(r in ('HIGH', 'SEVERE')
                            for r in [ds_risk, fus_lvl, bp_risk, pyt_risk])
        frost_flag    = (d['night_min'] is not None and d['night_min'] < 2)

        # 7. Build row (no fog/lightning - cannot backfill forecast data)
        row = {
            'date':                target_date.isoformat(),
            'temp_max':            d['temp_max'],
            'temp_min':            d['temp_min'],
            'temp_max_time':       d.get('temp_max_time'),
            'temp_min_time':       d.get('temp_min_time'),
            'temp_mean':           d['temp_mean'],
            'rh_mean':             d['rh_mean'],
            'wind_max_kmh':        d['wind_max_kmh'],
            'wind_mean_kmh':       d['wind_mean_kmh'],
            'wind_dir_deg':        d.get('wind_dir_deg'),
            'wind_run_km':         d.get('wind_run_km'),
            'rain_mm':             d['rain_mm'],
            'et_mm':               d['et_mm'],
            'rain_rate_max_mmhr':  d.get('rain_rate_max_mmhr'),
            'pressure_mean_hpa':   d['pressure_mean'],
            'solar_rad_avg':       d.get('solar_rad_avg'),
            'solar_rad_hi':        d.get('solar_rad_hi'),
            'solar_energy_ly':     d.get('solar_energy_ly'),
            'dew_point_c':         d.get('dew_point_c'),
            'wet_bulb_c':          d.get('wet_bulb_c'),
            'heat_index_c':        d.get('heat_index_c'),
            'wind_chill_c':        d.get('wind_chill_c'),
            'thsw_index_c':        d.get('thsw_index_c'),
            'delta_t_mean':        d['delta_t_mean'],
            'uv_max':              uv_max,
            'uv_index_avg':        d.get('uv_index_avg'),
            'uv_dose':             d.get('uv_dose'),
            'emc':                 d.get('emc'),
            'air_density_kgm3':    d.get('air_density_kgm3'),
            'night_cloud_cover':   d.get('night_cloud_cover'),
            'iss_reception':       d.get('iss_reception'),
            'gdd_bent':            gdd_bent_today,
            'gdd_kik':             gdd_kik_today,
            'gdd_bent_7d':         gdd_bent_7d,
            'gdd_kik_7d':          gdd_kik_7d,
            'leaf_wet_hours':      d['wet_hours'] or 0,
            'dollar_spot_pct':     sk_pct,
            'dollar_spot_risk':    ds_risk,
            'fusarium_score':      fus_score,
            'fusarium_risk':       fus_lvl,
            'brown_patch_risk':    bp_risk,
            'pythium_risk':        pyt_risk,
            'soil_balance_7d':     balance_7,
            'soil_zone':           soil_zone(balance_7),
            'spray_go_hours':      d['spray_go']      or 0,
            'spray_caution_hours': d['spray_caution'] or 0,
            'spray_nogo_hours':    d['spray_nogo']    or 0,
            'rain_day':            d['rain_day'],
            'frost_flag':          frost_flag,
            'disease_alert':       disease_alert,
            'fog_forecast':        False,
            'lightning_forecast':  False,
        }

        # Add FarmBot tank data from history file if available
        fb_day = load_farmbot_history_day(target_date.isoformat())
        row['tank_pct']      = fb_day.get('evening_pct')
        row['tank_volume_l'] = round((fb_day.get('evening_pct') or 0) / 100 * 250000) if fb_day.get('evening_pct') is not None else None
        row['tank_used_l']   = fb_day.get('used_l')
        row['tank_refill']   = fb_day.get('refill', False)

        existing[target_date.isoformat()] = row
        log.info(
            f'  ✓ max={d["temp_max"]}°C min={d["temp_min"]}°C '
            f'rain={d["rain_mm"]}mm GDD_bent={gdd_bent_today}')

        # Rate-limit: pause between Davis API calls
        if i < len(missing):
            time.sleep(1.5)

    # ── Rewrite CSV: sorted by date, duplicates removed ───────────────────
    log.info('Rewriting CSV in date order...')
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    sorted_rows = [existing[k] for k in sorted(existing.keys())]
    with open(CSV_PATH, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(sorted_rows)
    log.info(f'CSV rewritten: {len(sorted_rows)} rows total.')


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Yesterday in Sydney time
    now_sydney = datetime.now(tz=TZ)
    yesterday  = (now_sydney - timedelta(days=1)).date()
    log.info(f'Running daily report for {yesterday} (Sydney time)')

    # Guard against duplicate runs (two cron entries cover AEST and AEDT -
    # on DST transition days both fire within an hour of each other).
    existing = read_csv_history(3)
    if any(r.get('date') == yesterday.isoformat() for r in existing):
        log.info(f'Report for {yesterday} already exists in CSV - skipping duplicate run.')
        return

    # ── 1. Fetch data ──────────────────────────────────────────────────────
    log.info('Fetching Davis v2 historic data...')
    davis_records = fetch_davis_historic(yesterday)
    log.info(f'  {len(davis_records)} records returned')

    log.info('Fetching Open-Meteo archive...')
    om_data = fetch_openmeteo_archive(yesterday)

    # ── 2. Process Davis data ──────────────────────────────────────────────
    if davis_records:
        d = process_davis_records(davis_records)
    else:
        log.warning('No Davis records - using Open-Meteo fallback for basic stats')
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

    # UV: prefer Davis sensor data; Open-Meteo is fallback
    uv_max = d.get('uv_max')

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
    # UV: the Davis archive endpoint carries no UV fields, so max/avg/dose all
    # come from Open-Meteo's historical-forecast API (hourly integration for
    # avg and erythemal dose in MEDs)
    _uv_detail = fetch_openmeteo_uv(yesterday, want_detail=True)
    if uv_max is None:
        uv_max = _uv_detail.get('max')
    if d.get('uv_index_avg') is None and _uv_detail.get('avg') is not None:
        d['uv_index_avg'] = _uv_detail['avg']
    if d.get('uv_dose') is None and _uv_detail.get('dose_med') is not None:
        d['uv_dose'] = _uv_detail['dose_med']

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
    mean_temps_5 = [v for r in history_5 if (v := safe_float(r.get('temp_mean'))) is not None]
    mean_rh_5    = [v for r in history_5 if (v := safe_float(r.get('rh_mean'))) is not None]
    if d['temp_mean'] is not None: mean_temps_5.append(d['temp_mean'])
    if d['rh_mean']   is not None: mean_rh_5.append(d['rh_mean'])
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
    frost_flag    = (d['night_min'] is not None and d['night_min'] < 2)

    # ── 4b. Fetch today's forecast for fog/lightning warnings ──────────────
    today = (now_sydney).date()
    log.info('Fetching Open-Meteo forecast for fog/lightning detection...')
    fog_forecast, lightning_forecast = fetch_openmeteo_forecast(today)
    if fog_forecast:
        log.info('Fog forecast detected for this morning.')
    if lightning_forecast:
        log.info('Thunderstorm forecast detected for today.')

    log.info('Fetching 5-day forecast...')
    forecast_days = fetch_5day_forecast(today)
    log.info(f'  {len(forecast_days)} day(s) of forecast loaded')

    log.info('Fetching hourly forecast...')
    hourly_forecast = fetch_hourly_forecast(today)
    log.info(f'  {len(hourly_forecast)} hour(s) of hourly forecast loaded')

    # ── 5. Build CSV row ───────────────────────────────────────────────────
    row = {
        'date':              yesterday.isoformat(),
        'temp_max':          d['temp_max'],
        'temp_min':          d['temp_min'],
        'temp_max_time':     d.get('temp_max_time'),
        'temp_min_time':     d.get('temp_min_time'),
        'temp_mean':         d['temp_mean'],
        'rh_mean':           d['rh_mean'],
        'wind_max_kmh':      d['wind_max_kmh'],
        'wind_mean_kmh':     d['wind_mean_kmh'],
        'wind_dir_deg':      d.get('wind_dir_deg'),
        'wind_run_km':       d.get('wind_run_km'),
        'rain_mm':           d['rain_mm'],
        'et_mm':             d['et_mm'],
        'rain_rate_max_mmhr': d.get('rain_rate_max_mmhr'),
        'pressure_mean_hpa': d['pressure_mean'],
        'solar_rad_avg':     d.get('solar_rad_avg'),
        'solar_rad_hi':      d.get('solar_rad_hi'),
        'solar_energy_ly':   d.get('solar_energy_ly'),
        'dew_point_c':       d.get('dew_point_c'),
        'wet_bulb_c':        d.get('wet_bulb_c'),
        'heat_index_c':      d.get('heat_index_c'),
        'wind_chill_c':      d.get('wind_chill_c'),
        'thsw_index_c':      d.get('thsw_index_c'),
        'delta_t_mean':      d['delta_t_mean'],
        'uv_max':            uv_max,
        'uv_index_avg':      d.get('uv_index_avg'),
        'uv_dose':           d.get('uv_dose'),
        'emc':               d.get('emc'),
        'air_density_kgm3':  d.get('air_density_kgm3'),
        'night_cloud_cover': d.get('night_cloud_cover'),
        'iss_reception':     d.get('iss_reception'),
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
        'rain_day':           d['rain_day'],
        'frost_flag':         frost_flag,
        'disease_alert':      disease_alert,
        'fog_forecast':       fog_forecast,
        'lightning_forecast': lightning_forecast,
    }

    # ── 5b. Add FarmBot tank data for yesterday ────────────────────────────
    fb_day = load_farmbot_history_day(yesterday.isoformat())
    row['tank_pct']      = fb_day.get('evening_pct')   # end-of-day level
    row['tank_volume_l'] = round((fb_day.get('evening_pct') or 0) / 100 * 250000) if fb_day.get('evening_pct') is not None else None
    row['tank_used_l']   = fb_day.get('used_l')
    row['tank_refill']   = fb_day.get('refill', False)

    # ── 6. Save CSV row ────────────────────────────────────────────────────
    log.info('Appending row to daily_log.csv...')
    append_csv_row(row)

    # ── 7. Save HTML report to archive ────────────────────────────────────
    report_dir = REPORTS_ROOT / str(yesterday.year) / f'{yesterday.month:02d}'
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f'{yesterday.isoformat()}.html'
    history_for_report = read_csv_history(310)  # enough for full Sep-Jun season
    daily_html = build_daily_html(row, yesterday, history_for_report, forecast_days=forecast_days, hourly_forecast=hourly_forecast)
    report_path.write_text(daily_html, encoding='utf-8')
    log.info(f'Report saved: {report_path}')

    # ── 8. Send daily email to greenkeeper ────────────────────────────────
    subject = f'WWCC Morning Briefing - {_fmt_d(yesterday, "%-d %B %Y")}'
    log.info(f'Sending daily email: {subject}')
    send_email(subject, daily_html, EMAIL_RECIPIENTS_GK_ONLY)

    # ── 9. Weekly summary (Monday only) ───────────────────────────────────
    if now_sydney.weekday() == 0:  # Monday
        log.info('Monday detected - sending weekly summary...')
        week_history = read_csv_history(7)
        weekly_html  = build_weekly_html(week_history, yesterday)
        week_subject = f'WWCC Weekly Weather Summary - {_fmt_d(yesterday, "%-d %B %Y")}'
        send_email(week_subject, weekly_html, EMAIL_RECIPIENTS_ALL)

        # Save weekly report
        weekly_path = report_dir / f'{yesterday.isoformat()}-weekly.html'
        weekly_path.write_text(weekly_html, encoding='utf-8')

    # ── 10. Monthly summary (1st of month only) ───────────────────────────
    if yesterday.day == 1:
        log.info('1st of month - sending monthly summary...')
        month_history = read_csv_history(31)
        month_name    = (yesterday - timedelta(days=1)).strftime('%B %Y')
        monthly_html  = build_monthly_html(month_history, month_name)
        month_subject = f'WWCC Monthly Weather Summary - {month_name}'
        send_email(month_subject, monthly_html, EMAIL_RECIPIENTS_ALL)
        monthly_path  = report_dir / f'{yesterday.isoformat()}-monthly.html'
        monthly_path.write_text(monthly_html, encoding='utf-8')

    # ── 11. Annual summary (1st January only) ─────────────────────────────
    if yesterday.month == 1 and yesterday.day == 1:
        log.info('1st January - sending annual summary...')
        prev_year     = yesterday.year - 1
        year_history  = read_csv_history(366)
        year_label    = f'Full Year {prev_year}'
        yearly_html   = build_yearly_html(year_history, year_label)
        year_subject  = f'WWCC Annual Weather Summary - {prev_year}'
        send_email(year_subject, yearly_html, EMAIL_RECIPIENTS_ALL)
        yearly_path   = report_dir / f'{prev_year}-annual.html'
        yearly_path.write_text(yearly_html, encoding='utf-8')

    # ── 12. Water meter reading reminder (last day of month) ──────────────
    today_date = now_sydney.date()
    _, last_day_of_month = calendar.monthrange(today_date.year, today_date.month)
    if today_date.day == last_day_of_month:
        log.info('Last day of month - sending water meter reading reminder...')
        meter_html, meter_subject = build_meter_reading_html(now_sydney)
        send_email(meter_subject, meter_html, EMAIL_RECIPIENTS_ALL)

    # ── 13. WaterNSW submission reminder (13th of month) ──────────────────
    if today_date.day == 13:
        log.info('13th of month - sending WaterNSW submission reminder...')
        sub_html, sub_subject = build_meter_submission_html(now_sydney)
        send_email(sub_subject, sub_html, EMAIL_RECIPIENTS_ALL)

    log.info('Done.')


if __name__ == '__main__':
    import sys
    args = sys.argv[1:]

    if '--backfill' in args:
        # Usage (local/CLI only - not exposed in workflow):
        #   python daily_report.py --backfill
        #   python daily_report.py --backfill --from 2025-09-01 --to 2026-06-28 --force
        now_sydney = datetime.now(tz=TZ)
        from_str = None
        if '--from' in args:
            idx = args.index('--from')
            if idx + 1 < len(args):
                from_str = args[idx + 1]
        to_str = None
        if '--to' in args:
            idx = args.index('--to')
            if idx + 1 < len(args):
                to_str = args[idx + 1]
        if from_str:
            from_date = date.fromisoformat(from_str)
        else:
            y = now_sydney.year
            from_date = date(y if now_sydney.month >= 9 else y - 1, 9, 1)
        to_date = date.fromisoformat(to_str) if to_str else (now_sydney - timedelta(days=1)).date()
        force = '--force' in args
        log.info(f'--backfill: processing {from_date} → {to_date}' + (' (force)' if force else ''))
        backfill_history(from_date, to_date, force=force)
        log.info('Backfill complete.')

    elif '--diagnose' in args:
        # Usage (local/CLI only): python daily_report.py --diagnose
        now_sydney = datetime.now(tz=TZ)
        target = (now_sydney - timedelta(days=1)).date()
        log.info(f'--diagnose: fetching Davis v2 records for {target}')
        recs = fetch_davis_historic(target)
        log.info(f'  Total records returned: {len(recs)}')
        all_keys = sorted({k for r in recs for k in r.keys()})
        log.info(f'  All field names across all records ({len(all_keys)} total):')
        for k in all_keys:
            sample = next((r[k] for r in recs if r.get(k) is not None), None)
            log.info(f'    {k}: {sample}')

    elif '--resend' in args:
        # Re-send yesterday's daily email from existing CSV - no re-fetch or CSV changes.
        now_sydney = datetime.now(tz=TZ)
        yesterday  = (now_sydney - timedelta(days=1)).date()
        rows = read_csv_history(3)
        row  = next((r for r in reversed(rows) if r.get('date') == yesterday.isoformat()), None)
        if row is None:
            log.warning(f'No CSV row found for {yesterday} - run normally first.')
            sys.exit(1)
        log.info(f'--resend: re-sending daily email for {yesterday}.')
        history         = read_csv_history(310)
        forecast_days   = fetch_5day_forecast(now_sydney.date())
        hourly_forecast = fetch_hourly_forecast(now_sydney.date())
        daily_html = build_daily_html(row, yesterday, history,
                                      forecast_days=forecast_days,
                                      hourly_forecast=hourly_forecast)
        subject = f'WWCC Morning Briefing - {_fmt_d(yesterday, "%-d %B %Y")}'
        send_email(subject, daily_html, EMAIL_RECIPIENTS_GK_ONLY)
        log.info('Done.')

    elif '--test' in args:
        # Send test email(s) to andrew@bidgeepumps.com.au with [TEST] subject prefix.
        # Usage:
        #   python daily_report.py --test daily
        #   python daily_report.py --test weekly
        #   python daily_report.py --test monthly
        #   python daily_report.py --test annual
        #   python daily_report.py --test meter_reading
        #   python daily_report.py --test meter_submission
        #   python daily_report.py --test all
        now_sydney  = datetime.now(tz=TZ)
        yesterday   = (now_sydney - timedelta(days=1)).date()
        test_addr   = ['andrew@bidgeepumps.com.au']
        idx         = args.index('--test')
        which       = args[idx + 1] if idx + 1 < len(args) and not args[idx + 1].startswith('--') else 'all'
        valid = ('daily', 'weekly', 'monthly', 'annual', 'meter_reading', 'meter_submission', 'all')
        if which not in valid:
            log.error(f'Unknown test report "{which}". Choose from: {", ".join(valid)}')
            sys.exit(1)

        def _test_send(html, subj):
            send_email(f'[TEST] {subj}', html, test_addr)
            log.info(f'  Sent [TEST] "{subj}" to {test_addr[0]}')

        if which in ('daily', 'all'):
            rows = read_csv_history(3)
            row  = next((r for r in reversed(rows) if r.get('date') == yesterday.isoformat()), None)
            if row is None:
                log.warning('No CSV row for yesterday - skipping daily test.')
            else:
                history         = read_csv_history(310)
                forecast_days   = fetch_5day_forecast(now_sydney.date())
                hourly_forecast = fetch_hourly_forecast(now_sydney.date())
                html = build_daily_html(row, yesterday, history,
                                        forecast_days=forecast_days,
                                        hourly_forecast=hourly_forecast)
                _test_send(html, f'WWCC Morning Briefing - {_fmt_d(yesterday, "%-d %B %Y")}')

        if which in ('weekly', 'all'):
            html = build_weekly_html(read_csv_history(7), yesterday)
            _test_send(html, f'WWCC Weekly Weather Summary - {_fmt_d(yesterday, "%-d %B %Y")}')

        if which in ('monthly', 'all'):
            month_name = (yesterday - timedelta(days=1)).strftime('%B %Y')
            html = build_monthly_html(read_csv_history(31), month_name)
            _test_send(html, f'WWCC Monthly Weather Summary - {month_name}')

        if which in ('annual', 'all'):
            year_label = f'Full Year {yesterday.year - 1}'
            html = build_yearly_html(read_csv_history(366), year_label)
            _test_send(html, f'WWCC Annual Weather Summary - {yesterday.year - 1}')

        if which in ('meter_reading', 'all'):
            html, subj = build_meter_reading_html(now_sydney)
            _test_send(html, subj)

        if which in ('meter_submission', 'all'):
            html, subj = build_meter_submission_html(now_sydney)
            _test_send(html, subj)

        log.info('Done.')

    else:
        main()
