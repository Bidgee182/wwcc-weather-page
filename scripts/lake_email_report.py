#!/usr/bin/env python3
"""
Lake Albert Email Reports
=========================
Sends daily / weekly / monthly / yearly lake condition reports
to council and club stakeholders via SendGrid.

Schedule (Sydney time):
  Daily   - every morning
  Weekly  - Monday only
  Monthly - 1st of each month
  Yearly  - 1 January (calendar year)

Usage:
    python scripts/lake_email_report.py                    # auto (date-based)
    python scripts/lake_email_report.py --report=weekly    # specific type
    python scripts/lake_email_report.py --force-all        # all four types
    python scripts/lake_email_report.py --dry-run          # preview HTML, no send

Secrets required:
    SENDGRID_API_KEY   - SendGrid API key
    EMAIL_FROM         - sender address
    EMAIL_LAKE_TO      - comma-separated To addresses
    EMAIL_LAKE_CC      - comma-separated CC addresses (optional)
"""

import json, os, sys, logging, argparse
from datetime import datetime, timedelta, date as ddate
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

SYDNEY_TZ   = ZoneInfo('Australia/Sydney')
DATA_DIR    = Path(__file__).parent.parent / 'data'
# Refined at runtime to the surface area at the CURRENT lake level using the
# linear area model in lake_utils (full-supply value is only the fallback), so
# depth-change -> ML conversions match the board dashboard and board email.
LAKE_SURFACE_M2 = 1_202_046      # m² fallback (full supply)
LAKE_VOL_BOTTOM = 188.1          # physical lake bed AHD - matches lake-albert.html
LAKE_FULL_AHD_V = 191.551        # full supply level AHD - matches lake-albert.html
LAKE_FULL_ML    = 4148.3         # full capacity in ML (pre-computed)


def _ahd_to_ml(ahd):
    """Volume in ML via the shared linear area model (lake_utils)."""
    if ahd is None:
        return None
    try:
        import lake_utils as _lu
        return _lu.vol_between_ml(LAKE_VOL_BOTTOM, ahd)
    except Exception:
        return max(0.0, LAKE_SURFACE_M2 * (ahd - LAKE_VOL_BOTTOM) / 1000)


def _use_area_at_current_level(latest):
    """Point LAKE_SURFACE_M2 at the area for the current lake level so all
    depth-change -> ML conversions agree with the board dashboard."""
    global LAKE_SURFACE_M2
    try:
        import lake_utils as _lu
        ahd = (latest or {}).get('lake_ahd')
        if ahd is not None:
            LAKE_SURFACE_M2 = _lu.lake_area_m2(float(ahd))
    except Exception:
        pass


_WHITE_LOGO_URL = 'https://bidgee182.github.io/wwcc-weather-page/assets/images/logo-white.png'


def _white_logo_html():
    return (f'<img src="{_WHITE_LOGO_URL}" width="194" height="44" alt="Wagga Wagga Country Club"'
            f' style="display:block;border:0;">')


SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
EMAIL_FROM       = os.environ.get('EMAIL_FROM', '')
EMAIL_LAKE_TO    = os.environ.get('EMAIL_LAKE_TO', '')
EMAIL_LAKE_CC    = os.environ.get('EMAIL_LAKE_CC', '')
EMAIL_LAKE_BCC   = os.environ.get('EMAIL_LAKE_BCC', '')

# ── Data loading ──────────────────────────────────────────────────────────────

def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except Exception as e:
        log.warning(f'Could not load {path}: {e}')
        return None

def load_data():
    return {
        'latest':   load_json(DATA_DIR / 'farmbot_lake_latest.json'),
        'readings': load_json(DATA_DIR / 'farmbot_lake_readings.json') or [],
        'history':  load_json(DATA_DIR / 'davis_weather_history.json') or [],
        'pumping':  load_json(DATA_DIR / 'pumping_usage.json') or [],
    }

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_ahd(ahd):
    if ahd is None: return '-'
    return f'{ahd:.3f}&nbsp;m&nbsp;AHD'

def fmt_depth(ahd):
    if ahd is None: return '-'
    return f'{ahd - LAKE_VOL_BOTTOM:.2f}&nbsp;m'

def activity_status(ahd):
    if ahd is None: return ('Unknown', '#888888', '#ffffff')
    depth = ahd - LAKE_VOL_BOTTOM
    if depth >= 1.8: return ('All Vessels Permitted',                        '#1e8449', '#ffffff')
    if depth >= 1.2: return ('No Water Skiing - Sailing & Leisure OK', '#e67e22', '#ffffff')
    if depth >= 0.6: return ('Sailboats & Paddle Craft Only',                '#d4ac0d', '#1b2631')
    return                   ('No Boating',                                   '#c0392b', '#ffffff')

def readings_in_range(readings, start, end):
    """Filter lake readings to Sydney date range [start, end] inclusive.
    Returns list with added '_dt' and '_date' keys."""
    out = []
    for r in readings:
        try:
            dt = datetime.fromisoformat(
                r['date'].replace('Z', '+00:00')
            ).astimezone(SYDNEY_TZ)
            d = dt.date()
            if start <= d <= end:
                out.append({**r, '_dt': dt, '_date': d})
        except Exception:
            pass
    return out

def history_in_range(history, start, end):
    out = []
    for r in history:
        try:
            d = ddate.fromisoformat(r['date'])
            if start <= d <= end:
                out.append(r)
        except Exception:
            pass
    return out

def pumping_in_range(pumping, start, end):
    """Filter pumping records to [start, end] inclusive."""
    from datetime import date as ddate
    out = []
    for p in pumping:
        try:
            d = ddate.fromisoformat(p['date'])
            if start <= d <= end:
                out.append({**p, '_date': d})
        except Exception:
            pass
    return sorted(out, key=lambda x: x['_date'])

