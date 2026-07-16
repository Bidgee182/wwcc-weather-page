#!/usr/bin/env python3
"""
board_email.py - Weekly Board Weather and Water Update email.

Sends Monday 7 AM AEST. Shows lake level, licence level, extraction rate,
days to next level, 7-day weather table, rainfall totals, and tank status.

Environment variables:
    SENDGRID_API_KEY   - SendGrid API key
    EMAIL_FROM         - sender address
    EMAIL_BOARD_TO     - comma-separated To recipients
    EMAIL_BOARD_CC     - comma-separated CC recipients (optional)
    EMAIL_BOARD_BCC    - comma-separated BCC recipients (optional)
"""

import csv
import json
import logging
import os
import sys
import urllib.parse as _up
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ── paths ──────────────────────────────────────────────────────────────────────
_ROOT     = Path(__file__).parent.parent
_DATA_DIR = _ROOT / 'data'
sys.path.insert(0, str(Path(__file__).parent))
import lake_utils as lu

# ── logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
log = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────────
_LOGO_URL = 'https://bidgee182.github.io/wwcc-weather-page/assets/images/logo-white.png'
_HDR_BG   = '#1a5276'
_SEC_BG   = '#2471a3'
_BODY_BG  = '#ffffff'
_CARD_BG  = '#ffffff'
_BORDER   = '#a9cce3'
_ROW_A    = '#d6eaf8'
_ROW_B    = '#eaf4fb'

SYDNEY_TZ = ZoneInfo('Australia/Sydney')

SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
EMAIL_FROM       = os.environ.get('EMAIL_FROM', '')
EMAIL_BOARD_TO   = os.environ.get('EMAIL_BOARD_TO', '')
EMAIL_BOARD_CC   = os.environ.get('EMAIL_BOARD_CC', '')
EMAIL_BOARD_BCC  = os.environ.get('EMAIL_BOARD_BCC', '')

# ── dedup guard ────────────────────────────────────────────────────────────────
_SENT_FILE       = _DATA_DIR / 'board_sent_week.json'
_SENT_MONTH_FILE = _DATA_DIR / 'board_sent_month.json'
ZONE_HISTORY_CSV = _DATA_DIR / 'lake_zone_history.csv'


def _already_sent_this_week(now_syd):
    iso_week = now_syd.strftime('%G-W%V')
    try:
        data = json.loads(_SENT_FILE.read_text())
        return data.get('sent_week') == iso_week
    except Exception:
        return False


def _get_last_week_projection():
    """Return last week's stored projection data, or None if unavailable."""
    try:
        data = json.loads(_SENT_FILE.read_text())
        if 'cease_date' in data and 'cost' in data:
            return {
                'cease_date': data['cease_date'],
                'cost':       float(data['cost']),
            }
    except Exception:
        pass
    return None


def _mark_sent_this_week(now_syd, proj=None):
    iso_week = now_syd.strftime('%G-W%V')
    payload  = {'sent_week': iso_week}
    if proj:
        payload.update(proj)
    _SENT_FILE.write_text(json.dumps(payload))


def _already_sent_this_month(now_syd):
    iso_month = now_syd.strftime('%Y-%m')
    try:
        data = json.loads(_SENT_MONTH_FILE.read_text())
        return data.get('sent_month') == iso_month
    except Exception:
        return False


def _mark_sent_this_month(now_syd):
    _SENT_MONTH_FILE.write_text(json.dumps({'sent_month': now_syd.strftime('%Y-%m')}))


# ── HTML helpers ───────────────────────────────────────────────────────────────

def _wrap(body):
    """Outer page shell: grey body background, white content card."""
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
    .mob-stat {{ width:100% !important; display:block !important; border-top:none !important; }}
  }}
  </style>
</head>
<body style="margin:0;padding:20px 8px;background-color:{_BODY_BG};">
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       bgcolor="{_CARD_BG}" style="background-color:{_CARD_BG};">
  <tr><td>
{body}
  </td></tr>
</table>
</body>
</html>"""


def _header(subtitle):
    return f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    <td bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:20px 24px 18px 24px;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td valign="middle" class="mob-logo-cell" style="padding-right:16px;white-space:nowrap;width:1%;">
            <img src="{_LOGO_URL}" width="194" height="44" alt="Wagga Wagga Country Club" style="display:block;border:0;">
          </td>
          <td valign="middle" class="mob-text-cell">
            <p style="margin:0 0 6px 0;font-size:10px;color:#a9cce3;letter-spacing:2px;
                text-transform:uppercase;font-family:Arial,sans-serif;font-weight:normal;">
              Wagga Wagga Country Club &nbsp;&bull;&nbsp; Lake Albert
            </p>
            <h1 style="margin:0;font-size:22px;color:#ffffff;font-weight:bold;
                font-family:Arial,sans-serif;line-height:1.2;">Weekly Board Weather and Water Update</h1>
            <p style="margin:4px 0 0 0;font-size:12px;color:#a9cce3;
                font-family:Arial,sans-serif;font-weight:normal;">{subtitle}</p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>"""


def _card_open(title):
    """Open a section card: border wrapper + dark title bar + content cell."""
    return f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:20px;">
  <tr>
    <td style="border:1px solid #cbd5e1;padding:0;background-color:#ffffff;" bgcolor="#ffffff">
      <table width="100%" cellpadding="0" cellspacing="0" border="0"
             style="border-collapse:collapse;">
        <tr>
          <td bgcolor="{_SEC_BG}" style="background-color:{_SEC_BG};padding:8px 20px;">
            <p style="margin:0;font-size:13px;color:#ffffff;font-weight:bold;
                letter-spacing:0.5px;font-family:Arial,sans-serif;">{title}</p>
          </td>
        </tr>
        <tr>
          <td style="padding:0;" bgcolor="#ffffff">"""


def _card_close():
    """Close a section card."""
    return """
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>"""


def _stat_cell(width_pct, bg, label, value_html, sub, accent=None):
    """Single stat box - white card with coloured top accent, full border."""
    top_col = accent or _SEC_BG
    return (
        f'<td class="mob-stat" width="{width_pct}%" bgcolor="#ffffff" '
        f'style="background-color:#ffffff;padding:14px 18px;'
        f'border:1px solid {_BORDER};border-top:3px solid {top_col};vertical-align:top;">'
        f'<p style="margin:0 0 5px 0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;'
        f'color:#64748b;letter-spacing:0.7px;text-transform:uppercase;">{label}</p>'
        f'<p style="margin:0;font-family:Arial,sans-serif;font-size:20px;font-weight:700;'
        f'color:{_HDR_BG};">{value_html}</p>'
        f'<p style="margin:4px 0 0 0;font-family:Arial,sans-serif;font-size:11px;'
        f'color:#64748b;">{sub}</p>'
        f'</td>'
    )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _v(val, fmt='.1f', suffix=''):
    """Format an optional numeric value; returns '-' if missing or blank."""
    if val is None or str(val).strip() == '':
        return '-'
    try:
        return f'{float(val):{fmt}}{suffix}'
    except Exception:
        return '-'


def _deg_to_compass(deg):
    """Convert wind direction in degrees to 16-point compass label."""
    if not deg or str(deg).strip() == '':
        return ''
    dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE',
            'S','SSW','SW','WSW','W','WNW','NW','NNW']
    return dirs[round(float(deg) / 22.5) % 16]


# ── Charts ─────────────────────────────────────────────────────────────────────

def _seven_day_chart(readings):
    """QuickChart.io PNG - daily average AHD for past 7 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    by_day = defaultdict(list)
    for r in readings:
        try:
            ts = datetime.fromisoformat(r['date'].replace('Z', '+00:00'))
            if ts >= cutoff and r.get('ahd'):
                by_day[ts.date().isoformat()].append(float(r['ahd']))
        except Exception:
            pass

    days   = sorted(by_day.keys())
    vals   = [round(sum(by_day[d]) / len(by_day[d]), 3) for d in days]
    labels = [f'{datetime.fromisoformat(d).day} {datetime.fromisoformat(d).strftime("%b")}' for d in days]

    if not vals:
        return ''

    ymin = round(min(vals) - 0.05, 3)
    ymax = round(max(vals) + 0.05, 3)

    thresh_colors = {1: '#00762A', 2: '#8AC63F', 3: '#FFDD00', 4: '#F58E1E', 5: '#EB1E23'}
    datasets = [{
        'label': 'Lake Level', 'data': vals,
        'borderColor': '#1abc9c', 'backgroundColor': 'rgba(26,188,156,0.12)',
        'fill': True, 'lineTension': 0.3, 'pointRadius': 3, 'borderWidth': 2,
    }]
    for z in lu.get_config()['zone_thresholds']:
        t = z.get('min_ahd')
        if t and ymin - 0.05 <= t <= ymax + 0.05:
            datasets.append({
                'data': [t] * len(days),
                'borderColor': thresh_colors.get(z['zone'], '#888'),
                'borderWidth': 1, 'borderDash': [5, 4],
                'fill': False, 'pointRadius': 0, 'lineTension': 0,
            })

    config = {
        'type': 'line',
        'data': {'labels': labels, 'datasets': datasets},
        'options': {
            'legend': {'display': False},
            'scales': {
                'xAxes': [{'gridLines': {'color': '#1e3040'},
                           'ticks': {'fontColor': '#4a6070', 'maxRotation': 0}}],
                'yAxes': [{'gridLines': {'color': '#1e3040'},
                           'ticks': {'fontColor': '#4a6070', 'min': ymin, 'max': ymax}}],
            },
        },
    }
    cfg_json = json.dumps(config, separators=(',', ':'))
    url = f'https://quickchart.io/chart?bkg=%230d1b2a&w=560&h=200&c={_up.quote(cfg_json)}'
    return (f'<img src="{url}" width="100%" alt="Lake level - past 7 days"'
            f' style="display:block;border-radius:6px;max-width:100%;">')


