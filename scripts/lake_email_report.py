#!/usr/bin/env python3
"""
Lake Albert Email Reports
=========================
Sends daily / weekly / monthly / yearly lake condition reports
to council and club stakeholders via SendGrid.

Schedule (Sydney time):
  Daily   — every morning
  Weekly  — Monday only
  Monthly — 1st of each month
  Yearly  — 1 July (end of financial year)

Usage:
    python scripts/lake_email_report.py                    # auto (date-based)
    python scripts/lake_email_report.py --report=weekly    # specific type
    python scripts/lake_email_report.py --force-all        # all four types
    python scripts/lake_email_report.py --dry-run          # preview HTML, no send

Secrets required:
    SENDGRID_API_KEY   — SendGrid API key
    EMAIL_FROM         — sender address
    EMAIL_LAKE_TO      — comma-separated To addresses
    EMAIL_LAKE_CC      — comma-separated CC addresses (optional)
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
LAKE_BOTTOM = 189.362   # m AHD — lake bed elevation

SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
EMAIL_FROM       = os.environ.get('EMAIL_FROM', '')
EMAIL_LAKE_TO    = os.environ.get('EMAIL_LAKE_TO', '')
EMAIL_LAKE_CC    = os.environ.get('EMAIL_LAKE_CC', '')

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
    }

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_ahd(ahd):
    if ahd is None: return '&mdash;'
    return f'{ahd:.3f}&nbsp;m&nbsp;AHD'

def fmt_depth(ahd):
    if ahd is None: return '&mdash;'
    return f'{ahd - LAKE_BOTTOM:.2f}&nbsp;m'

def activity_status(ahd):
    if ahd is None: return ('Unknown', '#888888', '#ffffff')
    depth = ahd - LAKE_BOTTOM
    if depth >= 0.70: return ('Powerboating OK',     '#1e8449', '#ffffff')
    if depth >= 0.50: return ('Small Vessels Only',  '#e67e22', '#ffffff')
    return                    ('No Boating',          '#c0392b', '#ffffff')

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

def fmt_date(d):
    """Format a date with no leading zero on day (Linux strftime %-d)."""
    return d.strftime('%-d %b %Y')

def fmt_month(d):
    return d.strftime('%B %Y')

# ── HTML building blocks ──────────────────────────────────────────────────────

# Colour palette
HDR_BG   = '#1a5276'   # deep navy — header bar
SEC_BG   = '#2471a3'   # mid blue — section headers
ROW_A    = '#d6eaf8'   # light blue — alternating row
ROW_B    = '#eaf4fb'   # very light blue — alternating row
BODY_BG  = '#f0f4f8'   # page background
BORDER   = '#a9cce3'   # table border colour

def _pill(label, bg, fg='#ffffff'):
    return (f'<span style="display:inline-block;background-color:{bg};color:{fg};'
            f'padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700;'
            f'font-family:Arial,sans-serif;">{label}</span>')

def _header(title, subtitle=''):
    sub = (f'<p style="margin:6px 0 0 0;font-size:13px;color:#a9cce3;'
           f'font-family:Arial,sans-serif;">{subtitle}</p>') if subtitle else ''
    return f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <!--[if mso]><tr><td bgcolor="#ffffff" style="background-color:#ffffff;padding:10px 20px;text-align:right;"><img src="https://wwcc.com.au/cms/wp-content/themes/contemporary/assets/images/logo.png" height="44" style="display:block;height:44px;width:auto;margin-left:auto;" alt="Wagga Wagga Country Club"></td></tr><![endif]-->
  <tr>
    <td bgcolor="{HDR_BG}" style="background-color:{HDR_BG};padding:22px 20px 16px 20px;">
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td valign="top">
            <p style="margin:0;font-size:10px;color:#a9cce3;letter-spacing:1.5px;
                text-transform:uppercase;font-family:Arial,sans-serif;">
              WAGGA WAGGA CITY COUNCIL &nbsp;&bull;&nbsp; LAKE ALBERT
            </p>
            <h1 style="margin:8px 0 0 0;font-size:22px;color:#ffffff;font-weight:bold;
                font-family:Arial,sans-serif;">{title}</h1>
            {sub}
          </td>
          <!--[if !mso]><!-->
          <td align="right" valign="middle" style="padding-left:12px;">
            <img src="https://wwcc.com.au/cms/wp-content/themes/contemporary/assets/images/logo.png"
                 height="50" alt="Wagga Wagga Country Club"
                 style="display:block;filter:grayscale(1) contrast(200) invert(1);height:50px;width:auto;opacity:0.9;">
          </td>
          <!--<![endif]-->
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
        Generated {now_syd.strftime('%-d %b %Y %H:%M')} AEST &nbsp;&bull;&nbsp;
        Lake Albert, Wagga Wagga NSW &nbsp;&bull;&nbsp;
        Auto-generated &mdash; do not reply to this email.
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
</head>
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
    batt   = latest.get('lake_battery_v')
    try:
        lake_dt  = datetime.fromisoformat(
            latest['lake_date'].replace('Z', '+00:00')
        ).astimezone(SYDNEY_TZ)
        lake_ts  = lake_dt.strftime('%-d %b %Y %H:%M')
    except Exception:
        lake_ts = '&mdash;'

    act_lbl, act_bg, act_fg = activity_status(ahd)

    yday_wx  = next((r for r in reversed(data['history'])
                     if r['date'] == str(yesterday)), None)

    body  = _header('Lake Albert Daily Report', now_syd.strftime('%-d %B %Y'))
    body += _section('LAKE LEVEL')
    body += _kv_table([
        ('Lake Level (AHD)',    fmt_ahd(ahd)),
        ('Depth Above Bottom',  fmt_depth(ahd)),
        ('Activity Status',     _pill(act_lbl, act_bg, act_fg)),
        ('Reading Time',        lake_ts),
        ('Sensor Battery',      f'{batt:.2f}&nbsp;V' if batt else '&mdash;'),
    ])

    if yday_wx:
        body += _section(f"YESTERDAY'S WEATHER &mdash; {fmt_date(yesterday).upper()}")
        wx_rows = []
        if yday_wx.get('tMax')      is not None: wx_rows.append(('Max Temperature', f"{yday_wx['tMax']:.1f}&nbsp;&deg;C"))
        if yday_wx.get('tMin')      is not None: wx_rows.append(('Min Temperature', f"{yday_wx['tMin']:.1f}&nbsp;&deg;C"))
        if yday_wx.get('rain')      is not None: wx_rows.append(('Rainfall',        f"{yday_wx['rain']:.1f}&nbsp;mm"))
        if yday_wx.get('humidity')  is not None: wx_rows.append(('Humidity',        f"{yday_wx['humidity']:.0f}%"))
        if yday_wx.get('windMax')   is not None: wx_rows.append(('Max Wind Speed',  f"{yday_wx['windMax']:.1f}&nbsp;km/h"))
        if yday_wx.get('pressAvg')  is not None: wx_rows.append(('Avg Pressure',    f"{yday_wx['pressAvg']:.1f}&nbsp;hPa"))
        if wx_rows:
            body += _kv_table(wx_rows)

    body += _footer(now_syd)
    subject = f"Lake Albert Daily Report \u2014 {now_syd.strftime('%-d %b %Y')}"
    return _wrap(body), subject


# ── Report: Weekly ────────────────────────────────────────────────────────────

def build_weekly(data, now_syd):
    today      = now_syd.date()
    week_end   = today - timedelta(days=1)
    week_start = week_end - timedelta(days=6)

    readings = readings_in_range(data['readings'], week_start, week_end)
    wx_week  = history_in_range(data['history'], week_start, week_end)

    # One row per day — find closest-to-midday reading
    day_range = [week_start + timedelta(days=i) for i in range(7)]
    daily_levels = []
    for d in day_range:
        day_rdgs = [r for r in readings if r.get('_date') == d]
        if day_rdgs:
            best = min(day_rdgs, key=lambda r: abs(r['_dt'].hour * 60 + r['_dt'].minute - 720))
            daily_levels.append((d, best.get('ahd')))
        else:
            daily_levels.append((d, None))

    latest = data['latest'] or {}
    ahd    = latest.get('lake_ahd')
    act_lbl, act_bg, act_fg = activity_status(ahd)

    wx_tmax = max((r['tMax']    for r in wx_week if r.get('tMax')    is not None), default=None)
    wx_tmin = min((r['tMin']    for r in wx_week if r.get('tMin')    is not None), default=None)
    wx_rain = sum(r.get('rain', 0) or 0 for r in wx_week)
    wx_wmax = max((r['windMax'] for r in wx_week if r.get('windMax') is not None), default=None)

    period = f"{week_start.strftime('%-d %b')} &ndash; {fmt_date(week_end)}"
    body  = _header('Lake Albert Weekly Report', period)

    body += _section('CURRENT LAKE STATUS')
    body += _kv_table([
        ('Current Level (AHD)', fmt_ahd(ahd)),
        ('Depth Above Bottom',  fmt_depth(ahd)),
        ('Activity Status',     _pill(act_lbl, act_bg, act_fg)),
    ])

    body += _section('DAILY LAKE LEVELS &mdash; PAST 7 DAYS')
    level_rows = []
    for d, level_ahd in daily_levels:
        a_lbl, a_bg, _ = activity_status(level_ahd)
        level_rows.append((
            d.strftime('%a %-d %b'),
            fmt_ahd(level_ahd),
            fmt_depth(level_ahd),
            _pill(a_lbl, a_bg),
        ))
    body += _data_table(['Date', 'Level (AHD)', 'Depth', 'Activity'], level_rows)

    body += _section('WEEKLY WEATHER SUMMARY')
    wx_rows = []
    if wx_tmax is not None: wx_rows.append(('Peak Max Temperature',  f'{wx_tmax:.1f}&nbsp;&deg;C'))
    if wx_tmin is not None: wx_rows.append(('Lowest Min Temperature', f'{wx_tmin:.1f}&nbsp;&deg;C'))
    wx_rows.append(('Total Rainfall', f'{wx_rain:.1f}&nbsp;mm'))
    if wx_wmax is not None: wx_rows.append(('Peak Wind Speed', f'{wx_wmax:.1f}&nbsp;km/h'))
    body += _kv_table(wx_rows)

    body += _footer(now_syd)
    subject = (f"Lake Albert Weekly Report \u2014 "
               f"{week_start.strftime('%-d %b')}\u2013{fmt_date(week_end)}")
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

    body += _section('CURRENT LAKE STATUS')
    body += _kv_table([
        ('Current Level (AHD)', fmt_ahd(ahd)),
        ('Depth Above Bottom',  fmt_depth(ahd)),
        ('Activity Status',     _pill(act_lbl, act_bg, act_fg)),
    ])

    body += _section(f'LAKE LEVELS &mdash; {month_label.upper()}')
    body += _kv_table([
        ('Highest Level (AHD)', fmt_ahd(ahd_max)),
        ('Lowest Level (AHD)',  fmt_ahd(ahd_min)),
        ('Average Level (AHD)', fmt_ahd(ahd_avg)),
        ('Highest Depth',       fmt_depth(ahd_max)),
        ('Lowest Depth',        fmt_depth(ahd_min)),
        ('Readings Recorded',   str(len(ahd_vals)) if ahd_vals else '&mdash;'),
    ])

    body += _section(f'WEATHER SUMMARY &mdash; {month_label.upper()}')
    wx_rows = []
    if wx_tmax is not None: wx_rows.append(('Peak Max Temperature',  f'{wx_tmax:.1f}&nbsp;&deg;C'))
    if wx_tmin is not None: wx_rows.append(('Lowest Min Temperature', f'{wx_tmin:.1f}&nbsp;&deg;C'))
    wx_rows.append(('Total Rainfall',  f'{wx_rain:.1f}&nbsp;mm'))
    wx_rows.append(('Rain Days',       str(wx_rain_days)))
    if wx_wmax is not None: wx_rows.append(('Peak Wind Speed', f'{wx_wmax:.1f}&nbsp;km/h'))
    if wx_hot_days > 0:     wx_rows.append(('Days 35&deg;C+',  str(wx_hot_days)))
    body += _kv_table(wx_rows)

    body += _footer(now_syd)
    subject = f"Lake Albert Monthly Report \u2014 {month_label}"
    return _wrap(body), subject


# ── Report: Yearly ────────────────────────────────────────────────────────────

def build_yearly(data, now_syd):
    today      = now_syd.date()
    # Financial year: 1 Jul → 30 Jun
    # On 1 Jul, report covers the year that just ended (prev Jul 1 – yesterday Jun 30)
    year_end   = today - timedelta(days=1)                # 30 Jun
    year_start = year_end.replace(month=7, day=1)
    if year_start > year_end:
        year_start = year_start.replace(year=year_start.year - 1)

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

    fy_label  = f"FY{year_start.year}/{str(year_end.year)[2:]}"
    period    = f"{fmt_date(year_start)} &ndash; {fmt_date(year_end)}"

    body  = _header(f'Lake Albert Annual Report &mdash; {fy_label}', period)

    body += _section('CURRENT LAKE STATUS')
    body += _kv_table([
        ('Current Level (AHD)', fmt_ahd(ahd)),
        ('Depth Above Bottom',  fmt_depth(ahd)),
        ('Activity Status',     _pill(act_lbl, act_bg, act_fg)),
    ])

    body += _section(f'LAKE LEVEL SUMMARY &mdash; {fy_label}')
    body += _kv_table([
        ('Highest Level (AHD)', f"{fmt_ahd(ahd_max)}{(' &mdash; ' + peak_date) if peak_date else ''}"),
        ('Lowest Level (AHD)',  f"{fmt_ahd(ahd_min)}{(' &mdash; ' + low_date)  if low_date  else ''}"),
        ('Average Level (AHD)', fmt_ahd(ahd_avg)),
        ('Highest Depth',       fmt_depth(ahd_max)),
        ('Lowest Depth',        fmt_depth(ahd_min)),
        ('Total Readings',      str(len(ahd_vals)) if ahd_vals else '&mdash;'),
    ])

    body += _section(f'WEATHER SUMMARY &mdash; {fy_label}')
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

    body += _footer(now_syd)
    subject = f"Lake Albert Annual Report \u2014 {fy_label}"
    return _wrap(body), subject


# ── Email sending ─────────────────────────────────────────────────────────────

def send_email(subject, html_content):
    if not SENDGRID_API_KEY:
        log.error('SENDGRID_API_KEY not set — cannot send')
        return False
    if not EMAIL_FROM:
        log.error('EMAIL_FROM not set — cannot send')
        return False
    if not EMAIL_LAKE_TO:
        log.error('EMAIL_LAKE_TO not set — cannot send')
        return False

    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, To, Cc, Email
    except ImportError:
        log.error('sendgrid package not installed (pip install sendgrid)')
        return False

    to_list = [e.strip() for e in EMAIL_LAKE_TO.split(',') if e.strip()]
    cc_list = [e.strip() for e in EMAIL_LAKE_CC.split(',') if e.strip()] if EMAIL_LAKE_CC else []

    mail = Mail(
        from_email=Email(EMAIL_FROM),
        subject=subject,
        html_content=html_content,
    )
    mail.to = [To(e) for e in to_list]
    if cc_list:
        mail.cc = [Cc(e) for e in cc_list]

    try:
        sg   = SendGridAPIClient(SENDGRID_API_KEY)
        resp = sg.send(mail)
        log.info(f'Sent "{subject}" — status {resp.status_code} — to: {", ".join(to_list)}')
        if cc_list:
            log.info(f'  CC: {", ".join(cc_list)}')
        return True
    except Exception as e:
        log.error(f'SendGrid error sending "{subject}": {e}')
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
    args = parser.parse_args()

    now_syd = datetime.now(tz=SYDNEY_TZ)
    today   = now_syd.date()
    log.info(f'Sydney date/time: {now_syd.strftime("%Y-%m-%d %H:%M %Z")}')

    data = load_data()

    # Determine which reports to run
    if args.force_all:
        to_send = list(BUILDERS)
    elif args.report:
        to_send = [args.report]
    else:
        to_send = ['daily']
        if today.weekday() == 0:                     to_send.append('weekly')
        if today.day == 1:                           to_send.append('monthly')
        if today.month == 7 and today.day == 1:      to_send.append('yearly')

    log.info(f'Reports scheduled: {to_send}')

    for report_type in to_send:
        html, subject = BUILDERS[report_type](data, now_syd)

        if args.dry_run:
            preview_dir = DATA_DIR / 'reports'
            preview_dir.mkdir(exist_ok=True)
            out = preview_dir / f'lake_{report_type}_preview.html'
            out.write_text(html, encoding='utf-8')
            log.info(f'[dry-run] {report_type}: saved preview to {out}')
        else:
            send_email(subject, html)

if __name__ == '__main__':
    main()