def _fmt_d(dt, fmt):
    """Cross-platform strftime: %-d → no-pad day, %-I → no-pad 12-hour."""
    s = fmt.replace('%-d', str(dt.day))
    if '%-I' in s and hasattr(dt, 'hour'):
        s = s.replace('%-I', str(dt.hour % 12 or 12))
    return dt.strftime(s)

def fmt_date(d):
    """Format a date with no leading zero on day, cross-platform."""
    return f"{d.day} {d.strftime('%b %Y')}"

def fmt_month(d):
    return d.strftime('%B %Y')

# ── HTML building blocks ──────────────────────────────────────────────────────

# Colour palette
HDR_BG   = '#1a5276'   # deep navy - header bar
SEC_BG   = '#2471a3'   # mid blue - section headers
ROW_A    = '#d6eaf8'   # light blue - alternating row
ROW_B    = '#eaf4fb'   # very light blue - alternating row
BODY_BG  = '#f0f4f8'   # page background
BORDER   = '#a9cce3'   # table border colour

def _pill(label, bg, fg='#ffffff'):
    return (f'<span style="display:inline-block;background-color:{bg};color:{fg};'
            f'padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;'
            f'font-family:Arial,sans-serif;">{label}</span>')

def _header(title, subtitle=''):
    sub = (f'<p style="margin:4px 0 0 0;font-size:12px;color:#a9cce3;'
           f'font-family:Arial,sans-serif;font-weight:normal;">{subtitle}</p>') if subtitle else ''
    return f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    <td bgcolor="{HDR_BG}" style="background-color:{HDR_BG};padding:20px 24px 18px 24px;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td valign="middle" class="mob-logo-cell" style="padding-right:16px;white-space:nowrap;width:1%;">
            {_white_logo_html()}
          </td>
          <td valign="middle" class="mob-text-cell">
            <p style="margin:0 0 6px 0;font-size:10px;color:#a9cce3;letter-spacing:2px;
                text-transform:uppercase;font-family:Arial,sans-serif;font-weight:normal;">
              Wagga Wagga Country Club &nbsp;&bull;&nbsp; Lake Albert
            </p>
            <h1 style="margin:0;font-size:24px;color:#ffffff;font-weight:bold;
                font-family:Arial,sans-serif;line-height:1.2;">{title}</h1>
            {sub}
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>"""

def _section(text):
    return f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:14px;">
  <tr>
    <td bgcolor="{SEC_BG}" style="background-color:{SEC_BG};padding:7px 20px;">
      <p style="margin:0;font-size:13px;color:#ffffff;font-weight:bold;
          letter-spacing:0.5px;font-family:Arial,sans-serif;">{text}</p>
    </td>
  </tr>
</table>"""

def _kv_table(rows):
    """rows = [(label, value_html), ...]"""
    cells = ''
    for i, (k, v) in enumerate(rows):
        bg = ROW_A if i % 2 == 0 else ROW_B
        cells += f"""
  <tr>
    <td bgcolor="{bg}" style="background-color:{bg};padding:9px 20px;width:210px;
        font-family:Arial,sans-serif;font-size:13px;color:{HDR_BG};font-weight:bold;
        border-bottom:1px solid {BORDER};">{k}</td>
    <td bgcolor="{bg}" style="background-color:{bg};padding:9px 20px;
        font-family:Arial,sans-serif;font-size:13px;color:#1b2631;
        border-bottom:1px solid {BORDER};">{v}</td>
  </tr>"""
    return f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
{cells}
</table>"""

def _data_table(headers, rows):
    hcells = ''.join(
        f'<td bgcolor="{HDR_BG}" style="background-color:{HDR_BG};padding:8px 10px;'
        f'font-family:Arial,sans-serif;font-size:12px;color:#ffffff;font-weight:bold;'
        f'border-right:1px solid {BORDER};border-bottom:1px solid {BORDER};">{h}</td>'
        for h in headers
    )
    drows = ''
    for i, row in enumerate(rows):
        bg = ROW_A if i % 2 == 0 else ROW_B
        drows += '<tr>' + ''.join(
            f'<td bgcolor="{bg}" style="background-color:{bg};padding:7px 10px;'
            f'font-family:Arial,sans-serif;font-size:12px;color:#1b2631;'
            f'border-bottom:1px solid {BORDER};border-right:1px solid {BORDER};">{c}</td>'
            for c in row
        ) + '</tr>'
    return f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;border:1px solid {BORDER};">
  <tr>{hcells}</tr>
  {drows}
</table>"""

def _footer(now_syd):
    return f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:20px;">
  <tr>
    <td bgcolor="{HDR_BG}" style="background-color:{HDR_BG};padding:12px 20px;">
      <p style="margin:0;font-size:10px;color:#a9cce3;text-align:center;
          font-family:Arial,sans-serif;">
        Generated {_fmt_d(now_syd, '%-d %b %Y %H:%M')} AEST &nbsp;&bull;&nbsp;
        Lake Albert, Wagga Wagga NSW &nbsp;&bull;&nbsp;
        Auto-generated - do not reply to this email.
      </p>
    </td>
  </tr>
</table>"""

def _wrap(body):
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style type="text/css">
  @media only screen and (max-width:620px) {{
    table[width="600"] {{ width:100% !important; max-width:100% !important; }}
    .mob-logo-cell {{ display:block !important; width:100% !important; text-align:center !important; padding-right:0 !important; padding-bottom:14px !important; }}
    .mob-logo-cell img {{ height:44px !important; width:auto !important; max-width:100% !important; }}
    .mob-text-cell {{ display:block !important; width:100% !important; }}
  }}
  </style>
<body style="margin:0;padding:16px;background-color:{BODY_BG};">
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       bgcolor="{BODY_BG}" style="background-color:{BODY_BG};">
  <tr><td>
{body}
  </td></tr>
</table>
</body>
</html>"""