def _tank_chart_html(readings):
    """QuickChart.io PNG - daily average tank fill % for past 7 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    by_day = defaultdict(list)
    for r in readings:
        try:
            ts = datetime.fromisoformat(r['date'].replace('Z', '+00:00'))
            if ts >= cutoff and r.get('pct') is not None:
                by_day[ts.date().isoformat()].append(float(r['pct']))
        except Exception:
            pass

    days   = sorted(by_day.keys())
    vals   = [round(sum(by_day[d]) / len(by_day[d]), 1) for d in days]
    labels = [f'{datetime.fromisoformat(d).day} {datetime.fromisoformat(d).strftime("%b")}' for d in days]

    if not vals:
        return ''

    ymin = max(0,   round(min(vals) - 5))
    ymax = min(100, round(max(vals) + 5))

    config = {
        'type': 'line',
        'data': {
            'labels': labels,
            'datasets': [
                {
                    'label': 'Tank Level', 'data': vals,
                    'borderColor': '#2980b9', 'backgroundColor': 'rgba(41,128,185,0.15)',
                    'fill': True, 'lineTension': 0.3, 'pointRadius': 3, 'borderWidth': 2,
                },
                {
                    'data': [20] * len(days),
                    'borderColor': '#e67e22', 'borderWidth': 1, 'borderDash': [5, 4],
                    'fill': False, 'pointRadius': 0, 'lineTension': 0,
                },
            ],
        },
        'options': {
            'legend': {'display': False},
            'scales': {
                'xAxes': [{'gridLines': {'color': '#1e3040'},
                           'ticks': {'fontColor': '#4a6070', 'maxRotation': 0}}],
                'yAxes': [{'gridLines': {'color': '#1e3040'},
                           'ticks': {'fontColor': '#4a6070', 'min': ymin, 'max': ymax,
                                     'callback': '|function(v){return v+"%"}'},
                           'scaleLabel': {'display': False}}],
            },
        },
    }
    cfg_str = json.dumps(config, separators=(',', ':'))
    cfg_str = cfg_str.replace('"callback":"|function(v){return v+\\"%\\"}"',
                               '"callback":function(v){return v+"%"}')
    url = f'https://quickchart.io/chart?bkg=%230d1b2a&w=560&h=180&c={_up.quote(cfg_str)}'
    return (f'<img src="{url}" width="100%" alt="Tank level - past 7 days"'
            f' style="display:block;border-radius:6px;max-width:100%;">')


# ── Email body ─────────────────────────────────────────────────────────────────

def _load_json(path):
    """Load a JSON file, returning None and logging a warning on any failure."""
    try:
        return json.loads(Path(path).read_text())
    except Exception as e:
        log.error(f'Failed to load {path}: {e}')
        return None


def build_html(now_syd):
    # ── Load data ──────────────────────────────────────────────────────────────
    lake_latest   = _load_json(_DATA_DIR / 'farmbot_lake_latest.json')
    readings      = _load_json(_DATA_DIR / 'farmbot_lake_readings.json')
    tank          = _load_json(_DATA_DIR / 'farmbot_latest.json')
    tank_readings = _load_json(_DATA_DIR / 'farmbot_readings.json')

    if any(v is None for v in (lake_latest, readings, tank, tank_readings)):
        log.error('One or more required data files could not be loaded - aborting email build')
        return None, None

    wx_rows = []
    try:
        with open(_DATA_DIR / 'daily_log.csv', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                if r.get('date'):
                    wx_rows.append(r)
        wx_rows.sort(key=lambda r: r['date'])
    except Exception as e:
        log.error(f'Failed to load daily_log.csv: {e}')
        return None, None

    # ── Lake calculations ──────────────────────────────────────────────────────
    ahd = lake_latest.get('lake_ahd')
    if ahd is None:
        log.error('No lake_ahd in farmbot_lake_latest.json')
        return None, None

    month    = now_syd.month
    level    = lu.current_zone_info(ahd)
    lv_num   = level['zone']
    lv_name  = level['name']
    lv_pump  = level['max_pump_ml_day']
    lv_bg    = level['color_bg']
    lv_txt   = level['color_text']

    nxt     = lu.next_zone_below(ahd)
    days, _ = lu.days_to_next_zone(ahd, month)

    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=7)
    recent_lake = sorted(
        [r for r in readings
         if datetime.fromisoformat(r['date'].replace('Z', '+00:00')) >= cutoff_dt
         and r.get('ahd')],
        key=lambda r: r['date']
    )
    week_change = (ahd - float(recent_lake[0]['ahd'])) if recent_lake else None

    cfg      = lu.get_config()
    pan_mm   = cfg['evaporation']['monthly_pan_mm_day'][str(month)]
    lake_mm  = pan_mm * cfg['evaporation']['pan_factor']
    evap_ml  = lu.evap_ml_day(ahd, month)
    mth_name = now_syd.strftime('%B')

    # ── Days display ───────────────────────────────────────────────────────────
    if days is None:
        days_display = '-'
        days_sub     = 'No lower level - cease to pump'
        days_col     = '#EB1E23'
    elif days == float('inf'):
        days_display = 'Rising'
        days_sub     = 'Net lake gain - level increasing'
        days_col     = '#00762A'
    else:
        days_int     = int(days)
        days_display = f'~{days_int:,}'
        next_name    = nxt['name'] if nxt else 'next level'
        days_sub     = f'days until {next_name}'
        days_col     = '#00762A' if days_int > 90 else ('#F58E1E' if days_int > 30 else '#EB1E23')

    if week_change is not None:
        arrow     = '&uarr;' if week_change > 0.001 else ('&darr;' if week_change < -0.001 else '&rarr;')
        trend_str = f'{arrow} {abs(week_change):.3f}&nbsp;m past 7&nbsp;days'
    else:
        trend_str = ''

    date_str   = f'{now_syd.day} {now_syd.strftime("%B %Y")}'
    chart_html = _seven_day_chart(readings)

    # ── Weather calculations ───────────────────────────────────────────────────
    today_str   = now_syd.date().isoformat()
    month_start = now_syd.date().replace(day=1).isoformat()
    year_start  = f'{now_syd.year}-01-01'
    cutoff_7    = (now_syd.date() - timedelta(days=7)).isoformat()

    wx_7   = [r for r in wx_rows if cutoff_7 <= r['date'] < today_str]
    wx_mtd = [r for r in wx_rows if month_start <= r['date'] < today_str]
    wx_ytd = [r for r in wx_rows if year_start  <= r['date'] < today_str]

    rain_7   = sum(float(r.get('rain_mm') or 0) for r in wx_7)
    rain_mtd = sum(float(r.get('rain_mm') or 0) for r in wx_mtd)
    rain_ytd = sum(float(r.get('rain_mm') or 0) for r in wx_ytd)
    rain_days_7 = sum(1 for r in wx_7 if float(r.get('rain_mm') or 0) >= 0.2)

    et_7   = sum(float(r.get('et_mm') or 0) for r in wx_7)
    et_mtd = sum(float(r.get('et_mm') or 0) for r in wx_mtd)

    # Water balance: ET - rain (positive = deficit, negative = surplus)
    bal_7   = et_7   - rain_7
    bal_mtd = et_mtd - rain_mtd

    # ── Cease-to-pump projection ───────────────────────────────────────────────
    cease_date    = lu.project_to_cease(ahd, now_syd.date())
    cost_to_march = lu.town_water_cost_projection(cease_date) if cease_date else None
    last_proj     = _get_last_week_projection()

    # Rainfall savings (two-component method - see town-water-cost-report.html)
    cfg_tw      = lu.get_config()['town_water']
    irrig_kl    = cfg_tw['daily_kl_by_month']
    cost_per_kl = cfg_tw['cost_per_kl']
    active_m    = set(lu.get_config()['irrigation_season']['active_months'])

    # Component 1: rain falling directly on lake surface (ML)
    rain_on_lake_ml = rain_7 * lu.lake_area_m2(ahd) / 1_000_000

    # Component 2: irrigation not pumped because rain covered ET demand (ML)
    # Only applies in active irrigation months; uses ET not BOM evaporation
    if month in active_m and et_7 > 0:
        et_covered_frac = min(rain_7, et_7) / et_7  # fraction of ET met by rain
        irrig_saved_ml  = et_covered_frac * float(irrig_kl.get(str(month), 0)) / 1000.0
    else:
        irrig_saved_ml  = 0.0

    total_rain_ml = rain_on_lake_ml + irrig_saved_ml

    # Convert ML benefit to cost savings using the boundary month rates
    rainfall_savings = None
    rain_days_saved  = None
    if cease_date and total_rain_ml > 0:
        bm        = cease_date.month   # boundary month
        bm_evap   = lu.evap_ml_day(ahd, bm)
        bm_pump   = float(irrig_kl.get(str(bm), 0)) / 1000.0
        bm_draw   = bm_evap + bm_pump
        bm_tw_day = float(irrig_kl.get(str(bm), 0)) * cost_per_kl
        if bm_draw > 0:
            rain_days_saved  = total_rain_ml / bm_draw
            rainfall_savings = rain_days_saved * bm_tw_day

    # Projection data to persist for next week's comparison
    proj_data = None
    if cease_date and cost_to_march is not None:
        proj_data = {
            'cease_date': cease_date.isoformat(),
            'cost':       round(cost_to_march, 2),
        }

    def _bal_str(val):
        if val > 0:
            return f'{val:.1f}&nbsp;mm deficit'
        elif val < 0:
            return f'{abs(val):.1f}&nbsp;mm surplus'
        return 'balanced'

    def _bal_col(val):
        return '#EB1E23' if val > 10 else ('#F58E1E' if val > 0 else '#00762A')

    temps_max = [float(r['temp_max']) for r in wx_7 if r.get('temp_max')]
    temps_min = [float(r['temp_min']) for r in wx_7 if r.get('temp_min')]
    week_temp = (f'{min(temps_min):.1f} to {max(temps_max):.1f}&nbsp;&deg;C'
                 if temps_max and temps_min else '-')

    # ── Tank ──────────────────────────────────────────────────────────────────
    tank_pct    = tank.get('tank_pct')
    tank_vol_l  = tank.get('tank_volume_l')
    tank_cap_l  = tank.get('tank_total_volume') or 250000
    tank_status = tank.get('tank_status', '')
    tank_chart  = _tank_chart_html(tank_readings)

    tank_pct_str = f'{tank_pct:.1f}%' if tank_pct is not None else '-'
    tank_vol_str = (f'{tank_vol_l:,.0f}&nbsp;L&nbsp;({tank_vol_l/1000:.1f}&nbsp;kL)'
                    if tank_vol_l is not None else '-')
    tank_cap_str = f'{tank_cap_l:,.0f}&nbsp;L&nbsp;({tank_cap_l/1000:.0f}&nbsp;kL)'

    # Weekly tank change: net volume difference over past 7 days
    tank_cutoff = (now_syd.date() - timedelta(days=7)).isoformat()
    tank_week_readings = [
        r for r in tank_readings
        if r.get('date', '') >= tank_cutoff and r.get('volume_l') is not None
    ]
    if len(tank_week_readings) >= 2:
        tank_week_readings.sort(key=lambda r: r['date'])
        tank_change_l = float(tank_week_readings[-1]['volume_l']) - float(tank_week_readings[0]['volume_l'])
        if tank_change_l < 0:
            tank_week_str  = f'{abs(tank_change_l):,.0f}&nbsp;L used'
            tank_week_sub  = f'net drawdown ({abs(tank_change_l)/1000:.1f}&nbsp;kL)'
            tank_week_col  = _HDR_BG
        else:
            tank_week_str  = f'+{tank_change_l:,.0f}&nbsp;L'
            tank_week_sub  = f'net gain ({tank_change_l/1000:.1f}&nbsp;kL)'
            tank_week_col  = '#00762A'
    else:
        tank_week_str = '-'
        tank_week_sub = 'insufficient data'
        tank_week_col = '#64748b'

    # ── Licence level reference table rows ────────────────────────────────────
    level_rows = ''
    for z in cfg['zone_thresholds']:
        is_cur = (z['zone'] == lv_num)
        row_bg = _ROW_A if z['zone'] % 2 == 1 else _ROW_B
        weight = '700' if is_cur else '400'
        marker = '&nbsp;&nbsp;&#9664; current' if is_cur else ''
        t_str  = (f'&ge;&nbsp;{z["min_ahd"]:.3f}&nbsp;m&nbsp;AHD'
                  if z['min_ahd'] is not None else '&lt;&nbsp;189.650&nbsp;m&nbsp;AHD')
        pill   = (f'<span style="display:inline-block;background:{z["color_bg"]};'
                  f'color:{z["color_text"]};font-size:10px;font-weight:700;'
                  f'padding:2px 8px;border-radius:4px;">Level&nbsp;{z["zone"]}</span>')
        level_rows += f"""
    <tr>
      <td bgcolor="{row_bg}" style="background-color:{row_bg};padding:7px 10px;
          font-family:Arial,sans-serif;font-size:12px;color:#1b2631;
          border-bottom:1px solid {_BORDER};">{pill}&nbsp;
        <span style="font-weight:{weight};">{z['name']}{marker}</span></td>
      <td bgcolor="{row_bg}" style="background-color:{row_bg};padding:7px 10px;
          font-family:Arial,sans-serif;font-size:12px;color:#475569;white-space:nowrap;
          border-bottom:1px solid {_BORDER};">{t_str}</td>
      <td bgcolor="{row_bg}" style="background-color:{row_bg};padding:7px 10px;
          font-family:Arial,sans-serif;font-size:12px;font-weight:{weight};
          color:#1b2631;white-space:nowrap;text-align:right;
          border-bottom:1px solid {_BORDER};">{z['max_pump_ml_day']:.2f}&nbsp;ML/day</td>
    </tr>"""

    # ── Next level banner ──────────────────────────────────────────────────────
    next_banner = ''
    if nxt and days not in (None, float('inf')):
        next_banner = f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:8px;">
  <tr>
    <td style="background:#fff8e1;padding:12px 20px;border-left:4px solid #F58E1E;">
      <p style="margin:0;font-family:Arial,sans-serif;font-size:12px;color:#78350f;">
        <strong>Next level: Level&nbsp;{nxt['zone']} - {nxt['name']}</strong>
        &nbsp;&bull;&nbsp; Entry threshold: {level['min_ahd']:.3f}&nbsp;m AHD
        &nbsp;&bull;&nbsp; Max extraction drops to
        <strong>{nxt['max_pump_ml_day']:.2f}&nbsp;ML/day</strong>
      </p>
    </td>
  </tr>
</table>"""

    # ── Cease-to-pump projection banner ───────────────────────────────────────
    projection_banner = ''
    if lv_num >= 2:  # show when not at Normal Operations (Level 1)
        if cease_date is None:
            # Already at or below cease level
            _pb_date_html = '<span style="color:#b83c3c;">CEASE LEVEL REACHED</span>'
            _pb_days_html = 'extraction must stop now'
            _pb_cost_str  = (f'${cost_to_march:,.0f}' if cost_to_march else '-')
            _pb_cost_sub  = f'today to 31 Mar {now_syd.year + 1}'
        else:
            _cease_str    = f'{cease_date.day} {cease_date.strftime("%b %Y")}'
            _days_away    = (cease_date - now_syd.date()).days
            _pb_date_html = _cease_str
            _pb_days_html = f'{_days_away:,} days from today'
            _end_yr       = cease_date.year + (1 if cease_date.month > 3 else 0)
            _pb_cost_str  = (f'${cost_to_march:,.0f}' if cost_to_march is not None else '-')
            _pb_cost_sub  = f'{_cease_str} to 31 Mar {_end_yr}'

        # Rainfall, evaporation and ET - always shown when a cease date is known
        _rain_row = ''
        if cease_date is not None:
            _lk_area_km2    = lu.lake_area_m2(ahd) / 1_000_000
            _pan_day        = float(lu.get_config()['evaporation']['monthly_pan_mm_day'][str(month)])
            _evap_mm_day    = _pan_day * lu.get_config()['evaporation']['pan_factor']
            _weekly_evap_ml = lu.evap_ml_day(ahd, month) * 7
            _net_lake_ml    = rain_on_lake_ml - _weekly_evap_ml
            _net_lake_str   = (f'+{_net_lake_ml:.1f}&nbsp;ML added to the lake this week'
                               if _net_lake_ml >= 0
                               else f'{abs(_net_lake_ml):.1f}&nbsp;ML net loss (evaporation outweighed rain)')

            # Flag potential sensor anomaly if any single day ET > 2.5x BOM monthly pan
            _et_max_day = max((float(r.get('et_mm') or 0) for r in wx_7), default=0)
            _et_note    = ('<br><em style="color:#64748b;font-size:10px;">Note: one day recorded unusually '
                           'high ET - station sensor may include an anomaly in this figure.</em>'
                           if _et_max_day > 2.5 * _pan_day else '')

            if month in active_m and et_7 > 0:
                _rain_cov_mm   = min(rain_7, et_7)
                _et_pct        = _rain_cov_mm / et_7 * 100
                _pump_total_kl = float(irrig_kl.get(str(month), 0)) * 7
                _pump_saved_kl = irrig_saved_ml * 1000
                _pump_still_kl = _pump_total_kl - _pump_saved_kl
                if rain_7 >= 0.2:
                    _course_html = (
                        f'The grass needed <strong>{et_7:.1f}&nbsp;mm</strong> of water this week '
                        f'(evapotranspiration - what turf uses through sun, wind and growth).'
                        f'<br>Rain provided <strong>{rain_7:.1f}&nbsp;mm</strong>, '
                        f'covering <strong>{_et_pct:.0f}%</strong> of that demand.'
                        f'<br>The remaining <strong>{max(0,et_7-rain_7):.1f}&nbsp;mm</strong> '
                        f'was pumped from the lake (approx. '
                        f'<strong>{_pump_still_kl:,.0f}&nbsp;kL</strong>). '
                        f'Rain saved ~<strong>{_pump_saved_kl:.0f}&nbsp;kL</strong> of lake water.'
                        f'{_et_note}'
                    )
                else:
                    _course_html = (
                        f'No meaningful rainfall this week. '
                        f'The grass needed <strong>{et_7:.1f}&nbsp;mm</strong> of water '
                        f'(evapotranspiration - what turf uses through sun, wind and growth).'
                        f'<br>The full demand was pumped from the lake '
                        f'(approx. <strong>{_pump_still_kl:,.0f}&nbsp;kL</strong> this week).'
                        f'{_et_note}'
                    )
            else:
                _course_html = (
                    f'No irrigation is running this month (June-August - winter off-season).'
                    f'<br>Rainfall this week: <strong>{rain_7:.1f}&nbsp;mm</strong>. '
                    f'Winter rain builds up lake reserves for the spring irrigation season, '
                    f'but does not replace irrigation pumping directly.'
                    f'{_et_note}'
                )

            # Boundary month rates - always computed so both rain and no-rain cases can use them
            _bm_month_name = cease_date.strftime('%B')
            _bm_kl_day     = float(irrig_kl.get(str(cease_date.month), 347))
            _bm_day_cost   = _bm_kl_day * cost_per_kl
            _bm_evap_ml    = lu.evap_ml_day(ahd, cease_date.month)
            _bm_pump_ml    = float(irrig_kl.get(str(cease_date.month), 0)) / 1000.0
            _bm_draw       = _bm_evap_ml + _bm_pump_ml

            if rainfall_savings is not None and rain_days_saved is not None and rain_days_saved > 0.05:
                _sav_str     = f'~${rainfall_savings:,.0f}'
                _days_str    = f'{rain_days_saved:.1f}&nbsp;days'
                _irrig_extra = (f', plus {irrig_saved_ml:.2f}&nbsp;ML saved by not irrigating,'
                                if irrig_saved_ml > 0.01 else '')
                _fin_html = (
                    f'The <strong>{rain_on_lake_ml:.1f}&nbsp;ML</strong> added to the lake by this '
                    f'week\'s rainfall{_irrig_extra} extends the projected cease date by approximately '
                    f'<strong>{_days_str}</strong>.'
                    f'<br>At the {_bm_month_name} town water rate of '
                    f'<strong>${_bm_day_cost:,.0f}/day</strong>, '
                    f'this saves the club an estimated <strong>{_sav_str}</strong> in future costs.'
                )
            else:
                _evap_days   = (_weekly_evap_ml / _bm_draw) if _bm_draw > 0 else 0
                _week_cost   = _evap_days * _bm_day_cost
                _fin_html = (
                    f'No rainfall offset this week. Evaporation removed '
                    f'<strong>{_weekly_evap_ml:.1f}&nbsp;ML</strong> from the lake, '
                    f'advancing the projected cease date by approximately '
                    f'<strong>{_evap_days:.1f}&nbsp;days</strong> this week alone.'
                    f'<br>At the {_bm_month_name} town water rate of '
                    f'<strong>${_bm_day_cost:,.0f}/day</strong>, each week without rain '
                    f'increases the club\'s future cost exposure by approximately '
                    f'<strong>~${_week_cost:,.0f}</strong>.'
                )

            _rain_row = f"""
      <tr>
        <td colspan="2" style="padding:0;border-top:1px solid #fca5a5;">
          <table width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td colspan="2" bgcolor="#fee2e2" style="background-color:#fee2e2;
                  padding:6px 16px;border-bottom:1px solid #fca5a5;">
                <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;
                    color:#991b1b;text-transform:uppercase;letter-spacing:0.5px;">
                  Impact on the Lake This Week
                </p>
              </td>
            </tr>
            <tr>
              <td width="50%" style="background:#fef2f2;padding:10px 16px;
                  border-right:1px solid #fca5a5;border-bottom:1px solid #fca5a5;vertical-align:top;">
                <p style="margin:0 0 3px 0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;
                    color:#991b1b;text-transform:uppercase;">Rainfall Added to Lake</p>
                <p style="margin:0;font-family:Arial,sans-serif;font-size:12px;color:#1b2631;line-height:1.6;">
                  {rain_7:.1f}&nbsp;mm rain &times; {_lk_area_km2:.2f}&nbsp;km&sup2; lake<br>
                  <strong>= {rain_on_lake_ml:.1f}&nbsp;ML added</strong>
                </p>
              </td>
              <td width="50%" style="background:#fef2f2;padding:10px 16px;
                  border-bottom:1px solid #fca5a5;vertical-align:top;">
                <p style="margin:0 0 3px 0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;
                    color:#991b1b;text-transform:uppercase;">Evaporation Lost from Lake</p>
                <p style="margin:0;font-family:Arial,sans-serif;font-size:12px;color:#1b2631;line-height:1.6;">
                  {_evap_mm_day:.2f}&nbsp;mm/day (sun &amp; wind) &times; 7&nbsp;days<br>
                  <strong>= {_weekly_evap_ml:.1f}&nbsp;ML lost</strong> (always occurs)
                </p>
              </td>
            </tr>
            <tr>
              <td colspan="2" style="background:#fff5f5;padding:8px 16px;
                  border-bottom:1px solid #fca5a5;">
                <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:#7f1d1d;">
                  <strong>Net: {_net_lake_str}</strong>
                </p>
              </td>
            </tr>
            <tr>
              <td colspan="2" bgcolor="#fee2e2" style="background-color:#fee2e2;
                  padding:6px 16px;border-bottom:1px solid #fca5a5;">
                <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;
                    color:#991b1b;text-transform:uppercase;letter-spacing:0.5px;">
                  Impact on Course Irrigation This Week
                </p>
              </td>
            </tr>
            <tr>
              <td colspan="2" style="background:#fff5f5;padding:10px 16px;
                  border-bottom:1px solid #fca5a5;">
                <p style="margin:0;font-family:Arial,sans-serif;font-size:12px;
                    color:#1b2631;line-height:1.7;">{_course_html}</p>
              </td>
            </tr>
            <tr>
              <td colspan="2" bgcolor="#fee2e2" style="background-color:#fee2e2;
                  padding:6px 16px;border-bottom:1px solid #fca5a5;">
                <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;
                    color:#991b1b;text-transform:uppercase;letter-spacing:0.5px;">
                  What This Means for the Club
                </p>
              </td>
            </tr>
            <tr>
              <td colspan="2" style="background:#fff5f5;padding:10px 16px;">
                <p style="margin:0;font-family:Arial,sans-serif;font-size:12px;
                    color:#1b2631;line-height:1.7;">{_fin_html}</p>
              </td>
            </tr>
          </table>
        </td>
      </tr>"""

        projection_banner = f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:8px;">
  <tr>
    <td style="padding:0;border:1px solid #fca5a5;border-left:4px solid #b83c3c;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td width="50%" style="background:#fef2f2;padding:12px 16px;
              border-right:1px solid #fca5a5;vertical-align:top;">
            <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;
                color:#991b1b;text-transform:uppercase;letter-spacing:0.5px;">Projected Cease Date</p>
            <p style="margin:4px 0 2px 0;font-family:Arial,sans-serif;font-size:20px;
                font-weight:700;color:#b83c3c;">{_pb_date_html}</p>
            <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;
                color:#991b1b;">{_pb_days_html}</p>
          </td>
          <td width="50%" style="background:#fef2f2;padding:12px 16px;vertical-align:top;">
            <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;
                color:#991b1b;text-transform:uppercase;letter-spacing:0.5px;">Est. Town Water Cost</p>
            <p style="margin:4px 0 2px 0;font-family:Arial,sans-serif;font-size:20px;
                font-weight:700;color:#b83c3c;">{_pb_cost_str}</p>
            <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;
                color:#991b1b;">{_pb_cost_sub}</p>
          </td>
        </tr>{_rain_row}
      </table>
    </td>
  </tr>
