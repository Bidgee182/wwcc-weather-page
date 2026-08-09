#!/usr/bin/env python3
"""
Pump Station Daily Summary Email
=================================
Sends a daily summary email at 7am AEST covering the previous day's
pump performance: pressure range, pump run hours, energy, flow, and alarms.

Scheduled via pump-daily-email.yml at 21:00 UTC (= 7:00am AEST, UTC+10).

Required env vars:
  SENDGRID_API_KEY
  EMAIL_FROM
  PUMP_EMAIL_RECIPIENTS  (comma-separated addresses)
"""
import json, logging, os, re, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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

PUMP_PAGE_URL = 'https://bidgee182.github.io/wwcc-weather-page/pump-station.html'
LOGO_URL      = 'https://bidgee182.github.io/wwcc-weather-page/assets/images/logo-white.png'
PUMP_LABELS   = ['P1', 'P2', 'P3', 'Jockey']

SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
EMAIL_FROM       = os.environ.get('EMAIL_FROM', '')
_TO_RAW          = os.environ.get('PUMP_EMAIL_RECIPIENTS', '')
_CC_RAW          = os.environ.get('PUMP_EMAIL_CC', '')
_BCC_RAW         = os.environ.get('PUMP_EMAIL_BCC', '')


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


def build_html(day_data, latest, yesterday_str, alarms_24h):
    from datetime import datetime as _dt
    date_label = _dt.strptime(yesterday_str, '%Y-%m-%d').strftime('%-d %B %Y')

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
               font-weight:bold;color:#ffffff;">Pump Station Daily Summary</td></tr>
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
{_section_heading("Yesterday's System Pressure")}
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
{_section_heading("Yesterday's Pump Run Hours")}
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
{_section_heading("Yesterday's Alarms and Events")}
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

def send_email(subject, html):
    to_list  = _addr(_TO_RAW)
    cc_list  = _addr(_CC_RAW)
    bcc_list = _addr(_BCC_RAW)
    if not SENDGRID_API_KEY or not to_list or not EMAIL_FROM:
        log.warning('Missing credentials or recipients - skipping.')
        return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, To, Cc, Bcc, Email
        mail = Mail(
            from_email=Email(EMAIL_FROM),
            subject=subject,
            plain_text_content=html_to_text(html),
            html_content=html,
        )
        mail.to = [To(r) for r in to_list]
        if cc_list:
            mail.cc = [Cc(r) for r in cc_list]
        if bcc_list:
            mail.bcc = [Bcc(r) for r in bcc_list]
        resp = SendGridAPIClient(SENDGRID_API_KEY).send(mail)
        log.info(f'Sent "{subject}" - HTTP {resp.status_code} - to {to_list} cc {cc_list} bcc {bcc_list}')
        return True
    except Exception as e:
        log.error(f'Send error: {e}')
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    now_syd   = datetime.now(tz=SYDNEY_TZ)
    yesterday = (now_syd - timedelta(days=1)).date()
    yesterday_str = yesterday.isoformat()

    # Daily stats for yesterday
    daily_data    = load_json(DAILY_FILE, {})
    days          = daily_data.get('days', [])
    day_data      = next((d for d in reversed(days)
                          if d.get('date') == yesterday_str), None)
    if day_data is None:
        log.warning(f'No daily data for {yesterday_str} - using empty defaults.')
        day_data = {'date': yesterday_str}

    latest      = load_json(LATEST_FILE, {})
    alarms_data = load_json(ALARM_FILE, {})
    all_events  = alarms_data.get('events', [])

    # Alarms that occurred yesterday (AEST)
    y_start = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=SYDNEY_TZ)
    y_end   = y_start + timedelta(days=1)
    alarms_24h = []
    for e in all_events:
        try:
            ts = datetime.fromisoformat(
                e['timestamp_iso'].replace('Z', '+00:00')).astimezone(SYDNEY_TZ)
            if y_start <= ts < y_end:
                alarms_24h.append(e)
        except Exception:
            pass
    alarms_24h.sort(key=lambda e: e.get('timestamp_iso', ''))

    date_label = yesterday.strftime('%-d %B %Y')
    subject    = f'Pump Station Daily Summary - {date_label}'
    html       = build_html(day_data, latest, yesterday_str, alarms_24h)
    send_email(subject, html)


if __name__ == '__main__':
    main()