# ── Report: Daily ─────────────────────────────────────────────────────────────

def build_daily(data, now_syd):
    today     = now_syd.date()
    yesterday = today - timedelta(days=1)

    latest = data['latest'] or {}
    ahd    = latest.get('lake_ahd')
    try:
        lake_dt  = datetime.fromisoformat(
            latest['lake_date'].replace('Z', '+00:00')
        ).astimezone(SYDNEY_TZ)
        lake_ts  = _fmt_d(lake_dt, '%-d %b %Y %H:%M')
    except Exception:
        lake_ts = '-'

    act_lbl, act_bg, act_fg = activity_status(ahd)

    yday_wx  = next((r for r in reversed(data['history'])
                     if r['date'] == str(yesterday)), None)

    body  = _header('Lake Albert Daily Report', _fmt_d(now_syd, '%-d %B %Y'))
    # Volume
    vol_ml  = _ahd_to_ml(ahd)
    vol_pct = min(100.0, vol_ml / LAKE_FULL_ML * 100) if vol_ml is not None else None

    # Daily level change - compare yesterday vs day before
    pumping_data = data.get('pumping', [])
    pump_today = next((p for p in pumping_data if p['date'] == str(yesterday)), None)
    pump_ml_today = float(pump_today['ml']) if pump_today else 0.0

    # Get level readings for yesterday and day before
    prev_day   = yesterday - timedelta(days=1)
    rdgs_today = [r for r in data['readings'] if r.get('_date') == yesterday]
    rdgs_prev  = [r for r in data['readings'] if r.get('_date') == prev_day]

    # Readings may not have _date populated yet - fall back to parsing
    if not rdgs_today:
        rdgs_today = [r for r in readings_in_range(data['readings'], yesterday, yesterday)]
    if not rdgs_prev:
        rdgs_prev  = [r for r in readings_in_range(data['readings'], prev_day, prev_day)]

    today_ahd_r = float(rdgs_today[-1]['ahd']) if rdgs_today and rdgs_today[-1].get('ahd') else None
    prev_ahd_r  = float(rdgs_prev[-1]['ahd'])  if rdgs_prev  and rdgs_prev[-1].get('ahd')  else None

    daily_chg_ahd = (today_ahd_r - prev_ahd_r) if (today_ahd_r and prev_ahd_r) else None
    daily_chg_ml  = (daily_chg_ahd * LAKE_SURFACE_M2 / 1000) if daily_chg_ahd is not None else None

    if daily_chg_ahd is not None:
        chg_sign = '+' if daily_chg_ahd >= 0 else ''
        chg_str  = f'{chg_sign}{daily_chg_ahd:.3f}&nbsp;m ({chg_sign}{daily_chg_ml:.1f}&nbsp;ML)'
    else:
        chg_str = '-'

    # Evaporation
    if daily_chg_ml is not None:
        evap_ml = max(0.0, -daily_chg_ml - pump_ml_today)
        evap_str = f'{evap_ml:.1f}&nbsp;ML'
    else:
        evap_ml  = None
        evap_str = '-'

    # Yesterday snapshot - volume/change/evap based on yesterday's last reading
    yday_vol_ml  = _ahd_to_ml(today_ahd_r)
    yday_vol_pct = (min(100.0, yday_vol_ml / LAKE_FULL_ML * 100)
                    if yday_vol_ml is not None else None)

    body += _section('LAKE LEVEL')
    body += _kv_table([
        (f'Current Depth &nbsp;<span style="font-weight:normal;font-size:11px;'
         f'color:#5d6d7e;">as at {lake_ts}</span>',
         f'<strong style="font-size:16px;color:#1a5276;">{fmt_depth(ahd)}</strong>'),
        ('Activity Status', _pill(act_lbl, act_bg, act_fg)),
    ])

    body += _section("YESTERDAY'S SNAPSHOT")
    body += _kv_table([
        ('Volume',             (f'{yday_vol_ml:.0f}&nbsp;ML'
                                f' ({yday_vol_pct:.1f}% of {LAKE_FULL_ML:.0f}&nbsp;ML)')
                               if yday_vol_ml is not None else '-'),
        ('Daily Level Change', chg_str),
        ('Evaporation (est.)', evap_str),
        ('Lake Level (AHD)',   fmt_ahd(today_ahd_r)),
    ])

    if yday_wx:
        body += _section(f"YESTERDAY'S WEATHER - {fmt_date(yesterday).upper()}")
        wx_rows = []
        if yday_wx.get('tMax') is not None:
            tmax_str = f"{yday_wx['tMax']:.1f}&nbsp;&deg;C"
            if yday_wx.get('tMaxTime'): tmax_str += f"&nbsp;<span style='color:#5d6d7e;font-size:11px;'>({yday_wx['tMaxTime']})</span>"
            wx_rows.append(('Max Temperature', tmax_str))
        if yday_wx.get('tMin') is not None:
            tmin_str = f"{yday_wx['tMin']:.1f}&nbsp;&deg;C"
            if yday_wx.get('tMinTime'): tmin_str += f"&nbsp;<span style='color:#5d6d7e;font-size:11px;'>({yday_wx['tMinTime']})</span>"
            wx_rows.append(('Min Temperature', tmin_str))
        if yday_wx.get('rain')      is not None: wx_rows.append(('Rainfall',        f"{yday_wx['rain']:.1f}&nbsp;mm"))
        if yday_wx.get('humidity')  is not None: wx_rows.append(('Humidity',        f"{yday_wx['humidity']:.0f}%"))
        if yday_wx.get('windMean')  is not None: wx_rows.append(('Mean Wind Speed', f"{yday_wx['windMean']:.1f}&nbsp;km/h"))
        if yday_wx.get('windMax')   is not None: wx_rows.append(('Max Wind Speed',  f"{yday_wx['windMax']:.1f}&nbsp;km/h"))
        if yday_wx.get('windDir')   is not None: wx_rows.append(('Wind Direction',  f"{yday_wx['windDir']}"))
        if yday_wx.get('pressAvg')  is not None: wx_rows.append(('Avg Pressure',    f"{yday_wx['pressAvg']:.1f}&nbsp;hPa"))
        if wx_rows:
            body += _kv_table(wx_rows)

    body += _footer(now_syd)
    subject = f"Lake Albert Daily Report - {fmt_date(now_syd.date())}"
    return _wrap(body), subject