</table>"""

    # ── Daily weather table rows ───────────────────────────────────────────────
    daily_rows = ''
    for i, r in enumerate(reversed(wx_7)):
        bg = _ROW_A if i % 2 == 0 else _ROW_B
        try:
            _dt = datetime.fromisoformat(r['date'])
            d_label = f'{_dt.strftime("%a")} {_dt.day} {_dt.strftime("%b")}'
        except Exception:
            d_label = r['date']
        wind_dir = _deg_to_compass(r.get('wind_dir_deg'))
        wind_spd = r.get('wind_max_kmh', '').strip()
        if wind_spd:
            wind_str = f'{float(wind_spd):.0f}&nbsp;km/h&nbsp;{wind_dir}'.strip()
        else:
            wind_str = wind_dir if wind_dir else '-'

        td = (f'style="background-color:{bg};padding:6px 7px;'
              f'font-family:Arial,sans-serif;font-size:11px;color:#1b2631;'
              f'border-bottom:1px solid {_BORDER};border-right:1px solid {_BORDER};'
              f'white-space:nowrap;text-align:center;"')
        td_l = (f'style="background-color:{bg};padding:6px 7px;'
                f'font-family:Arial,sans-serif;font-size:11px;color:#1b2631;'
                f'border-bottom:1px solid {_BORDER};border-right:1px solid {_BORDER};'
                f'white-space:nowrap;"')
        daily_rows += f"""
    <tr>
      <td {td_l}>{d_label}</td>
      <td {td}>{_v(r.get('temp_max'))}&deg;</td>
      <td {td}>{_v(r.get('temp_min'))}&deg;</td>
      <td {td}>{_v(r.get('rain_mm'))}</td>
      <td {td}>{_v(r.get('rh_mean'), '.0f')}%</td>
      <td {td}>{_v(r.get('et_mm'))}</td>
      <td {td}>{_v(r.get('uv_max'), '.1f')}</td>
      <td {td}>{wind_str}</td>
      <td {td}>{_v(r.get('dew_point_c'))}&deg;</td>
    </tr>"""

    # ── Lake status card (5-dot scale) ───────────────────────────────────────
    _muted_bg = {1: '#d4edda', 2: '#e8f5d0', 3: '#fef9c3', 4: '#ffedd5', 5: '#fef2f2'}
    _muted_tx = {1: '#155724', 2: '#4a7215', 3: '#713f12', 4: '#9a3412', 5: '#991b1b'}
    _scale_cells = ''
    for _z in cfg['zone_thresholds']:
        _zn = _z['zone']
        _pump_lbl = (f'{_z["max_pump_ml_day"]:.2f}&nbsp;ML/day'
                     if _z['max_pump_ml_day'] > 0 else 'Cease')
        if _z['min_ahd'] is not None:
            _ahd_lbl = f'&ge;{_z["min_ahd"]:.3f}m'
        else:
            _prev_ahd = next((z['min_ahd'] for z in cfg['zone_thresholds'] if z['zone'] == _zn - 1), None)
            _ahd_lbl  = f'&lt;{_prev_ahd:.3f}m' if _prev_ahd else ''
        if _zn < lv_num:
            _bg = _muted_bg[_zn]; _ft = _muted_tx[_zn]; _brd = 'none'; _fw = '400'
            _sub = (f'<p style="margin:1px 0 0;font-family:Arial,sans-serif;'
                    f'font-size:9px;color:{_ft};">above</p>')
        elif _zn == lv_num:
            _bg = lv_bg; _ft = lv_txt; _brd = '2px solid #1b2631'; _fw = '700'
            _sub = (f'<p style="margin:4px 0 0;font-family:Arial,sans-serif;'
                    f'font-size:18px;line-height:1;color:{_ft};">&#9660;</p>')
        elif _zn == 5:
            _bg = '#fef2f2'; _ft = '#991b1b'; _brd = '1px solid #fca5a5'; _fw = '400'
            _sub = (f'<p style="margin:1px 0 0;font-family:Arial,sans-serif;'
                    f'font-size:9px;color:{_ft};">cease</p>')
        else:
            _bg = '#f1f5f9'; _ft = '#94a3b8'; _brd = 'none'; _fw = '400'
            _sub = ''
        _scale_cells += (
            f'<td width="20%" style="background-color:{_bg};padding:10px 4px 8px 4px;'
            f'border:{_brd};text-align:center;vertical-align:top;">'
            f'<p style="margin:0;font-family:Arial,sans-serif;font-size:11px;'
            f'font-weight:{_fw};color:{_ft};">Level&nbsp;{_zn}</p>'
            f'<p style="margin:2px 0 0;font-family:Arial,sans-serif;font-size:9px;'
            f'color:{_ft};">{_pump_lbl}</p>'
            f'<p style="margin:1px 0 0;font-family:Arial,sans-serif;font-size:8px;'
            f'color:{_ft};opacity:0.8;">{_ahd_lbl}</p>{_sub}</td>'
        )

    if nxt and days not in (None, float('inf')):
        _dv = int(days)
        if _dv <= 7:
            _wbg = '#fef2f2'; _wbord = '4px solid #b83c3c'; _wtxt = '#991b1b'; _wico = '&nbsp;&#9888;'
        elif _dv <= 30:
            _wbg = '#fff7ed'; _wbord = '4px solid #F58E1E'; _wtxt = '#9a3412'; _wico = '&nbsp;&#9888;'
        else:
            _wbg = _ROW_B; _wbord = f'1px solid {_BORDER}'; _wtxt = '#1b2631'; _wico = ''
        _warn_html = (
            f'<p style="margin:0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;'
            f'color:{_wtxt};text-transform:uppercase;">Days to Next Restriction{_wico}</p>'
            f'<p style="margin:4px 0 2px;font-family:Arial,sans-serif;font-size:22px;'
            f'font-weight:700;color:{_wtxt};">{days_display}</p>'
            f'<p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:{_wtxt};">'
            f'Pump limit drops to <strong>{nxt["max_pump_ml_day"]:.2f}&nbsp;ML/day</strong>'
            f' at Level&nbsp;{nxt["zone"]}</p>'
        )
    elif days == float('inf'):
        _wbg = '#f0fdf4'; _wbord = '1px solid #bbf7d0'; _wtxt = '#15803d'
        _warn_html = (
            f'<p style="margin:0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;'
            f'color:{_wtxt};text-transform:uppercase;">Lake Status</p>'
            f'<p style="margin:4px 0 2px;font-family:Arial,sans-serif;font-size:22px;'
            f'font-weight:700;color:{_wtxt};">Rising</p>'
            f'<p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:{_wtxt};">'
            f'Net lake gain this month</p>'
        )
    else:
        _wbg = _ROW_B; _wbord = f'1px solid {_BORDER}'; _wtxt = '#1b2631'
        _warn_html = (
            f'<p style="margin:0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;'
            f'color:{_wtxt};text-transform:uppercase;">Restriction Status</p>'
            f'<p style="margin:4px 0;font-family:Arial,sans-serif;font-size:20px;'
            f'font-weight:700;color:{_wtxt};">-</p>'
        )

    _lake_card_html = f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    <td colspan="2" bgcolor="{lv_bg}" style="background-color:{lv_bg};padding:14px 20px;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td style="vertical-align:middle;">
            <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;
                color:{lv_txt};letter-spacing:1.5px;text-transform:uppercase;">Lake Level</p>
            <p style="margin:4px 0 0;font-family:Arial,sans-serif;font-size:24px;
                font-weight:700;color:{lv_txt};">Level&nbsp;{lv_num}</p>
          </td>
          <td style="vertical-align:middle;text-align:right;">
            <p style="margin:0;font-family:Arial,sans-serif;font-size:16px;font-weight:700;
                color:{lv_txt};">{ahd:.3f}&nbsp;m&nbsp;AHD</p>
            <p style="margin:4px 0 0;font-family:Arial,sans-serif;font-size:12px;
                color:{lv_txt};">{trend_str if trend_str else 'No change this week'}</p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
  <tr>
    <td colspan="2" bgcolor="#f8fafc" style="background:#f8fafc;padding:12px 16px 8px 16px;">
      <table width="100%" cellpadding="0" cellspacing="3" border="0"
             style="border-collapse:separate;border-spacing:3px;">
        <tr>{_scale_cells}
        </tr>
      </table>
      <p style="margin:6px 0 0;font-family:Arial,sans-serif;font-size:10px;
          color:#64748b;text-align:center;">
        Level 1 = full operations &nbsp;&bull;&nbsp; Level 5 = cease to pump
        &nbsp;&bull;&nbsp; Intermediate levels reduce the daily pump limit
      </p>
    </td>
  </tr>
  <tr>
    <td width="50%" bgcolor="{_ROW_A}" style="background-color:{_ROW_A};padding:14px 16px;
        vertical-align:top;border-top:1px solid {_BORDER};">
      <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;
          color:#1b2631;text-transform:uppercase;">Current Pump Limit</p>
      <p style="margin:4px 0 2px;font-family:Arial,sans-serif;font-size:22px;
          font-weight:700;color:#1b2631;">{lv_pump:.2f}&nbsp;ML/day</p>
      <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:#475569;">
        {lv_pump * 1000:.0f}&nbsp;kL/day maximum extraction
      </p>
      <p style="margin:6px 0 0;font-family:Arial,sans-serif;font-size:11px;color:#475569;">
        {ahd:.3f}&nbsp;m&nbsp;AHD current reading
      </p>
    </td>
    <td width="50%" style="background-color:{_wbg};padding:14px 16px;vertical-align:top;
        border-top:1px solid {_BORDER};border-left:{_wbord};">
      {_warn_html}
    </td>
  </tr>
</table>"""

    # ── Assemble body ──────────────────────────────────────────────────────────
    body = (
        _header(f'Week ending {date_str}')

        # ── Cease-to-pump projection (top priority for board) ─────────────────
        + ((_card_open('Cease-to-Pump Projection - No Future Rainfall Assumed')
            + projection_banner
            + _card_close()) if projection_banner else '')

        # ── Lake section (single card: current level + chart + licence table) ────
        + _card_open('Lake Albert')
        + _lake_card_html
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    <td bgcolor="#f1f5f9" style="background-color:#f1f5f9;padding:8px 16px;
        border-top:1px solid #cbd5e1;">
      <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;font-weight:700;
          color:#475569;letter-spacing:0.5px;text-transform:uppercase;">Lake Level - Past 7 Days</p>
    </td>
  </tr>
  <tr>
    <td style="background:#0d1b2a;padding:12px;">
      {chart_html if chart_html else
       '<p style="color:#94a3b8;font-family:Arial;font-size:12px;margin:0;">No chart data available.</p>'}
    </td>
  </tr>
</table>
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    <td bgcolor="#f1f5f9" style="background-color:#f1f5f9;padding:8px 16px;
        border-top:1px solid #cbd5e1;">
      <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;font-weight:700;
          color:#475569;letter-spacing:0.5px;text-transform:uppercase;">Water Licence Levels</p>
    </td>
  </tr>
  <tr>
    <th style="background-color:{_HDR_BG};padding:7px 10px;font-family:Arial,sans-serif;
        font-size:11px;color:#ffffff;font-weight:700;border-right:1px solid {_BORDER};
        text-align:left;">Level</th>
    <th style="background-color:{_HDR_BG};padding:7px 10px;font-family:Arial,sans-serif;
        font-size:11px;color:#ffffff;font-weight:700;border-right:1px solid {_BORDER};
        text-align:left;">Threshold</th>
    <th style="background-color:{_HDR_BG};padding:7px 10px;font-family:Arial,sans-serif;
        font-size:11px;color:#ffffff;font-weight:700;text-align:right;">Max Extraction</th>
  </tr>
{level_rows}
</table>"""
        + _card_close()

        # ── Tank section ───────────────────────────────────────────────────────
        + _card_open('Water Tank')
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    {_stat_cell(25, _ROW_A, 'Tank Level',
        tank_pct_str, tank_status)}
    {_stat_cell(25, _ROW_B, 'Water Available',
        f'<span style="font-size:14px;">{tank_vol_str}</span>',
        'in tank now')}
    {_stat_cell(25, _ROW_A, 'This Week',
        f'<span style="color:{tank_week_col};">{tank_week_str}</span>',
        tank_week_sub)}
    {_stat_cell(25, _ROW_B, 'Total Capacity',
        f'<span style="font-size:14px;">{tank_cap_str}</span>',
        'tank capacity')}
  </tr>
</table>"""
        + (f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:8px;">
  <tr>
    <td style="background:#0d1b2a;padding:12px;">
      {tank_chart}
    </td>
  </tr>
</table>""" if tank_chart else '')
        + _card_close()

        # ── Weather section ────────────────────────────────────────────────────
        + _card_open('Weather - Past 7 Days')

        # Row 1: rainfall totals + temp
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:8px;">
  <tr>
    {_stat_cell(34, _ROW_A,
        'Week Rainfall',
        f'{rain_7:.1f}&nbsp;mm',
        f'{rain_days_7} rain day{"s" if rain_days_7 != 1 else ""}')}
    {_stat_cell(33, _ROW_B,
        'Month to Date',
        f'{rain_mtd:.1f}&nbsp;mm',
        mth_name)}
    {_stat_cell(33, _ROW_A,
        'Year to Date',
        f'{rain_ytd:.1f}&nbsp;mm',
        str(now_syd.year))}
  </tr>
</table>"""

        # Row 2: temperature + water balance
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:0;">
  <tr>
    {_stat_cell(34, _ROW_B,
        'Temp Range',
        f'<span style="font-size:14px;">{week_temp}</span>',
        'past 7 days')}
    {_stat_cell(33, _ROW_A,
        'Water Balance (Week)',
        f'<span style="color:{_bal_col(bal_7)};">{_bal_str(bal_7)}</span>',
        f'ET {et_7:.1f} mm &minus; Rain {rain_7:.1f} mm')}
    {_stat_cell(33, _ROW_B,
        f'Water Balance ({mth_name[:3]})',
        f'<span style="color:{_bal_col(bal_mtd)};">{_bal_str(bal_mtd)}</span>',
        f'ET {et_mtd:.1f} mm &minus; Rain {rain_mtd:.1f} mm')}
  </tr>
</table>"""

        # Daily weather table
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:12px;">
  <tr>
    <td style="padding:0;border:1px solid {_BORDER};">
      <div style="overflow-x:auto;">
      <table cellpadding="0" cellspacing="0" border="0"
             style="border-collapse:collapse;min-width:560px;width:100%;">
        <tr>
          <th style="background-color:{_HDR_BG};padding:7px 8px;font-family:Arial,sans-serif;
              font-size:11px;color:#ffffff;font-weight:700;text-align:left;
              border-right:1px solid {_BORDER};white-space:nowrap;">Date</th>
          <th style="background-color:{_HDR_BG};padding:7px 8px;font-family:Arial,sans-serif;
              font-size:11px;color:#ffffff;font-weight:700;text-align:center;
              border-right:1px solid {_BORDER};white-space:nowrap;">High</th>
          <th style="background-color:{_HDR_BG};padding:7px 8px;font-family:Arial,sans-serif;
              font-size:11px;color:#ffffff;font-weight:700;text-align:center;
              border-right:1px solid {_BORDER};white-space:nowrap;">Low</th>
          <th style="background-color:{_HDR_BG};padding:7px 8px;font-family:Arial,sans-serif;
              font-size:11px;color:#ffffff;font-weight:700;text-align:center;
              border-right:1px solid {_BORDER};white-space:nowrap;">Rain</th>
          <th style="background-color:{_HDR_BG};padding:7px 8px;font-family:Arial,sans-serif;
              font-size:11px;color:#ffffff;font-weight:700;text-align:center;
              border-right:1px solid {_BORDER};white-space:nowrap;">Hum</th>
          <th style="background-color:{_HDR_BG};padding:7px 8px;font-family:Arial,sans-serif;
              font-size:11px;color:#ffffff;font-weight:700;text-align:center;
              border-right:1px solid {_BORDER};white-space:nowrap;">ET</th>
          <th style="background-color:{_HDR_BG};padding:7px 8px;font-family:Arial,sans-serif;
              font-size:11px;color:#ffffff;font-weight:700;text-align:center;
              border-right:1px solid {_BORDER};white-space:nowrap;">UV</th>
          <th style="background-color:{_HDR_BG};padding:7px 8px;font-family:Arial,sans-serif;
              font-size:11px;color:#ffffff;font-weight:700;text-align:center;
              border-right:1px solid {_BORDER};white-space:nowrap;">Wind (Max)</th>
          <th style="background-color:{_HDR_BG};padding:7px 8px;font-family:Arial,sans-serif;
              font-size:11px;color:#ffffff;font-weight:700;text-align:center;
              white-space:nowrap;">Dew Pt</th>
        </tr>
{daily_rows}
      </table>
      </div>
    </td>
  </tr>
</table>"""
        + _card_close()

        # ── Disclaimer ─────────────────────────────────────────────────────────
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:24px;">
  <tr>
    <td style="padding:14px 20px;border-top:1px solid {_BORDER};">
      <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:#64748b;
          line-height:1.7;">
        <strong>Projection methodology:</strong> Days-to-next-level assumes pumping at the
        maximum current licence rate ({lv_pump:.2f}&nbsp;ML/day) with <em>no rainfall</em> -
        a conservative planning figure. {mth_name} open-water evaporation based on
        BOM Wagga Wagga Airport long-term average
        ({pan_mm:.2f}&nbsp;mm/day pan &times;&nbsp;0.70&nbsp;lake factor
        = {lake_mm:.2f}&nbsp;mm/day = {evap_ml:.2f}&nbsp;ML/day at current lake area).
        Lake surface area adjusts as level changes. All figures are estimates only.
        <strong>Water balance</strong> = ET &minus; rainfall; positive = irrigation demand
        exceeds rainfall (deficit), negative = rainfall exceeds ET (surplus).
        Weather data from on-site Davis weather station.
      </p>
    </td>
  </tr>
</table>"""

        # ── Footer ─────────────────────────────────────────────────────────────
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:0;">
  <tr>
    <td bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:16px 24px;text-align:center;">
      <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;
          color:rgba(255,255,255,0.45);">Wagga Wagga Country Club &middot; Lake Albert Irrigation System</p>
      <p style="margin:4px 0 0 0;font-family:Arial,sans-serif;font-size:10px;
          color:rgba(255,255,255,0.3);">Data: FarmBot sensor &middot; Davis weather station &middot; Updated weekly</p>
    </td>
  </tr>
</table>"""
    )

    return _wrap(body), proj_data


# ── Monthly board report ───────────────────────────────────────────────────────

def _monthly_header(subtitle):
    return f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    <td bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:20px 24px 18px 24px;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td valign="middle" class="mob-logo-cell" style="padding-right:16px;white-space:nowrap;width:1%;">
            <img src="{_LOGO_URL}" width="194" height="44" alt="Wagga Wagga Country Club" style="display:block;border:0;">
          </td>
          <td valign="middle" class="mob-text-cell">
            <p style="margin:0 0 6px 0;font-size:10px;color:#a9cce3;letter-spacing:2px;
                text-transform:uppercase;font-family:Arial,sans-serif;font-weight:normal;">
              Wagga Wagga Country Club &nbsp;&bull;&nbsp; Board Monthly Report
            </p>
            <h1 style="margin:0;font-size:22px;color:#ffffff;font-weight:bold;
                font-family:Arial,sans-serif;line-height:1.2;">Water &amp; Finance Summary</h1>
            <p style="margin:4px 0 0 0;font-size:12px;color:#a9cce3;
                font-family:Arial,sans-serif;font-weight:normal;">{subtitle}</p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>"""


def _section_label(text):
    """Blue subheading bar between cards."""
    return f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:4px;">
  <tr>
    <td bgcolor="#f1f5f9" style="background-color:#f1f5f9;padding:7px 16px;
        border-top:1px solid #cbd5e1;">
      <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;font-weight:700;
          color:#475569;letter-spacing:0.5px;text-transform:uppercase;">{text}</p>
    </td>
  </tr>
</table>"""


def _zone_pill_html(zone_num, zone_cfg):
    """Inline coloured zone pill."""
    z = next((z for z in zone_cfg if z['zone'] == zone_num), None)
    if not z:
        return f'Level {zone_num}'
    return (f'<span style="display:inline-block;background:{z["color_bg"]};color:{z["color_text"]};'
            f'font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;">'
            f'Level&nbsp;{zone_num}</span>')


def _month_lake_chart(readings, month_start, month_end, zone_cfg):
    """QuickChart.io PNG - daily average AHD for a calendar month."""
    by_day = defaultdict(list)
    ms, me = month_start.isoformat(), month_end.isoformat()
    for r in readings:
        try:
            d = r['date'][:10]
            if ms <= d <= me and r.get('ahd'):
                by_day[d].append(float(r['ahd']))
        except Exception:
            pass
    days  = sorted(by_day.keys())
    vals  = [round(sum(by_day[d]) / len(by_day[d]), 3) for d in days]
    labels = [f'{int(d[8:])} {datetime.strptime(d, "%Y-%m-%d").strftime("%b")}' for d in days]
    if not vals:
        return ''
    ymin = round(min(vals) - 0.05, 3)
    ymax = round(max(vals) + 0.05, 3)
    thresh_colors = {z['zone']: z['color_bg'] for z in zone_cfg}
    datasets = [{
        'label': 'Lake Level', 'data': vals,
        'borderColor': '#1abc9c', 'backgroundColor': 'rgba(26,188,156,0.12)',
        'fill': True, 'lineTension': 0.3, 'pointRadius': 2, 'borderWidth': 2,
    }]
    for z in zone_cfg:
        t = z.get('min_ahd')
        if t and ymin - 0.05 <= t <= ymax + 0.05:
            datasets.append({
                'data': [t] * len(days),
                'borderColor': thresh_colors.get(z['zone'], '#888'),
                'borderWidth': 1, 'borderDash': [5, 4],
                'fill': False, 'pointRadius': 0, 'lineTension': 0,
            })
    config = {
        'type': 'line',
        'data': {'labels': labels, 'datasets': datasets},
        'options': {
            'legend': {'display': False},
            'scales': {
                'xAxes': [{'gridLines': {'color': '#1e3040'},
                           'ticks': {'fontColor': '#4a6070', 'maxRotation': 45, 'autoSkip': True, 'maxTicksLimit': 10}}],
                'yAxes': [{'gridLines': {'color': '#1e3040'},
                           'ticks': {'fontColor': '#4a6070', 'min': ymin, 'max': ymax}}],
            },
        },
    }
    import urllib.parse as _up2
    cfg_json = json.dumps(config, separators=(',', ':'))
    url = f'https://quickchart.io/chart?bkg=%230d1b2a&w=560&h=200&c={_up2.quote(cfg_json)}'
    return (f'<img src="{url}" width="100%" alt="Lake level - {month_start.strftime("%B %Y")}"'
            f' style="display:block;border-radius:6px;max-width:100%;">')


def _load_zone_timeline():
    """Read lake_zone_history.csv and return sorted list of (date, zone_num) transitions.

    Returns list of (datetime.date, int) pairs, oldest first.
    Skips suppressed/initialised rows since they don't represent actual transitions.
    """
    timeline = []
    try:
        if not ZONE_HISTORY_CSV.exists():
            return []
        with ZONE_HISTORY_CSV.open(newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                event = row.get('event', '')
                new_z = row.get('new_zone', '')
                if event == 'suppressed' or not new_z:
                    continue
                try:
                    ts = datetime.strptime(row['timestamp_aest'], '%Y-%m-%d %H:%M:%S').date()
                    timeline.append((ts, int(new_z)))
                except Exception:
                    pass
    except Exception as e:
        log.warning(f'Could not load zone timeline: {e}')
    return sorted(timeline, key=lambda x: x[0])


def _estimate_ml_pumped(month_start, month_end, timeline, zone_cfg):
    """Estimate ML pumped from lake for a date range using zone history.

    Returns (zone_days, zone_ml_max, zone_ml_demand):
      zone_ml_max    - licence maximum rate x days (theoretical ceiling)
      zone_ml_demand - daily irrigation demand capped at the licence rate
                       (realistic estimate). Both noted as estimates since
                       the flow sensor is not yet connected.
    """
    from datetime import timedelta
    cfg = lu.get_config()
    active_m  = set(cfg['irrigation_season']['active_months'])
    irrig_kl  = cfg['town_water']['daily_kl_by_month']
    zone_rates = {z['zone']: z['max_pump_ml_day'] for z in zone_cfg}

    # Find zone active at month_start
    zone_at_start = None
    for d, z in reversed(timeline):
        if d <= month_start:
            zone_at_start = z
            break
    if zone_at_start is None and timeline:
        zone_at_start = timeline[0][1]
    if zone_at_start is None:
        zone_at_start = 1  # default if no history

    # Build list of (date, zone) for changes within range
    changes = [(month_start, zone_at_start)]
    for d, z in timeline:
        if month_start < d <= month_end:
            changes.append((d, z))

    # Accumulate ML by zone
    zone_days      = {}
    zone_ml        = {}
    zone_ml_demand = {}
    for i, (from_d, zone) in enumerate(changes):
        to_d = changes[i + 1][0] if i + 1 < len(changes) else month_end + timedelta(days=1)
        cur = from_d
        while cur < to_d:
            if cur.month in active_m:
                rate      = zone_rates.get(zone, 0)
                demand_ml = min(rate, float(irrig_kl.get(str(cur.month), 0)) / 1000.0)
                zone_days[zone]      = zone_days.get(zone, 0) + 1
                zone_ml[zone]        = zone_ml.get(zone, 0.0) + rate
                zone_ml_demand[zone] = zone_ml_demand.get(zone, 0.0) + demand_ml
            cur += timedelta(days=1)

    return zone_days, zone_ml, zone_ml_demand


def _historical_rain_avg(month_num, wx_rows, years=10):
    """Average total rainfall for a given month (1-12) across past N years of data."""
    from collections import defaultdict
    by_year = defaultdict(float)
    for r in wx_rows:
        try:
            d = datetime.strptime(r['date'], '%Y-%m-%d')
            if d.month == month_num:
                by_year[d.year] += float(r.get('rain_mm') or 0)
        except Exception:
            pass
    if not by_year:
        return None
    # Use up to `years` most-recent complete years
    sorted_years = sorted(by_year.keys())[-years:]
    vals = [by_year[y] for y in sorted_years]
    return sum(vals) / len(vals)


def build_monthly_html(now_syd):
    """Build the monthly board report. Reports on the PRIOR calendar month."""
    from datetime import timedelta, date as _date

    today      = now_syd.date()
    month_end  = today.replace(day=1) - timedelta(days=1)
    month_start = month_end.replace(day=1)
    month_label = month_end.strftime('%B %Y')
    month_num   = month_end.month
    year        = month_end.year

    # Season: Sep 1 of prior year → Aug 31 of current year (irrigation year)
    # If reporting month is Jan-Aug → season started Sep 1 two years ago?
    # Simpler: just track from Sep 1 of the year in which the reporting month falls
    if month_num >= 9:
        season_start = _date(year, 9, 1)
    else:
        season_start = _date(year - 1, 9, 1)

    # ── Load data ──────────────────────────────────────────────────────────────
    lake_latest   = _load_json(_DATA_DIR / 'farmbot_lake_latest.json')
    readings_raw  = _load_json(_DATA_DIR / 'farmbot_lake_readings.json')
    tank          = _load_json(_DATA_DIR / 'farmbot_latest.json')
    tank_readings = _load_json(_DATA_DIR / 'farmbot_readings.json')

    if any(v is None for v in (lake_latest, readings_raw, tank, tank_readings)):
        log.error('Monthly report: one or more required data files missing')
        return None

    wx_rows = []
    try:
        with open(_DATA_DIR / 'daily_log.csv', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                if r.get('date'):
                    wx_rows.append(r)
        wx_rows.sort(key=lambda r: r['date'])
    except Exception as e:
        log.error(f'Monthly report: failed to load daily_log.csv: {e}')
        return None

    cfg        = lu.get_config()
    zone_cfg   = cfg['zone_thresholds']
    active_m   = set(cfg['irrigation_season']['active_months'])
    cost_per_kl = cfg['town_water']['cost_per_kl']
    irrig_kl   = cfg['town_water']['daily_kl_by_month']

    # ── Current lake state ────────────────────────────────────────────────────
    ahd     = float(lake_latest.get('lake_ahd') or 0)
    level   = lu.current_zone_info(ahd)
    lv_num  = level['zone']
    lv_bg   = level['color_bg']
    lv_txt  = level['color_text']
    lv_name = level['name']
    lv_pump = level['max_pump_ml_day']

    # ── Lake readings for the reporting month ──────────────────────────────────
    ms_iso, me_iso = month_start.isoformat(), month_end.isoformat()
    month_readings = sorted(
        [r for r in readings_raw
         if r.get('date', '')[:10] >= ms_iso and r.get('date', '')[:10] <= me_iso
         and r.get('ahd')],
        key=lambda r: r['date']
    )
    ahd_vals_m = [float(r['ahd']) for r in month_readings]
    ahd_high   = max(ahd_vals_m) if ahd_vals_m else None
    ahd_low    = min(ahd_vals_m) if ahd_vals_m else None
    ahd_avg    = sum(ahd_vals_m) / len(ahd_vals_m) if ahd_vals_m else None
    ahd_start  = ahd_vals_m[0]  if ahd_vals_m else None
    ahd_end    = ahd_vals_m[-1] if ahd_vals_m else None
    month_change = (ahd_end - ahd_start) if (ahd_start and ahd_end) else None

    # Volume change in ML (depth change × average surface area)
    if month_change is not None and ahd_avg:
        avg_area = lu.lake_area_m2(ahd_avg)
        change_ml = month_change * avg_area / 1000.0
    else:
        change_ml = None

    # Same month last year
    ly_ms = month_start.replace(year=year - 1).isoformat()
    ly_me = month_end.replace(year=year - 1).isoformat()
    ly_readings = [r for r in readings_raw
                   if r.get('date', '')[:10] >= ly_ms and r.get('date', '')[:10] <= ly_me
                   and r.get('ahd')]
    ly_avg = (sum(float(r['ahd']) for r in ly_readings) / len(ly_readings)) if ly_readings else None

    # ── Zone history & ML estimation ──────────────────────────────────────────
    timeline   = _load_zone_timeline()
    zone_days_m, zone_ml_m, zone_mld_m = _estimate_ml_pumped(month_start, month_end, timeline, zone_cfg)
    zone_days_s, zone_ml_s, zone_mld_s = _estimate_ml_pumped(season_start, month_end, timeline, zone_cfg)

    total_ml_m  = sum(zone_ml_m.values())    # licence maximum
    total_ml_s  = sum(zone_ml_s.values())
    total_mld_m = sum(zone_mld_m.values())   # demand-based estimate
    total_mld_s = sum(zone_mld_s.values())

    # ── Town water savings ─────────────────────────────────────────────────────
    from datetime import timedelta as _td
    def _month_saving(from_d, to_d):
        """Daily irrigation cost × days in active months between dates."""
        total = 0.0
        cur = from_d
        while cur <= to_d:
            if cur.month in active_m:
                total += float(irrig_kl.get(str(cur.month), 0)) * cost_per_kl
            cur += timedelta(days=1)
        return total

    saving_month  = _month_saving(month_start, month_end)
    saving_season = _month_saving(season_start, month_end)

    # Town-supplied irrigation cost on ceased (zone 5) days - the club only
    # switches to tank/town water when the lake hits cease-to-pump
    def _ceased_cost(from_d, to_d):
        total, days = 0.0, 0
        zmap = dict()
        cur = from_d
        zone = None
        idx = 0
        while cur <= to_d:
            while idx < len(timeline) and timeline[idx][0] <= cur:
                zone = timeline[idx][1]
                idx += 1
            if cur.month in active_m and zone == 5:
                total += float(irrig_kl.get(str(cur.month), 0)) * cost_per_kl
                days  += 1
            cur += timedelta(days=1)
        return total, days

    ceased_cost_m, ceased_days_m = _ceased_cost(month_start, month_end)
    ceased_cost_s, ceased_days_s = _ceased_cost(season_start, month_end)

    # ── Level 1 capacity comparison ────────────────────────────────────────────
    max_rate_l1 = next((z['max_pump_ml_day'] for z in zone_cfg if z['zone'] == 1), 1.5)
    days_in_active_month = sum(1 for d in
        [month_start + timedelta(days=i) for i in range((month_end - month_start).days + 1)]
        if d.month in active_m)
    max_ml_l1 = max_rate_l1 * days_in_active_month
    utilisation_pct = (total_mld_m / max_ml_l1 * 100) if max_ml_l1 > 0 else None

    # ── Rainfall calculations ─────────────────────────────────────────────────
    wx_month  = [r for r in wx_rows if ms_iso <= r['date'] <= me_iso]
    wx_season = [r for r in wx_rows if season_start.isoformat() <= r['date'] <= me_iso]

    rain_month  = sum(float(r.get('rain_mm') or 0) for r in wx_month)
    rain_season = sum(float(r.get('rain_mm') or 0) for r in wx_season)
    rain_days_m = sum(1 for r in wx_month if float(r.get('rain_mm') or 0) >= 0.2)
    et_month    = sum(float(r.get('et_mm') or 0) for r in wx_month)
    rain_avg    = _historical_rain_avg(month_num, wx_rows)
    rain_vs_avg = (rain_month - rain_avg) if rain_avg is not None else None

    # ── Outlook ────────────────────────────────────────────────────────────────
    cease_date    = lu.project_to_cease(ahd, today)
    cost_to_march = lu.town_water_cost_projection(cease_date) if cease_date else None
    days_to_cease = (cease_date - today).days if cease_date else None
    days_nxt, nxt_zone = lu.days_to_next_zone(ahd, today.month)

    # ── Zone change log for this month ────────────────────────────────────────
    zone_events = []
    try:
        if ZONE_HISTORY_CSV.exists():
            with ZONE_HISTORY_CSV.open(newline='', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    if row.get('event') in ('initialised', 'suppressed'):
                        continue
                    try:
                        d = datetime.strptime(row['timestamp_aest'], '%Y-%m-%d %H:%M:%S').date()
                        if month_start <= d <= month_end:
                            zone_events.append(row)
                    except Exception:
                        pass
    except Exception as e:
        log.warning(f'Monthly report: could not read zone history: {e}')

    # ── Chart ─────────────────────────────────────────────────────────────────
    lake_chart = _month_lake_chart(readings_raw, month_start, month_end, zone_cfg)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _fmt_ahd(v):
        return f'{v:.3f}&nbsp;m&nbsp;AHD' if v is not None else '-'

    def _fmt_ml(v):
        return f'{v:.1f}&nbsp;ML' if v is not None else '-'

    def _fmt_dollar(v):
        return f'${v:,.0f}' if v is not None else '-'

    def _signed(v, fmt='.3f', unit='m'):
        if v is None:
            return '-'
        sign = '+' if v >= 0 else ''
        return f'{sign}{v:{fmt}}&nbsp;{unit}'

    chg_col = '#00762A' if (month_change or 0) >= 0 else '#EB1E23'
    chg_arrow = '&uarr;' if (month_change or 0) > 0.001 else ('&darr;' if (month_change or 0) < -0.001 else '&rarr;')

    ly_vs = ''
    if ly_avg is not None and ahd_avg is not None:
        diff = ahd_avg - ly_avg
        ly_col = '#00762A' if diff >= 0 else '#EB1E23'
        ly_arrow = '&uarr;' if diff > 0.001 else ('&darr;' if diff < -0.001 else '&rarr;')
        ly_vs = (f'<span style="color:{ly_col};font-weight:700;">'
                 f'{ly_arrow} {abs(diff):.3f}&nbsp;m vs {year-1}</span>')

    # ── Build HTML ─────────────────────────────────────────────────────────────
    date_str = f'{month_start.day} {month_start.strftime("%B")} - {month_end.day} {month_end.strftime("%B %Y")}'

    # 1. Header
    body = _monthly_header(date_str)

    # 2. Headline stat row (4 cells)
    util_str = f'{utilisation_pct:.0f}%' if utilisation_pct is not None else '-'
    util_sub = f'of Level 1 max ({max_ml_l1:.1f} ML available)'
    saving_col = '#00762A' if saving_month > 0 else '#64748b'

    body += f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:20px;">
  <tr>
    {_stat_cell(25, _ROW_A, 'Est. ML Pumped',
        f'<span style="font-size:18px;">{_fmt_ml(total_mld_m)}</span>',
        f'{month_label} (licence max {_fmt_ml(total_ml_m)})', accent='#2471a3')}
    {_stat_cell(25, _ROW_B, 'Town Water Saved',
        f'<span style="color:{saving_col};font-size:18px;">{_fmt_dollar(saving_month)}</span>',
        f'{month_label}', accent='#00762A')}
    {_stat_cell(25, _ROW_A, 'Season Saving',
        f'<span style="color:#00762A;font-size:18px;">{_fmt_dollar(saving_season)}</span>',
        f'Sep {season_start.year} to date', accent='#00762A')}
    {_stat_cell(25, _ROW_B, 'Licence Utilisation',
        f'<span style="font-size:18px;">{util_str}</span>',
        util_sub, accent='#2471a3')}
  </tr>
</table>"""

    # 3. Lake level card
    level_rows_m = ''
    for z in zone_cfg:
        days_in = zone_days_m.get(z['zone'], 0)
        ml_in   = zone_mld_m.get(z['zone'], 0.0)
        ml_max  = zone_ml_m.get(z['zone'], 0.0)
        if days_in == 0:
            continue
        is_cur = (z['zone'] == lv_num)
        rbg    = _ROW_A if z['zone'] % 2 == 1 else _ROW_B
        marker = '&nbsp;&nbsp;&#9664; now' if is_cur else ''
        pill   = _zone_pill_html(z['zone'], zone_cfg)
        level_rows_m += f"""
    <tr>
      <td bgcolor="{rbg}" style="background-color:{rbg};padding:7px 10px;
          font-family:Arial,sans-serif;font-size:12px;color:#1b2631;
          border-bottom:1px solid {_BORDER};">{pill}&nbsp;
        <span style="font-weight:{'700' if is_cur else '400'};">{z['name']}{marker}</span></td>
      <td bgcolor="{rbg}" style="background-color:{rbg};padding:7px 10px;
          font-family:Arial,sans-serif;font-size:12px;color:#475569;white-space:nowrap;
          border-bottom:1px solid {_BORDER};text-align:right;">{days_in}&nbsp;days</td>
      <td bgcolor="{rbg}" style="background-color:{rbg};padding:7px 10px;
          font-family:Arial,sans-serif;font-size:12px;color:#475569;white-space:nowrap;
          border-bottom:1px solid {_BORDER};text-align:right;">{ml_in:.2f}&nbsp;ML&nbsp;est.
        <span style="color:#94a3b8;">(max&nbsp;{ml_max:.2f})</span></td>
    </tr>"""

    lake_kv_rows = [
        ('Month High',  _fmt_ahd(ahd_high)),
        ('Month Low',   _fmt_ahd(ahd_low)),
        ('Month Average', _fmt_ahd(ahd_avg)),
        ('Start of Month', _fmt_ahd(ahd_start)),
        ('End of Month',   _fmt_ahd(ahd_end)),
        ('Month Change',
         f'<span style="color:{chg_col};">{chg_arrow} {_signed(month_change)}'
         f'{f" ({_fmt_ml(change_ml)})" if change_ml is not None else ""}</span>'),
        ('vs Same Month Last Year', ly_vs if ly_vs else '-'),
    ]
    kv_html = ''
    for i, (k, v) in enumerate(lake_kv_rows):
        rbg = _ROW_A if i % 2 == 0 else _ROW_B
        kv_html += f"""
    <tr>
      <td bgcolor="{rbg}" style="background-color:{rbg};padding:7px 12px;
          font-family:Arial,sans-serif;font-size:12px;font-weight:700;color:#1a4a2e;
          border-bottom:1px solid {_BORDER};width:50%;">{k}</td>
      <td bgcolor="{rbg}" style="background-color:{rbg};padding:7px 12px;
          font-family:Arial,sans-serif;font-size:12px;color:#111827;
          border-bottom:1px solid {_BORDER};">{v}</td>
    </tr>"""

    body += (
        _card_open(f'Lake Albert - {month_label}')
        + f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="border-collapse:collapse;">{kv_html}
</table>"""
        + _section_label(f'Lake Level - {month_label}')
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    <td style="background:#0d1b2a;padding:12px;">
      {lake_chart if lake_chart else
       '<p style="color:#94a3b8;font-family:Arial;font-size:12px;margin:0;">No chart data available.</p>'}
    </td>
  </tr>
</table>"""
        + _card_close()
    )

    # 4. Water cost & savings card
    irrig_note = ('<p style="margin:8px 12px 0;font-family:Arial,sans-serif;font-size:10px;color:#94a3b8;">'
                  '* Est. - flow sensor not yet connected. ML est. = daily irrigation demand capped at the '
                  'licence zone rate; (max) = licence maximum extraction for the days in that zone.</p>')

    # Next-month town water exposure: what this month's irrigation would cost
    # if the lake could not be pumped (cease). The monthly email sends on the
    # 1st, so "next month" is the month just starting.
    import calendar as _cal
    nm_year, nm_month = today.year, today.month
    nm_days   = _cal.monthrange(nm_year, nm_month)[1]
    nm_kl_day = float(irrig_kl.get(str(nm_month), 0))
    nm_cost   = nm_kl_day * nm_days * cost_per_kl
    nm_label  = _date(nm_year, nm_month, 1).strftime('%B %Y')
    if nm_kl_day > 0:
        nm_text = (f'If pumping from the lake ceased, {nm_label} irrigation from town water would '
                   f'cost approximately <strong>{_fmt_dollar(nm_cost)}</strong> '
                   f'({nm_kl_day:.0f} kL/day x {nm_days} days @ ${cost_per_kl:.2f}/kL).')
        nm_border, nm_bg, nm_col = '#2471a3', '#eff6ff', '#1e3a8a'
    else:
        nm_text = (f'{nm_label} is outside the irrigation season - no town water exposure '
                   f'if pumping ceased.')
        nm_border, nm_bg, nm_col = '#94a3b8', '#f8fafc', '#475569'
    if ceased_days_s > 0:
        ceased_html = (
            f'<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"'
            f' style="border-collapse:collapse;margin-top:12px;"><tr>'
            f'<td bgcolor="#fef2f2" style="background-color:#fef2f2;border-left:4px solid #EB1E23;'
            f'padding:10px 14px;font-family:Arial,sans-serif;font-size:12px;color:#991b1b;'
            f'border-radius:0 6px 6px 0;"><strong>Town-supplied irrigation while ceased:</strong> '
            f'{_fmt_dollar(ceased_cost_m)} this month ({ceased_days_m} days) - '
            f'{_fmt_dollar(ceased_cost_s)} season to date ({ceased_days_s} days). '
            f'Irrigation runs from tank/town water only while the lake is below the '
            f'cease-to-pump level.</td></tr></table>'
        )
    else:
        ceased_html = ''

    next_month_html = (
        f'<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"'
        f' style="border-collapse:collapse;margin-top:12px;"><tr>'
        f'<td bgcolor="{nm_bg}" style="background-color:{nm_bg};border-left:4px solid {nm_border};'
        f'padding:10px 14px;font-family:Arial,sans-serif;font-size:12px;color:{nm_col};'
        f'border-radius:0 6px 6px 0;"><strong>Next Month Exposure</strong> - {nm_text}</td>'
        f'</tr></table>'
    )

    body += (
        _card_open('Water Cost &amp; Savings')
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    {_stat_cell(50, _ROW_A, f'Month Saving ({month_label})',
        f'<span style="color:#00762A;">{_fmt_dollar(saving_month)}</span>',
        f'{total_mld_m:.1f} ML pumped est. (licence max {total_ml_m:.1f} ML)', accent='#00762A')}
    {_stat_cell(50, _ROW_B, f'Season Saving (Sep {season_start.year})',
        f'<span style="color:#00762A;">{_fmt_dollar(saving_season)}</span>',
        f'{total_mld_s:.1f} ML pumped season est. (licence max {total_ml_s:.1f} ML)', accent='#00762A')}
  </tr>
</table>"""
        + _section_label(f'Zone Breakdown - {month_label} (Active Irrigation Days Only)')
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    <th bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:7px 10px;
        font-family:Arial,sans-serif;font-size:11px;color:#fff;font-weight:700;
        border-right:1px solid {_BORDER};text-align:left;">Zone</th>
    <th bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:7px 10px;
        font-family:Arial,sans-serif;font-size:11px;color:#fff;font-weight:700;
        border-right:1px solid {_BORDER};text-align:right;">Irrig. Days</th>
    <th bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:7px 10px;
        font-family:Arial,sans-serif;font-size:11px;color:#fff;font-weight:700;
        border-right:1px solid {_BORDER};text-align:right;">ML Pumped*</th>
    <th bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:7px 10px;
        font-family:Arial,sans-serif;font-size:11px;color:#fff;font-weight:700;
        text-align:right;">Pump Rate</th>
  </tr>
{level_rows_m if level_rows_m else f'<tr><td colspan="4" bgcolor="{_ROW_A}" style="background:{_ROW_A};padding:10px 12px;font-family:Arial,sans-serif;font-size:12px;color:#64748b;">No active irrigation days in {month_label}</td></tr>'}
  <tr>
    <td bgcolor="#e2e8f0" colspan="2" style="background-color:#e2e8f0;padding:7px 10px;
        font-family:Arial,sans-serif;font-size:12px;font-weight:700;color:#1b2631;
        border-top:2px solid {_BORDER};">Total</td>
    <td bgcolor="#e2e8f0" style="background-color:#e2e8f0;padding:7px 10px;
        font-family:Arial,sans-serif;font-size:12px;font-weight:700;color:#1b2631;
        border-top:2px solid {_BORDER};text-align:right;">{total_mld_m:.2f}&nbsp;ML
        <span style="font-weight:400;color:#64748b;">(max&nbsp;{total_ml_m:.2f})</span></td>
    <td bgcolor="#e2e8f0" style="background-color:#e2e8f0;padding:7px 10px;
        font-family:Arial,sans-serif;font-size:12px;color:#475569;
        border-top:2px solid {_BORDER};text-align:right;">
        @ ${cost_per_kl:.2f}/kL town water</td>
  </tr>
</table>
{ceased_html}
{irrig_note}
{next_month_html}"""
        + _card_close()
    )

    # 5. Rainfall card
    avg_str = f'{rain_avg:.1f}&nbsp;mm' if rain_avg is not None else '-'
    if rain_vs_avg is not None:
        vs_col = '#00762A' if rain_vs_avg >= 0 else '#EB1E23'
        vs_str = f'<span style="color:{vs_col};font-weight:700;">{_signed(rain_vs_avg, ".1f", "mm")}</span> vs 10-yr avg'
    else:
        vs_str = '-'

    body += (
        _card_open('Rainfall &amp; Evapotranspiration')
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    {_stat_cell(33, _ROW_A, f'{month_label} Rainfall',
        f'{rain_month:.1f}&nbsp;mm',
        f'{rain_days_m} rain day{"s" if rain_days_m != 1 else ""}')}
    {_stat_cell(34, _ROW_B, '10-Year Monthly Avg',
        avg_str,
        f'{month_label[:3]} long-term average')}
    {_stat_cell(33, _ROW_A, 'vs Average',
        vs_str,
        'surplus green, deficit red')}
  </tr>
</table>
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:0;">
  <tr>
    {_stat_cell(50, _ROW_B, f'Season Rainfall (Sep {season_start.year})',
        f'{rain_season:.1f}&nbsp;mm',
        f'through end of {month_label}')}
    {_stat_cell(50, _ROW_A, f'{month_label} Evapotranspiration',
        f'{et_month:.1f}&nbsp;mm',
        'from weather station')}
  </tr>
</table>"""
        + _card_close()
    )

    # 6. Outlook card (always show - board needs forward view)
    if days_to_cease is not None and days_to_cease <= 0:
        days_disp = '<span style="color:#EB1E23;font-weight:700;">Already at cease level</span>'
        cease_col = '#EB1E23'
    elif days_to_cease is not None:
        cease_col = '#00762A' if days_to_cease > 90 else ('#F58E1E' if days_to_cease > 30 else '#EB1E23')
        days_disp = f'<span style="color:{cease_col};">~{days_to_cease}</span>'
    else:
        days_disp = '-'
        cease_col = '#64748b'

    if days_nxt is None:
        nxt_disp = 'At cease level'
    elif days_nxt == float('inf'):
        nxt_disp = 'Lake rising'
    else:
        nxt_zone_name = nxt_zone['name'] if nxt_zone else 'next level'
        nxt_disp = f'~{int(days_nxt)} days to {nxt_zone_name}'

    body += (
        _card_open('Outlook - No Future Rainfall Assumed')
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    {_stat_cell(25, _ROW_A, 'Current Level',
        f'<span style="background:{lv_bg};color:{lv_txt};padding:2px 8px;'
        f'border-radius:4px;font-size:14px;">Level {lv_num}</span>',
        lv_name, accent=lv_bg)}
    {_stat_cell(25, _ROW_B, 'Days to Next Zone',
        nxt_disp,
        'conservative - no rain')}
    {_stat_cell(25, _ROW_A, 'Days to Cease',
        days_disp,
        cease_date.strftime('%d %b %Y') if cease_date else 'N/A')}
    {_stat_cell(25, _ROW_B, 'Cost if Ceased',
        _fmt_dollar(cost_to_march),
        'town water to end Mar', accent='#F58E1E' if cost_to_march else None)}
  </tr>
</table>"""
        + _card_close()
    )

    # 7. Zone change log
    if zone_events:
        event_rows = ''
        for i, row in enumerate(zone_events):
            rbg = _ROW_A if i % 2 == 0 else _ROW_B
            td = (f'font-family:Arial,sans-serif;font-size:12px;padding:7px 10px;'
                  f'border-bottom:1px solid {_BORDER};background-color:{rbg};')
            try:
                ts_fmt = datetime.strptime(row['timestamp_aest'], '%Y-%m-%d %H:%M:%S').strftime('%d %b %H:%M')
            except Exception:
                ts_fmt = row.get('timestamp_aest', '')
            event_label = {
                'zone_change':    'Zone change',
                'cease_pumping':  'CEASE pumping',
                'resume_pumping': 'Resume pumping',
                'email_failed':   'Alert (failed)',
                'backfilled':     'Derived from sensor',
            }.get(row.get('event', ''), row.get('event', ''))
            old_p = _zone_pill_html(int(row['old_zone']), zone_cfg) if row.get('old_zone') else '-'
            new_p = _zone_pill_html(int(row['new_zone']), zone_cfg) if row.get('new_zone') else '-'
            ahd_s = f"{float(row['ahd']):.3f} m" if row.get('ahd') else '-'
            new_rate = row.get('new_rate', '-') or '-'
            event_rows += f"""
  <tr>
    <td style="{td}">{ts_fmt}</td>
    <td style="{td}">{event_label}</td>
    <td style="{td}">{old_p}</td>
    <td style="{td}">{new_p}</td>
    <td style="{td};text-align:right;">{new_rate}</td>
    <td style="{td};text-align:right;">{ahd_s}</td>
  </tr>"""

        body += (
            _card_open(f'Zone Change Log - {month_label}')
            + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    <th bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:7px 10px;
        font-family:Arial,sans-serif;font-size:11px;color:#fff;font-weight:700;
        border-right:1px solid {_BORDER};text-align:left;">Date/Time</th>
    <th bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:7px 10px;
        font-family:Arial,sans-serif;font-size:11px;color:#fff;font-weight:700;
        border-right:1px solid {_BORDER};text-align:left;">Event</th>
    <th bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:7px 10px;
        font-family:Arial,sans-serif;font-size:11px;color:#fff;font-weight:700;
        border-right:1px solid {_BORDER};text-align:left;">From</th>
    <th bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:7px 10px;
        font-family:Arial,sans-serif;font-size:11px;color:#fff;font-weight:700;
        border-right:1px solid {_BORDER};text-align:left;">To</th>
    <th bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:7px 10px;
        font-family:Arial,sans-serif;font-size:11px;color:#fff;font-weight:700;
        border-right:1px solid {_BORDER};text-align:right;">New Rate</th>
    <th bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:7px 10px;
        font-family:Arial,sans-serif;font-size:11px;color:#fff;font-weight:700;
        text-align:right;">AHD</th>
  </tr>
{event_rows}
</table>"""
            + _card_close()
        )
    else:
        body += (
            _card_open(f'Zone Change Log - {month_label}')
            + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    <td bgcolor="{_ROW_A}" style="background-color:{_ROW_A};padding:14px 16px;
        font-family:Arial,sans-serif;font-size:13px;color:#475569;">
      No zone changes during {month_label}. Pumping continued at <strong>Level {lv_num}
      - {lv_name}</strong> throughout the month.
    </td>
  </tr>
</table>"""
            + _card_close()
        )

    # 8. Footer
    body += f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:24px;">
  <tr>
    <td style="padding:16px 24px;border-top:1px solid #cbd5e1;">
      <p style="margin:0;font-family:Arial,sans-serif;font-size:10px;color:#94a3b8;
          text-align:center;">
        Wagga Wagga Country Club &nbsp;&bull;&nbsp; Monthly Board Water &amp; Finance Report
        &nbsp;&bull;&nbsp; {month_label}<br>
        Savings are estimates based on town water volume charge of ${cost_per_kl:.2f}/kL.
        ML figures estimated from licence zone rate x active irrigation days.
        Flow sensor installation pending.
      </p>
    </td>
  </tr>
</table>"""

    return _wrap(body)


# ── Send ───────────────────────────────────────────────────────────────────────

def send_email(subject, html_content, test_mode=False):
    if not SENDGRID_API_KEY:
        log.error('SENDGRID_API_KEY not set')
        return False
    if not EMAIL_FROM:
        log.error('EMAIL_FROM not set')
        return False

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, To, Cc, Bcc, Email
    except ImportError:
        log.error('sendgrid package not installed')
        return False

    if test_mode:
        to_list  = ['andrew@bidgeepumps.com.au']
        cc_list  = []
        bcc_list = []
        subject  = f'[TEST] {subject}'
        log.info(f'TEST MODE - sending only to {to_list[0]}')
    else:
        to_list  = [e for e in lu.recipients_for('board_to',  EMAIL_BOARD_TO)  if '@' in e]
        cc_list  = [e for e in lu.recipients_for('board_cc',  EMAIL_BOARD_CC)  if '@' in e]
        bcc_list = [e for e in lu.recipients_for('board_bcc', EMAIL_BOARD_BCC) if '@' in e]

    if not to_list:
        log.error('No To recipients - cannot send')
        return False

    mail = Mail(from_email=Email(EMAIL_FROM), subject=subject,
                plain_text_content=lu.html_to_text(html_content), html_content=html_content)
    mail.to = [To(e) for e in to_list]
    if cc_list:
        mail.cc = [Cc(e) for e in cc_list]
    if bcc_list:
        mail.bcc = [Bcc(e) for e in bcc_list]

    try:
        resp = SendGridAPIClient(SENDGRID_API_KEY).send(mail)
        log.info(f'Sent "{subject}" - status {resp.status_code} - to: {", ".join(to_list)}')
        lu.log_email('board_report', subject, to_list + cc_list + bcc_list, f'sent ({resp.status_code})')
        return True
    except Exception as e:
        log.error(f'SendGrid error: {e}')
        try:
            log.error(f'SendGrid response body: {e.body}')
        except AttributeError:
            pass
        return False


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Save HTML preview, do not send')
    parser.add_argument('--test',    action='store_true', help='Send to andrew@bidgeepumps.com.au only')
    parser.add_argument('--force',   action='store_true', help='Ignore dedup guard and send regardless')
    parser.add_argument('--monthly', action='store_true', help='Send monthly board finance report instead of weekly')
    args = parser.parse_args()

    now_syd = datetime.now(SYDNEY_TZ)

    if args.monthly:
        # ── Monthly report ────────────────────────────────────────────────────
        if not args.dry_run and not args.test and not args.force:
            if _already_sent_this_month(now_syd):
                log.info('Monthly board report already sent this month - skipping (use --force to override)')
                return

        prev_month_end = now_syd.date().replace(day=1) - __import__('datetime').timedelta(days=1)
        subject = f'Monthly Board Water & Finance Report - {prev_month_end.strftime("%B %Y")}'
        html    = build_monthly_html(now_syd)

        if html is None:
            log.error('Could not build monthly report - missing data')
            return

        if args.dry_run:
            out = (_DATA_DIR / 'reports'
                   / now_syd.strftime('%Y') / now_syd.strftime('%m')
                   / f'board_monthly_preview_{now_syd.strftime("%Y-%m-%d")}.html')
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html, encoding='utf-8')
            log.info(f'Monthly dry run - saved to {out}')
            return

        sent = send_email(subject, html, test_mode=args.test)
        if sent and not args.test:
            _mark_sent_this_month(now_syd)
            log.info('Monthly sent-this-month guard updated')
        return

    # ── Weekly report ─────────────────────────────────────────────────────────
    if not args.dry_run and not args.test and not args.force:
        if _already_sent_this_week(now_syd):
            log.info('Board email already sent this week - skipping (use --force to override)')
            return

    subject   = f'Weekly Board Weather and Water Update - {now_syd.day} {now_syd.strftime("%B %Y")}'
    html, proj = build_html(now_syd)

    if html is None:
        log.error('Could not build email - no lake data')
        return

    if args.dry_run:
        out = (_DATA_DIR / 'reports'
               / now_syd.strftime('%Y') / now_syd.strftime('%m')
               / f'board_preview_{now_syd.strftime("%Y-%m-%d")}.html')
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding='utf-8')
        log.info(f'Dry run - saved to {out}')
        return

    sent = send_email(subject, html, test_mode=args.test)

    if sent and not args.test:
        _mark_sent_this_week(now_syd, proj)
        log.info('Sent-this-week guard updated')


if __name__ == '__main__':
    main()
