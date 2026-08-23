#!/usr/bin/env python3
"""
Pump Station Daily Summary Email
=================================
Sends a daily summary email at 7am AEST covering the previous day's
pump performance: pressure range, pump run hours, energy, flow, and alarms.

Scheduled via pump-daily-email.yml at 21:00 UTC (= 7:00am AEST, UTC+10).

Required env vars:
  RESEND_API_KEY
  EMAIL_FROM
  PUMP_EMAIL_RECIPIENTS  (comma-separated addresses)
"""
import json, logging, os, re, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))
from lake_utils import log_email

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

SYDNEY_TZ = ZoneInfo('Australia/Sydney')
DATA_DIR  = Path(__file__).parent.parent / 'data'

LATEST_FILE = DATA_DIR / 'pump_station_latest.json'
DAILY_FILE  = DATA_DIR / 'pump_station_daily.json'
ALARM_FILE  = DATA_DIR / 'pump_alarm_history.json'
EMCFG_FILE  = DATA_DIR / 'email_config.json'

PUMP_PAGE_URL  = 'https://bidgee182.github.io/wwcc-weather-page/pump-station.html'
LOGO_URL       = 'https://bidgee182.github.io/wwcc-weather-page/assets/images/logo-white.png'
PUMP_LABELS    = ['P1', 'P2', 'P3', 'Jockey']
TEST_RECIPIENT = 'andrew@bidgeepumps.com.au'

RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
EMAIL_FROM       = os.environ.get('EMAIL_FROM', '')
_TO_RAW          = os.environ.get('PUMP_EMAIL_RECIPIENTS', '')
_CC_RAW          = os.environ.get('PUMP_EMAIL_CC', '')
_BCC_RAW         = os.environ.get('PUMP_EMAIL_BCC', '')
TEST_SEND        = os.environ.get('TEST_SEND', 'false').lower() == 'true'
FORCE_SEND       = os.environ.get('FORCE_SEND', 'false').lower() == 'true'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _addr(raw):
    return [r.strip() for r in raw.split(',') if r.strip()]


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def html_to_text(html):
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def fmt_bar(v):
    return f'{v:.2f} bar' if v is not None else '-'


def fmt_m3h(v):
    return f'{v:.1f} m³/h' if v else '-'


def fmt_m3(v):
    return f'{v:.1f} m³' if v else '-'


def fmt_hrs(v):
    return f'{v:.1f} h' if v else '-'


def fmt_kwh(v):
    return f'{v:.2f} kWh' if v else '-'


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _logo():
    return (f'<img src="{LOGO_URL}" width="194" height="44"'
            f' alt="Wagga Wagga Country Club" style="display:block;border:0;">')


def _section_heading(label):
    return (f'<tr><td style="font-family:Arial,sans-serif;font-size:12px;'
            f'font-weight:bold;color:#888;letter-spacing:1.5px;'
            f'text-transform:uppercase;padding:20px 32px 8px;">{label}</td></tr>')


def _key_value_table(rows):
    """rows: list of (label, value) - alternating shading applied automatically"""
    cells = ''
    for i, (label, value) in enumerate(rows):
        bg = 'background:#f9f9f9;' if i % 2 == 0 else ''
        cells += (f'<tr style="{bg}">'
                  f'<td style="padding:8px 16px;color:#555;width:45%;'
                  f'font-family:Arial,sans-serif;font-size:14px;">{label}</td>'
                  f'<td style="padding:8px 16px;font-family:Arial,sans-serif;'
                  f'font-size:14px;">{value}</td></tr>')
    return (f'<table width="100%" cellpadding="0" cellspacing="0"'
            f' style="border:1px solid #eee;border-radius:4px;">{cells}</table>')