# ── Report: Weekly ────────────────────────────────────────────────────────────

def build_weekly(data, now_syd):
    today      = now_syd.date()
    week_end   = today - timedelta(days=1)
    week_start = week_end - timedelta(days=6)

    readings = readings_in_range(data['readings'], week_start, week_end)
    wx_week  = history_in_range(data['history'], week_start, week_end)

    # One row per day - last reading of day
    day_range = [week_start + timedelta(days=i) for i in range(7)]
    daily_levels = []
    for d in day_range:
        day_rdgs = [r for r in readings if r.get('_date') == d]
        level_ahd = float(day_rdgs[-1]['ahd']) if (day_rdgs and day_rdgs[-1].get('ahd')) else None
        daily_levels.append((d, level_ahd))

    # Get reading from day before week for first-day change calc
    day_before_wk = week_start - timedelta(days=1)
    rdgs_before_wk = readings_in_range(data['readings'], day_before_wk, day_before_wk)
    prev_level_wk = (float(rdgs_before_wk[-1]['ahd'])
                     if (rdgs_before_wk and rdgs_before_wk[-1].get('ahd')) else None)

    latest = data['latest'] or {}
    ahd    = latest.get('lake_ahd')
    act_lbl, act_bg, act_fg = activity_status(ahd)

    wx_tmax = max((r['tMax']    for r in wx_week if r.get('tMax')    is not None), default=None)
    wx_tmin = min((r['tMin']    for r in wx_week if r.get('tMin')    is not None), default=None)
    wx_rain = sum(r.get('rain', 0) or 0 for r in wx_week)
    wx_wmax = max((r['windMax'] for r in wx_week if r.get('windMax') is not None), default=None)

    period = f"{week_start.day} {week_start.strftime('%b')} &ndash; {fmt_date(week_end)}"
    body  = _header('Lake Albert Weekly Report', period)

    # Volume and weekly change
    vol_ml  = _ahd_to_ml(ahd)
    vol_pct = min(100.0, vol_ml / LAKE_FULL_ML * 100) if vol_ml is not None else None

    week_ahd_vals = [r['ahd'] for r in readings if r.get('ahd') is not None]
    first_ahd_w = week_ahd_vals[0]  if week_ahd_vals else None
    last_ahd_w  = week_ahd_vals[-1] if week_ahd_vals else None
    week_chg    = (last_ahd_w - first_ahd_w) if (first_ahd_w and last_ahd_w) else None
    week_chg_ml = (week_chg * LAKE_SURFACE_M2 / 1000) if week_chg is not None else None

    week_pump_data = pumping_in_range(data.get('pumping', []), week_start, week_end)
    total_pump_ml  = sum(float(p.get('ml', 0)) for p in week_pump_data)

    if week_chg_ml is not None:
        chg_sign = '+' if week_chg >= 0 else ''
        ml_sign  = '+' if week_chg_ml >= 0 else ''
        chg_str  = f'{chg_sign}{week_chg:.3f}&nbsp;m ({ml_sign}{week_chg_ml:.1f}&nbsp;ML)'
        evap_ml  = max(0.0, -week_chg_ml - total_pump_ml)
        evap_str = f'{evap_ml:.1f}&nbsp;ML'
    else:
        chg_str  = '-'
        evap_str = '-'

    body += _section('CURRENT LAKE STATUS')
    body += _kv_table([
        ('Current Depth',       fmt_depth(ahd)),
        ('Activity Status',     _pill(act_lbl, act_bg, act_fg)),
        ('Volume',              f'{vol_ml:.0f}&nbsp;ML ({vol_pct:.1f}% of {LAKE_FULL_ML:.0f}&nbsp;ML)' if vol_ml is not None else '-'),
        ('Week Level Change',   chg_str),
        ('Evaporation (est.)',  evap_str),
        ('Current Level (AHD)', fmt_ahd(ahd)),
    ])

    body += _section('DAILY LAKE LEVELS - PAST 7 DAYS')
    level_rows = []
    prev_l = prev_level_wk
    for d, level_ahd in daily_levels:
        chg_ahd = (level_ahd - prev_l) if (level_ahd is not None and prev_l is not None) else None
        chg_ml  = (chg_ahd * LAKE_SURFACE_M2 / 1000) if chg_ahd is not None else None
        pump_d  = next((p for p in week_pump_data if p['_date'] == d), None)
        pump_ml = float(pump_d.get('ml', 0)) if pump_d else 0.0
        evap_d  = max(0.0, -chg_ml - pump_ml) if chg_ml is not None else None
        sign    = '+' if (chg_ahd or 0) >= 0 else ''
        chg_s   = f'{sign}{chg_ahd:.3f}m' if chg_ahd is not None else '-'
        evap_s  = f'{evap_d:.1f}' if evap_d is not None else '-'
        a_lbl, a_bg, _ = activity_status(level_ahd)
        level_rows.append((
            _fmt_d(d, '%a %-d %b'),
            fmt_ahd(level_ahd),
            fmt_depth(level_ahd),
            chg_s,
            evap_s,
            _pill(a_lbl, a_bg),
        ))
        prev_l = level_ahd
    body += _data_table(['Date', 'Level (AHD)', 'Depth', 'Change', 'Evap (ML)', 'Status'], level_rows)

    body += _section('WEEKLY WEATHER SUMMARY')
    wx_rows = []
    if wx_tmax is not None: wx_rows.append(('Peak Max Temperature',  f'{wx_tmax:.1f}&nbsp;&deg;C'))
    if wx_tmin is not None: wx_rows.append(('Lowest Min Temperature', f'{wx_tmin:.1f}&nbsp;&deg;C'))
    wx_rows.append(('Total Rainfall', f'{wx_rain:.1f}&nbsp;mm'))
    if wx_wmax is not None: wx_rows.append(('Peak Wind Speed', f'{wx_wmax:.1f}&nbsp;km/h'))
    body += _kv_table(wx_rows)

    body += _footer(now_syd)
    subject = (f"Lake Albert Weekly Report - "
               f"{week_start.day} {week_start.strftime('%b')}-{fmt_date(week_end)}")
    return _wrap(body), subject


