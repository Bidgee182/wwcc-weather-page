#!/usr/bin/env python3
"""
Pump Station Alert Email
========================
Sends alert emails when:
  - Station goes offline (after 15+ min of failed polls)
  - Station recovers after an outage
  - A new fault-level alarm appears (type_code == 1)

Runs as a step in poll-grundfos.yml after each poll.
State is persisted in data/pump_email_state.json so dedup
works across workflow runs.

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
ALARM_FILE  = DATA_DIR / 'pump_alarm_history.json'
STATE_FILE  = DATA_DIR / 'pump_email_state.json'

PUMP_PAGE_URL = 'https://bidgee182.github.io/wwcc-weather-page/pump-station.html'
LOGO_URL      = 'https://bidgee182.github.io/wwcc-weather-page/assets/images/logo-white.png'

SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
EMAIL_FROM       = os.environ.get('EMAIL_FROM', '')
_TO_RAW          = os.environ.get('PUMP_EMAIL_RECIPIENTS', '')
_CC_RAW          = os.environ.get('PUMP_EMAIL_CC', '')
_BCC_RAW         = os.environ.get('PUMP_EMAIL_BCC', '')

OFFLINE_GRACE_MIN = 15  # alert after this many minutes offline


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


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, default=str))


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except Exception:
        return None


def fmt_aest(dt):
    if dt is None:
        return 'unknown'
    return dt.astimezone(SYDNEY_TZ).strftime('%-d %b %Y %-I:%M %p AEST')


def duration_str(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60} min'
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f'{h}h {m}m' if m else f'{h}h'


def html_to_text(html):
    text = re.sub(r'<br\s*/?>', '\n', html, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


# ---------------------------------------------------------------------------
# HTML building blocks
# ---------------------------------------------------------------------------

def _logo_html():
    return (f'<img src="{LOGO_URL}" width="194" height="44"'
            f' alt="Wagga Wagga Country Club" style="display:block;border:0;">')


def _wrap(header_html, body_html):
    return f'''<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center" style="padding:24px 0;">
<table width="620" cellpadding="0" cellspacing="0"
       style="background:#ffffff;border-radius:6px;overflow:hidden;">
<tr><td>
{header_html}
{body_html}
<table width="100%" cellpadding="0" cellspacing="0"
       style="padding:20px 32px 28px;background:#f4f4f4;">
<tr><td style="font-family:Arial,sans-serif;font-size:12px;color:#888;text-align:center;">
<a href="{PUMP_PAGE_URL}" style="color:#1a73e8;">View Pump Station Dashboard</a>
</td></tr></table>
</td></tr></table>
</td></tr></table>
</body></html>'''


def _header(bg, title):
    return f'''<table width="100%" cellpadding="0" cellspacing="0"
        style="background:{bg};padding:28px 32px;">
<tr><td>{_logo_html()}</td></tr>
<tr><td style="padding-top:18px;font-family:Arial,sans-serif;font-size:22px;
               font-weight:bold;color:#ffffff;letter-spacing:0.5px;">{title}</td></tr>
</table>'''


def _table(rows):
    """rows: list of (label, value, is_shaded) tuples"""
    cells = ''
    for label, value, shaded in rows:
        bg = 'background:#f9f9f9;' if shaded else ''
        cells += (f'<tr style="{bg}"><td style="padding:8px 12px;color:#666;width:40%;">'
                  f'{label}</td><td style="padding:8px 12px;">{value}</td></tr>')
    return (f'<table width="100%" cellpadding="0" cellspacing="0"'
            f' style="border:1px solid #eee;border-radius:4px;'
            f'font-family:Arial,sans-serif;font-size:14px;">{cells}</table>')


def _body_section(table_html, note=''):
    note_html = (f'<tr><td style="padding-top:18px;font-family:Arial,sans-serif;'
                 f'font-size:13px;color:#888;">{note}</td></tr>') if note else ''
    return f'''<table width="100%" cellpadding="0" cellspacing="0"
        style="padding:24px 32px 8px;">
<tr><td>{table_html}</td></tr>
{note_html}
</table>'''


# ---------------------------------------------------------------------------
# Email bodies
# ---------------------------------------------------------------------------

def offline_html(latest, offline_since_dt, offline_dur):
    sys = latest.get('system', {})
    last_p = sys.get('pressure_actual_bar')
    p_str  = f'{last_p:.2f} bar' if last_p is not None else 'unknown'
    last_c = parse_iso(latest.get('last_connected') or latest.get('last_seen'))

    tbl = _table([
        ('Status',              '<span style="color:#c0392b;font-weight:bold;">OFFLINE</span>', True),
        ('Offline for',         offline_dur, False),
        ('Station went offline', fmt_aest(offline_since_dt), True),
        ('Last pressure reading', p_str, False),
        ('Last successful data', fmt_aest(last_c), True),
    ])
    note = ('This may indicate a power outage, network failure, or SIM IP change.<br>'
            'Check that Duck DNS is pointing to the correct IP and the USR modem is online.')
    return _wrap(_header('#c0392b', 'PUMP STATION - CONNECTION LOST'),
                 _body_section(tbl, note))


def recovery_html(latest, offline_since_dt, offline_dur):
    sys   = latest.get('system', {})
    p     = sys.get('pressure_actual_bar')
    p_str = f'{p:.2f} bar' if p is not None else 'unknown'
    mode  = sys.get('mode', 'unknown').title()

    tbl = _table([
        ('Status',         '<span style="color:#27ae60;font-weight:bold;">ONLINE</span>', True),
        ('Outage duration', offline_dur, False),
        ('Offline since',   fmt_aest(offline_since_dt), True),
        ('System pressure', p_str, False),
        ('Mode',            mode, True),
    ])
    return _wrap(_header('#27ae60', 'PUMP STATION - CONNECTION RESTORED'),
                 _body_section(tbl))


def fault_html(alarm, latest):
    sys  = latest.get('system', {})
    p    = sys.get('pressure_actual_bar')
    p_str = f'{p:.2f} bar' if p is not None else 'unknown'
    ts   = parse_iso(alarm.get('timestamp_iso'))
    name = alarm.get('type_name', 'Fault Alarm')
    desc = alarm.get('description', '-')
    src  = alarm.get('pump_label', 'System')

    intro = (f'<table width="100%" cellpadding="0" cellspacing="0" style="padding:20px 32px 0;">'
             f'<tr><td style="font-family:Arial,sans-serif;font-size:15px;color:#333;">'
             f'A fault alarm has been detected on the Grundfos pump station.</td></tr></table>')
    tbl = _table([
        ('Alarm',           f'<span style="color:#e67e22;font-weight:bold;">{name}</span>', True),
        ('Source',           src, False),
        ('Time detected',    fmt_aest(ts), True),
        ('Description',      desc, False),
        ('System pressure',  p_str, True),
    ])
    note = 'Review the pump station dashboard for current system status.'
    return _wrap(_header('#e67e22', f'PUMP STATION - FAULT ALARM'),
                 intro + _body_section(tbl, note))


# ---------------------------------------------------------------------------
# SendGrid
# ---------------------------------------------------------------------------

def send_email(subject, html):
    to_list  = _addr(_TO_RAW)
    cc_list  = _addr(_CC_RAW)
    bcc_list = _addr(_BCC_RAW)
    if not SENDGRID_API_KEY:
        log.warning('No SENDGRID_API_KEY - skipping.')
        return False
    if not to_list:
        log.warning('No PUMP_EMAIL_RECIPIENTS - skipping.')
        return False
    if not EMAIL_FROM:
        log.warning('No EMAIL_FROM - skipping.')
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
# Fault dedup key
# ---------------------------------------------------------------------------

def fault_key(alarm):
    return f"{alarm.get('timestamp_iso','?')}|{alarm.get('code','?')}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    latest = load_json(LATEST_FILE, {})
    if not latest:
        log.info('No latest data - skipping.')
        return

    alarms_data = load_json(ALARM_FILE, {})
    events      = alarms_data.get('events', [])
    state       = load_json(STATE_FILE, {
        'was_connected':      None,
        'offline_since':      None,
        'offline_alert_sent': False,
        'alerted_fault_keys': [],
    })

    now       = datetime.now(timezone.utc)
    connected = latest.get('connected', False)
    last_conn = parse_iso(latest.get('last_connected') or latest.get('last_seen'))
    changed   = False

    # -- Offline / recovery -------------------------------------------------
    if not connected:
        offline_sec = (now - last_conn).total_seconds() if last_conn else 99999
        dur         = duration_str(offline_sec)

        if offline_sec >= OFFLINE_GRACE_MIN * 60 and not state.get('offline_alert_sent'):
            log.info(f'Station offline {offline_sec/60:.0f} min - sending alert')
            html    = offline_html(latest, last_conn, dur)
            subject = f'PUMP STATION OFFLINE - {fmt_aest(last_conn)}'
            if send_email(subject, html):
                state['offline_alert_sent'] = True
                state['offline_since']      = last_conn.isoformat() if last_conn else None
                changed = True

        if state.get('was_connected') is not False:
            state['was_connected'] = False
            changed = True

    else:  # connected
        was_offline = (state.get('was_connected') is False
                       and state.get('offline_alert_sent'))
        if was_offline:
            log.info('Station recovered - sending recovery email')
            offline_since_dt = parse_iso(state.get('offline_since'))
            offline_sec = ((now - offline_since_dt).total_seconds()
                           if offline_since_dt else 0)
            html    = recovery_html(latest, offline_since_dt, duration_str(offline_sec))
            subject = 'PUMP STATION - Connection Restored'
            send_email(subject, html)
            state['offline_alert_sent'] = False
            state['offline_since']      = None
            changed = True

        if state.get('was_connected') is not True:
            state['was_connected'] = True
            changed = True

    # -- New fault alarms (type_code == 1 only) ------------------------------
    alerted = set(state.get('alerted_fault_keys', []))
    new_faults = [e for e in events
                  if e.get('type_code') == 1 and fault_key(e) not in alerted]

    for alarm in new_faults[:3]:   # cap at 3 per poll to prevent spam
        name = alarm.get('type_name', 'Alarm')
        log.info(f'New fault alarm: {name} - sending alert')
        html    = fault_html(alarm, latest)
        subject = f'PUMP STATION FAULT - {name}'
        if send_email(subject, html):
            alerted.add(fault_key(alarm))
            changed = True

    # Keep last 50 alerted keys
    state['alerted_fault_keys'] = list(alerted)[-50:]

    if changed:
        save_json(STATE_FILE, state)
        log.info('State file updated.')


if __name__ == '__main__':
    main()