def build_html(day_data, latest, period_label, alarms_24h):
    date_label = period_label

    p_min    = day_data.get('p_min')
    p_max    = day_data.get('p_max')
    p_avg    = day_data.get('p_avg')
    fl_total = day_data.get('fl_total', 0.0)
    fl_max   = day_data.get('fl_max', 0.0)
    hrs      = day_data.get('hrs', [0, 0, 0, 0])
    kwh      = day_data.get('kwh', [0, 0, 0, 0])

    # Current status
    sys_data   = latest.get('system', {})
    connected  = latest.get('connected', False)
    pressure   = sys_data.get('pressure_actual_bar')
    mode       = sys_data.get('mode', 'unknown').title()
    fault      = sys_data.get('fault', False)
    conn_color = '#27ae60' if connected else '#c0392b'
    conn_label = 'ONLINE' if connected else 'OFFLINE'
    fault_note = ' - FAULT ACTIVE' if fault else ''

    # Pump run table rows
    pump_rows = ''
    any_pump  = False
    for i, label in enumerate(PUMP_LABELS):
        h = hrs[i] if i < len(hrs) else 0
        k = kwh[i] if i < len(kwh) else 0
        if h or k:
            any_pump = True
            bg = 'background:#f9f9f9;' if i % 2 == 0 else ''
            pump_rows += (f'<tr style="{bg}">'
                          f'<td style="padding:8px 16px;font-family:Arial,sans-serif;'
                          f'font-size:14px;">{label}</td>'
                          f'<td style="padding:8px 16px;font-family:Arial,sans-serif;'
                          f'font-size:14px;">{fmt_hrs(h)}</td>'
                          f'<td style="padding:8px 16px;font-family:Arial,sans-serif;'
                          f'font-size:14px;">{fmt_kwh(k)}</td></tr>')
    if not any_pump:
        pump_rows = ('<tr><td colspan="3" style="padding:10px 16px;'
                     'font-family:Arial,sans-serif;font-size:14px;color:#999;">'
                     'No pump runs recorded</td></tr>')

    # Alarm table rows
    alarm_rows = ''
    for a in alarms_24h[:10]:
        ts = a.get('timestamp_iso', '')
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(SYDNEY_TZ)
            ts_str = dt.strftime('%-I:%M %p')
        except Exception:
            ts_str = ts[:16]
        t_code = a.get('type_code', 3)
        color  = '#c0392b' if t_code == 1 else '#e67e22'
        name   = a.get('type_name', 'Event')
        src    = a.get('pump_label', 'System')
        i      = alarms_24h.index(a)
        bg     = 'background:#f9f9f9;' if i % 2 == 0 else ''
        alarm_rows += (f'<tr style="{bg}">'
                       f'<td style="padding:7px 16px;font-family:Arial,sans-serif;'
                       f'font-size:13px;color:#666;">{ts_str}</td>'
                       f'<td style="padding:7px 16px;font-family:Arial,sans-serif;'
                       f'font-size:13px;color:{color};">{name}</td>'
                       f'<td style="padding:7px 16px;font-family:Arial,sans-serif;'
                       f'font-size:13px;">{src}</td></tr>')
    if not alarm_rows:
        alarm_rows = ('<tr><td colspan="3" style="padding:10px 16px;'
                      'font-family:Arial,sans-serif;font-size:14px;color:#999;">'
                      'No alarms or events</td></tr>')

    TH = ('padding:9px 16px;font-family:Arial,sans-serif;font-size:13px;'
          'font-weight:bold;color:#fff;background:#003366;')

    return f'''<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f0f0f0;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center" style="padding:24px 0;">
<table width="640" cellpadding="0" cellspacing="0"
       style="background:#ffffff;border-radius:6px;overflow:hidden;">
<tr><td>

<!-- Header -->
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#003366;padding:28px 32px;">
<tr><td>{_logo()}</td></tr>
<tr><td style="padding-top:14px;font-family:Arial,sans-serif;font-size:20px;
               font-weight:bold;color:#ffffff;">Pump Station Weekly Summary</td></tr>
<tr><td style="padding-top:4px;font-family:Arial,sans-serif;font-size:14px;
               color:#99bbdd;">{date_label}</td></tr>
</table>

<!-- Current Status -->
<table width="100%" cellpadding="0" cellspacing="0">
{_section_heading('Current Status')}
<tr><td style="padding:0 32px 4px;">
{_key_value_table([
    ('Connection', f'<span style="color:{conn_color};font-weight:bold;">{conn_label}{fault_note}</span>'),
    ('Live pressure', fmt_bar(pressure)),
    ('Mode', mode),
])}
</td></tr>
</table>

<!-- Pressure -->
<table width="100%" cellpadding="0" cellspacing="0">
{_section_heading("This Week's System Pressure")}
<tr><td style="padding:0 32px 4px;">
{_key_value_table([
    ('Minimum', fmt_bar(p_min)),
    ('Maximum', fmt_bar(p_max)),
    ('Average', fmt_bar(p_avg)),
    ('Peak flow', fmt_m3h(fl_max)),
    ('Total volume', fmt_m3(fl_total)),
])}
</td></tr>
</table>

<!-- Pump Runs -->
<table width="100%" cellpadding="0" cellspacing="0">
{_section_heading("This Week's Pump Run Hours")}
<tr><td style="padding:0 32px 4px;">
<table width="100%" cellpadding="0" cellspacing="0"
       style="border:1px solid #eee;border-radius:4px;">
<tr>
<td style="{TH}">Pump</td>
<td style="{TH}">Run Hours</td>
<td style="{TH}">Energy</td>
</tr>
{pump_rows}
</table>
</td></tr>
</table>

<!-- Alarms -->
<table width="100%" cellpadding="0" cellspacing="0">
{_section_heading("This Week's Alarms and Events")}
<tr><td style="padding:0 32px 4px;">
<table width="100%" cellpadding="0" cellspacing="0"
       style="border:1px solid #eee;border-radius:4px;">
<tr>
<td style="{TH}">Time</td>
<td style="{TH}">Event</td>
<td style="{TH}">Source</td>
</tr>
{alarm_rows}
</table>
</td></tr>
</table>

<!-- Footer -->
<table width="100%" cellpadding="0" cellspacing="0"
       style="padding:20px 32px 28px;background:#f4f4f4;margin-top:20px;">
<tr><td style="font-family:Arial,sans-serif;font-size:12px;color:#888;text-align:center;">
<a href="{PUMP_PAGE_URL}" style="color:#1a73e8;">View Pump Station Dashboard</a>
</td></tr></table>

</td></tr></table>
</td></tr></table>
</body></html>'''


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def daily_enabled():
    try:
        cfg = json.loads(EMCFG_FILE.read_text())
        return cfg.get('email_enabled', {}).get('pump_daily', True)
    except Exception:
        return True