# ── Report: Monthly ───────────────────────────────────────────────────────────

def build_monthly(data, now_syd):
    today          = now_syd.date()
    first_this     = today.replace(day=1)
    month_end      = first_this - timedelta(days=1)   # last day of prev month
    month_start    = month_end.replace(day=1)

    readings = readings_in_range(data['readings'], month_start, month_end)
    wx_month = history_in_range(data['history'], month_start, month_end)

    ahd_vals = [r['ahd'] for r in readings if r.get('ahd') is not None]
    ahd_max  = max(ahd_vals, default=None)
    ahd_min  = min(ahd_vals, default=None)
    ahd_avg  = round(sum(ahd_vals) / len(ahd_vals), 3) if ahd_vals else None

    latest = data['latest'] or {}
    ahd    = latest.get('lake_ahd')
    act_lbl, act_bg, act_fg = activity_status(ahd)

    wx_tmax      = max((r['tMax']    for r in wx_month if r.get('tMax')    is not None), default=None)
    wx_tmin      = min((r['tMin']    for r in wx_month if r.get('tMin')    is not None), default=None)
    wx_rain      = sum(r.get('rain', 0) or 0 for r in wx_month)
    wx_wmax      = max((r['windMax'] for r in wx_month if r.get('windMax') is not None), default=None)
    wx_rain_days = sum(1 for r in wx_month if (r.get('rain') or 0) > 0)
    wx_hot_days  = sum(1 for r in wx_month if (r.get('tMax') or 0) >= 35)

    month_label = fmt_month(month_end)

    body  = _header('Lake Albert Monthly Report', month_label)

    vol_ml  = _ahd_to_ml(ahd)
    vol_pct = min(100.0, vol_ml / LAKE_FULL_ML * 100) if vol_ml is not None else None

    first_ahd_m = ahd_vals[0]  if ahd_vals else None
    last_ahd_m  = ahd_vals[-1] if ahd_vals else None
    month_chg   = (last_ahd_m - first_ahd_m) if (first_ahd_m and last_ahd_m) else None
    month_chg_ml = (month_chg * LAKE_SURFACE_M2 / 1000) if month_chg is not None else None

    month_pump_data = pumping_in_range(data.get('pumping', []), month_start, month_end)
    total_pump_ml   = sum(float(p.get('ml', 0)) for p in month_pump_data)

    if month_chg_ml is not None:
        chg_sign = '+' if month_chg >= 0 else ''
        ml_sign  = '+' if month_chg_ml >= 0 else ''
        chg_str  = f'{chg_sign}{month_chg:.3f}&nbsp;m ({ml_sign}{month_chg_ml:.1f}&nbsp;ML)'
        evap_ml  = max(0.0, -month_chg_ml - total_pump_ml)
        evap_str = f'{evap_ml:.1f}&nbsp;ML'
    else:
        chg_str  = '-'
        evap_str = '-'

    body += _section('CURRENT LAKE STATUS')
    body += _kv_table([
        ('Current Depth',       fmt_depth(ahd)),
        ('Activity Status',     _pill(act_lbl, act_bg, act_fg)),
        ('Volume',              f'{vol_ml:.0f}&nbsp;ML ({vol_pct:.1f}% of {LAKE_FULL_ML:.0f}&nbsp;ML)' if vol_ml is not None else '-'),
        ('Month Level Change',  chg_str),
        ('Evaporation (est.)',  evap_str),
        ('Current Level (AHD)', fmt_ahd(ahd)),
    ])

    body += _section(f'LAKE LEVELS - {month_label.upper()}')
    body += _kv_table([
        ('Highest Level (AHD)', fmt_ahd(ahd_max)),
        ('Lowest Level (AHD)',  fmt_ahd(ahd_min)),
        ('Average Level (AHD)', fmt_ahd(ahd_avg)),
        ('Highest Depth',       fmt_depth(ahd_max)),
        ('Lowest Depth',        fmt_depth(ahd_min)),
        ('Readings Recorded',   str(len(ahd_vals)) if ahd_vals else '-'),
    ])

    # Per-day breakdown
    body += _section(f'DAILY BREAKDOWN - {month_label.upper()}')
    all_month_days = [month_start + timedelta(days=i)
                      for i in range((month_end - month_start).days + 1)]
    day_before_m    = month_start - timedelta(days=1)
    rdgs_before_m   = readings_in_range(data['readings'], day_before_m, day_before_m)
    prev_ahd_m      = (float(rdgs_before_m[-1]['ahd'])
                       if (rdgs_before_m and rdgs_before_m[-1].get('ahd')) else None)
    month_pump_data = pumping_in_range(data.get('pumping', []), month_start, month_end)
    daily_rows = []
    for d in all_month_days:
        day_rdgs = [r for r in readings if r.get('_date') == d]
        day_ahd  = float(day_rdgs[-1]['ahd']) if (day_rdgs and day_rdgs[-1].get('ahd')) else None
        chg_ahd  = (day_ahd - prev_ahd_m) if (day_ahd is not None and prev_ahd_m is not None) else None
        chg_ml   = (chg_ahd * LAKE_SURFACE_M2 / 1000) if chg_ahd is not None else None
        pump_d   = next((p for p in month_pump_data if p['_date'] == d), None)
        pump_ml  = float(pump_d.get('ml', 0)) if pump_d else 0.0
        evap_d   = max(0.0, -chg_ml - pump_ml) if chg_ml is not None else None
        sign     = '+' if (chg_ahd or 0) >= 0 else ''
        chg_s    = f'{sign}{chg_ahd:.3f}m' if chg_ahd is not None else '-'
        evap_s   = f'{evap_d:.1f}' if evap_d is not None else '-'
        daily_rows.append((
            _fmt_d(d, '%-d %b'),
            fmt_ahd(day_ahd),
            fmt_depth(day_ahd),
            chg_s,
            evap_s,
        ))
        prev_ahd_m = day_ahd
    body += _data_table(['Date', 'Level (AHD)', 'Depth', 'Change', 'Evap (ML)'], daily_rows)

    body += _section(f'WEATHER SUMMARY - {month_label.upper()}')
    wx_rows = []
    if wx_tmax is not None: wx_rows.append(('Peak Max Temperature',  f'{wx_tmax:.1f}&nbsp;&deg;C'))
    if wx_tmin is not None: wx_rows.append(('Lowest Min Temperature', f'{wx_tmin:.1f}&nbsp;&deg;C'))
    wx_rows.append(('Total Rainfall',  f'{wx_rain:.1f}&nbsp;mm'))
    wx_rows.append(('Rain Days',       str(wx_rain_days)))
    if wx_wmax is not None: wx_rows.append(('Peak Wind Speed', f'{wx_wmax:.1f}&nbsp;km/h'))
    if wx_hot_days > 0:     wx_rows.append(('Days 35&deg;C+',  str(wx_hot_days)))
    body += _kv_table(wx_rows)

    body += _footer(now_syd)
    subject = f"Lake Albert Monthly Report - {month_label}"
    return _wrap(body), subject


# ── Report: Yearly ────────────────────────────────────────────────────────────

def build_yearly(data, now_syd):
    today      = now_syd.date()
    # Calendar year: 1 Jan → 31 Dec
    # On 1 Jan, report covers the previous calendar year
    year_end   = today - timedelta(days=1)                # 31 Dec of last year
    year_start = year_end.replace(month=1, day=1)         # 1 Jan of last year

    readings = readings_in_range(data['readings'], year_start, year_end)
    wx_year  = history_in_range(data['history'], year_start, year_end)

    ahd_vals = [r['ahd'] for r in readings if r.get('ahd') is not None]
    ahd_max  = max(ahd_vals, default=None)
    ahd_min  = min(ahd_vals, default=None)
    ahd_avg  = round(sum(ahd_vals) / len(ahd_vals), 3) if ahd_vals else None

    # Dates of peak and low
    def find_date(target_ahd):
        r = next((x for x in readings if x.get('ahd') == target_ahd), None)
        return fmt_date(r['_date']) if r else ''

    peak_date = find_date(ahd_max) if ahd_max is not None else ''
    low_date  = find_date(ahd_min) if ahd_min is not None else ''

    latest = data['latest'] or {}
    ahd    = latest.get('lake_ahd')
    act_lbl, act_bg, act_fg = activity_status(ahd)

    wx_tmax       = max((r['tMax']    for r in wx_year if r.get('tMax')    is not None), default=None)
    wx_tmin       = min((r['tMin']    for r in wx_year if r.get('tMin')    is not None), default=None)
    wx_rain       = sum(r.get('rain', 0) or 0 for r in wx_year)
    wx_wmax       = max((r['windMax'] for r in wx_year if r.get('windMax') is not None), default=None)
    wx_rain_days  = sum(1 for r in wx_year if (r.get('rain') or 0) > 0)
    wx_hot_days   = sum(1 for r in wx_year if (r.get('tMax') or 0) >= 35)
    wx_frost_days = sum(1 for r in wx_year if (r.get('tMin') or 0) < 0)

    cy_label  = str(year_end.year)
    period    = f"{fmt_date(year_start)} &ndash; {fmt_date(year_end)}"

    body  = _header(f'Lake Albert Annual Report - {cy_label}', period)

    vol_ml  = _ahd_to_ml(ahd)
    vol_pct = min(100.0, vol_ml / LAKE_FULL_ML * 100) if vol_ml is not None else None

    first_ahd_y = ahd_vals[0]  if ahd_vals else None
    last_ahd_y  = ahd_vals[-1] if ahd_vals else None
    year_chg    = (last_ahd_y - first_ahd_y) if (first_ahd_y and last_ahd_y) else None
    year_chg_ml = (year_chg * LAKE_SURFACE_M2 / 1000) if year_chg is not None else None

    year_pump_data = pumping_in_range(data.get('pumping', []), year_start, year_end)
    total_pump_ml  = sum(float(p.get('ml', 0)) for p in year_pump_data)

    if year_chg_ml is not None:
        chg_sign = '+' if year_chg >= 0 else ''
        ml_sign  = '+' if year_chg_ml >= 0 else ''
        chg_str  = f'{chg_sign}{year_chg:.3f}&nbsp;m ({ml_sign}{year_chg_ml:.1f}&nbsp;ML)'
        evap_ml  = max(0.0, -year_chg_ml - total_pump_ml)
        evap_str = f'{evap_ml:.1f}&nbsp;ML'
    else:
        chg_str  = '-'
        evap_str = '-'

    body += _section('CURRENT LAKE STATUS')
    body += _kv_table([
        ('Current Depth',       fmt_depth(ahd)),
        ('Activity Status',     _pill(act_lbl, act_bg, act_fg)),
        ('Volume',              f'{vol_ml:.0f}&nbsp;ML ({vol_pct:.1f}% of {LAKE_FULL_ML:.0f}&nbsp;ML)' if vol_ml is not None else '-'),
        ('Year Level Change',   chg_str),
        ('Evaporation (est.)',  evap_str),
        ('Current Level (AHD)', fmt_ahd(ahd)),
    ])

    body += _section(f'LAKE LEVEL SUMMARY - {cy_label}')
    body += _kv_table([
        ('Highest Level (AHD)', f"{fmt_ahd(ahd_max)}{(' - ' + peak_date) if peak_date else ''}"),
        ('Lowest Level (AHD)',  f"{fmt_ahd(ahd_min)}{(' - ' + low_date)  if low_date  else ''}"),
        ('Average Level (AHD)', fmt_ahd(ahd_avg)),
        ('Highest Depth',       fmt_depth(ahd_max)),
        ('Lowest Depth',        fmt_depth(ahd_min)),
        ('Total Readings',      str(len(ahd_vals)) if ahd_vals else '-'),
    ])

    body += _section(f'WEATHER SUMMARY - {cy_label}')
    wx_rows = []
    if wx_tmax is not None:  wx_rows.append(('Peak Max Temperature',   f'{wx_tmax:.1f}&nbsp;&deg;C'))
    if wx_tmin is not None:  wx_rows.append(('Lowest Min Temperature', f'{wx_tmin:.1f}&nbsp;&deg;C'))
    wx_rows.append(('Annual Rainfall',  f'{wx_rain:.1f}&nbsp;mm'))
    wx_rows.append(('Rain Days',        f'{wx_rain_days} days'))
    if wx_wmax is not None:  wx_rows.append(('Peak Wind Speed',        f'{wx_wmax:.1f}&nbsp;km/h'))
    if wx_hot_days > 0:      wx_rows.append(('Days 35&deg;C+',         f'{wx_hot_days} days'))
    if wx_frost_days > 0:    wx_rows.append(('Frost Days (below 0&deg;C)', f'{wx_frost_days} days'))
    wx_rows.append(('Weather Records', f'{len(wx_year)} days'))
    body += _kv_table(wx_rows)

    # Per-month breakdown
    body += _section(f'MONTHLY BREAKDOWN - {cy_label}')
    monthly_rows = []
    for month in range(1, 13):
        m_start = ddate(year_end.year, month, 1)
        if month < 12:
            m_end = ddate(year_end.year, month + 1, 1) - timedelta(days=1)
        else:
            m_end = ddate(year_end.year, 12, 31)
        if m_start > year_end:
            break
        m_end = min(m_end, year_end)

        m_rdgs     = [r for r in readings if r.get('_date') and m_start <= r['_date'] <= m_end]
        m_ahd_vals = [float(r['ahd']) for r in m_rdgs if r.get('ahd') is not None]
        m_max      = max(m_ahd_vals, default=None)
        m_min      = min(m_ahd_vals, default=None)
        first_m    = m_ahd_vals[0]  if m_ahd_vals else None
        last_m     = m_ahd_vals[-1] if m_ahd_vals else None
        m_chg      = (last_m - first_m) if (first_m is not None and last_m is not None) else None
        m_chg_ml   = (m_chg * LAKE_SURFACE_M2 / 1000) if m_chg is not None else None
        m_pump     = pumping_in_range(data.get('pumping', []), m_start, m_end)
        m_pump_ml  = sum(float(p.get('ml', 0)) for p in m_pump)
        m_evap_ml  = max(0.0, -m_chg_ml - m_pump_ml) if m_chg_ml is not None else None
        m_wx       = [r for r in wx_year if ddate.fromisoformat(r['date']).month == month]
        m_rain     = sum(r.get('rain', 0) or 0 for r in m_wx)
        sign_m     = '+' if (m_chg or 0) >= 0 else ''
        chg_s      = f'{sign_m}{m_chg:.3f}m' if m_chg is not None else '-'
        evap_s     = f'{m_evap_ml:.1f}' if m_evap_ml is not None else '-'
        monthly_rows.append((
            m_start.strftime('%B'),
            fmt_depth(m_max) if m_max is not None else '-',
            fmt_depth(m_min) if m_min is not None else '-',
            chg_s,
            evap_s,
            f'{m_rain:.1f}',
        ))
    body += _data_table(
        ['Month', 'High Depth', 'Low Depth', 'Change', 'Evap (ML)', 'Rain (mm)'],
        monthly_rows
    )

    body += _footer(now_syd)
    subject = f"Lake Albert Annual Report - {cy_label}"
    return _wrap(body), subject