def send_email(subject, html):
    if TEST_SEND:
        to_list, cc_list, bcc_list = [TEST_RECIPIENT], [], []
        subject = '[TEST] ' + subject
    else:
        to_list  = _addr(_TO_RAW)
        cc_list  = _addr(_CC_RAW)
        bcc_list = _addr(_BCC_RAW)

    everyone = to_list + cc_list + bcc_list
    if not to_list:
        log.warning('No recipients - skipping.')
        return False
    from mailer import send_html
    ok, detail = send_html(subject, html, to_list, cc_list, bcc_list,
                           stream='pump', text=html_to_text(html))
    if ok:
        log.info(f'Sent "{subject}" - {detail} - to {to_list} cc {cc_list} bcc {bcc_list}')
    else:
        log.error(f'Send error: {detail}')
    log_email('pump_report', subject, everyone, detail)
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def aggregate_week(days, start_date, end_date):
    """Combine the daily rollups whose date falls in [start_date, end_date]
    into one day_data-shaped dict. Pressure min/max/avg are min/max/mean of the
    daily figures; flow volume and pump hours/energy are summed; peak flow is the
    week's max. Returns (agg_dict, day_count)."""
    s, e = start_date.isoformat(), end_date.isoformat()
    in_range = [d for d in days if s <= (d.get('date') or '') <= e]
    if not in_range:
        return {}, 0

    p_mins = [d['p_min'] for d in in_range if d.get('p_min') is not None]
    p_maxs = [d['p_max'] for d in in_range if d.get('p_max') is not None]
    p_avgs = [d['p_avg'] for d in in_range if d.get('p_avg') is not None]
    fl_max = [(d.get('fl_max') or 0.0) for d in in_range]

    hrs, kwh = [0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]
    for d in in_range:
        dh, dk = d.get('hrs', []), d.get('kwh', [])
        for i in range(4):
            hrs[i] += (dh[i] if i < len(dh) and dh[i] else 0)
            kwh[i] += (dk[i] if i < len(dk) and dk[i] else 0)

    agg = {
        'p_min':    min(p_mins) if p_mins else None,
        'p_max':    max(p_maxs) if p_maxs else None,
        'p_avg':    round(sum(p_avgs) / len(p_avgs), 2) if p_avgs else None,
        'fl_total': sum((d.get('fl_total') or 0.0) for d in in_range),
        'fl_max':   max(fl_max) if fl_max else 0.0,
        'hrs':      hrs,
        'kwh':      kwh,
    }
    return agg, len(in_range)


def main():
    if not FORCE_SEND and not TEST_SEND and not daily_enabled():
        log.info('pump_daily disabled in email_config.json - skipping.')
        return

    now_syd    = datetime.now(tz=SYDNEY_TZ)
    week_end   = (now_syd - timedelta(days=1)).date()    # yesterday (Sunday)
    week_start = week_end - timedelta(days=6)            # previous Monday

    # Weekly stats aggregated from the daily rollups
    daily_data      = load_json(DAILY_FILE, {})
    days            = daily_data.get('days', [])
    day_data, ndays = aggregate_week(days, week_start, week_end)
    if not day_data:
        log.warning(f'No daily data for {week_start}..{week_end} - using empty defaults.')
        day_data = {}

    latest      = load_json(LATEST_FILE, {})
    alarms_data = load_json(ALARM_FILE, {})
    all_events  = alarms_data.get('events', [])

    # Alarms across the week (AEST)
    w_start = datetime(week_start.year, week_start.month, week_start.day, tzinfo=SYDNEY_TZ)
    w_end   = datetime(week_end.year, week_end.month, week_end.day, tzinfo=SYDNEY_TZ) + timedelta(days=1)
    alarms_week = []
    for e in all_events:
        try:
            ts = datetime.fromisoformat(
                e['timestamp_iso'].replace('Z', '+00:00')).astimezone(SYDNEY_TZ)
            if w_start <= ts < w_end:
                alarms_week.append(e)
        except Exception:
            pass
    alarms_week.sort(key=lambda e: e.get('timestamp_iso', ''))

    if week_start.month == week_end.month:
        period_label = f"{week_start.strftime('%-d')} - {week_end.strftime('%-d %B %Y')}"
    else:
        period_label = f"{week_start.strftime('%-d %b')} - {week_end.strftime('%-d %b %Y')}"
    subject = f'Pump Station Weekly Summary - {period_label}'
    html    = build_html(day_data, latest, period_label, alarms_week)
    send_email(subject, html)


if __name__ == '__main__':
    main()