# ── Email sending ─────────────────────────────────────────────────────────────

def send_email(subject, html_content, test_mode=False):
    if not SENDGRID_API_KEY:
        log.error('SENDGRID_API_KEY not set - cannot send')
        return False
    if not EMAIL_FROM:
        log.error('EMAIL_FROM not set - cannot send')
        return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, To, Cc, Bcc, Email
    except ImportError:
        log.error('sendgrid package not installed (pip install sendgrid)')
        return False

    if test_mode:
        to_list, cc_list, bcc_list = ['andrew@bidgeepumps.com.au'], [], []
        subject = f'[TEST] {subject}'
        log.info(f'[TEST MODE] Sending only to {to_list[0]}')
    else:
        # Encrypted recipients file first, env secrets as fallback - checking
        # the env var alone here once blocked sends even with a valid file
        from lake_utils import recipients_tcb
        to_list, cc_list, bcc_list = recipients_tcb(
            'lake', EMAIL_LAKE_TO, EMAIL_LAKE_CC, EMAIL_LAKE_BCC)

    if not to_list:
        log.error('No To recipients - cannot send')
        return False

    everyone = to_list + cc_list + bcc_list
    from lake_utils import html_to_text
    mail = Mail(
        from_email=Email(EMAIL_FROM),
        subject=subject,
        plain_text_content=html_to_text(html_content),
        html_content=html_content,
    )
    mail.to = [To(e) for e in to_list]
    if cc_list:
        mail.cc = [Cc(e) for e in cc_list]
    if bcc_list:
        mail.bcc = [Bcc(e) for e in bcc_list]

    try:
        sg   = SendGridAPIClient(SENDGRID_API_KEY)
        resp = sg.send(mail)
        log.info(f'Sent "{subject}" - status {resp.status_code} - to: {", ".join(to_list)}')
        if cc_list:
            log.info(f'  CC: {", ".join(cc_list)}')
        if bcc_list:
            log.info(f'  BCC: {len(bcc_list)} address(es)')
        from lake_utils import log_email
        log_email('lake_report', subject, everyone, f'sent ({resp.status_code})')
        return True
    except Exception as e:
        log.error(f'SendGrid error sending "{subject}": {e}')
        try:
            from lake_utils import log_email
            log_email('lake_report', subject, everyone, f'failed: {e}')
        except Exception:
            pass
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

BUILDERS = {
    'daily':   build_daily,
    'weekly':  build_weekly,
    'monthly': build_monthly,
    'yearly':  build_yearly,
}

def main():
    parser = argparse.ArgumentParser(description='Lake Albert email reports')
    parser.add_argument('--report',    choices=list(BUILDERS), metavar='TYPE',
                        help='Send a specific report type: daily|weekly|monthly|yearly')
    parser.add_argument('--force-all', action='store_true',
                        help='Send all four report types regardless of date')
    parser.add_argument('--dry-run',   action='store_true',
                        help='Build HTML and save preview files, do not send email')
    parser.add_argument('--test',      action='store_true',
                        help='Send only to andrew@bidgeepumps.com.au with [TEST] subject prefix')
    args = parser.parse_args()

    now_syd = datetime.now(tz=SYDNEY_TZ)
    today   = now_syd.date()
    log.info(f'Sydney date/time: {now_syd.strftime("%Y-%m-%d %H:%M %Z")}')

    data = load_data()
    _use_area_at_current_level(data.get('latest'))

    # Determine which reports to run
    if args.force_all:
        to_send = list(BUILDERS)
    elif args.report:
        to_send = [args.report]
    else:
        to_send = ['daily']
        if today.weekday() == 0:                to_send.append('weekly')
        if today.day == 1:                      to_send.append('monthly')
        if today.month == 1 and today.day == 1: to_send.append('yearly')

    log.info(f'Reports scheduled: {to_send}')

    # ── Duplicate-send guard ──────────────────────────────────────────────────
    SENT_MARKER = DATA_DIR / 'lake_sent_today.json'
    today_str   = str(today)

    def _load_sent():
        try:
            d = json.loads(SENT_MARKER.read_text(encoding='utf-8'))
            if d.get('date') == today_str:
                return d.get('sent', [])
        except Exception:
            pass
        return []

    def _mark_sent(report_type, already_sent):
        updated = list(already_sent)
        if report_type not in updated:
            updated.append(report_type)
        try:
            SENT_MARKER.write_text(
                json.dumps({'date': today_str, 'sent': updated}, indent=2),
                encoding='utf-8'
            )
        except Exception as e:
            log.warning(f'Could not write sent marker: {e}')
        return updated

    already_sent = _load_sent()
    failed = []

    for report_type in to_send:
        if not args.dry_run and not args.force_all and report_type in already_sent:
            log.info(f'[skip] {report_type} already sent today - skipping')
            continue

        html, subject = BUILDERS[report_type](data, now_syd)

        if args.dry_run:
            preview_dir = DATA_DIR / 'reports'
            preview_dir.mkdir(exist_ok=True)
            out = preview_dir / f'lake_{report_type}_preview.html'
            out.write_text(html, encoding='utf-8')
            log.info(f'[dry-run] {report_type}: saved preview to {out}')
        else:
            ok = send_email(subject, html, test_mode=args.test)
            if ok and not args.test:
                already_sent = _mark_sent(report_type, already_sent)
            if not ok:
                failed.append(report_type)

    if failed:
        # Red run -> watchdog alerts; a silent success would hide a dead send
        log.error(f'Send FAILED for: {", ".join(failed)} - failing the run')
        sys.exit(1)

if __name__ == '__main__':
    main()
