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

sys.path.insert(0, str(Path(__file__).parent))
from lake_utils import log_email

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

SYDNEY_TZ  = ZoneInfo('Australia/Sydney')
DATA_DIR   = Path(__file__).parent.parent / 'data'

LATEST_FILE  = DATA_DIR / 'pump_station_latest.json'
ALARM_FILE   = DATA_DIR / 'pump_alarm_history.json'
EMCFG_FILE   = DATA_DIR / 'email_config.json'
STATE_FILE  = DATA_DIR / 'pump_email_state.json'

PUMP_PAGE_URL = 'https://bidgee182.github.io/wwcc-weather-page/pump-station.html'
LOGO_URL      = 'https://bidgee182.github.io/wwcc-weather-page/assets/images/logo-white.png'

SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY', '')
EMAIL_FROM       = os.environ.get('EMAIL_FROM', '')
_TO_RAW          = os.environ.get('PUMP_EMAIL_RECIPIENTS', '')
_CC_RAW          = os.environ.get('PUMP_EMAIL_CC', '')
_BCC_RAW         = os.environ.get('PUMP_EMAIL_BCC', '')

OFFLINE_GRACE_MIN = 15  # alert after this many minutes offline
REMINDER_HOURS    = 6

# Guidance hints per alarm code - shown inside fault and reminder emails
ALARM_HINTS = {
    '4': {
        'what': (
            'The controller detected too many restart attempts in a short period. '
            'This is usually caused by a motor temperature or water temperature trip '
            '(via the PT100 sensor fitted at the top of each pump), but can also be '
            'caused by a mechanical or electrical fault.'
        ),
        'check': [
            'Let the pump rest 10-15 minutes so the motor or water temperature can stabilise.',
            'Check the CU352 display for a secondary code - it will show whether this '
            'is a motor temperature trip or a water temperature (PT100 sensor) trip.',
            'Check suction - is the tank level low, or is the inlet valve fully open?',
            'Check the discharge valve is not closed or partially closed.',
        ],
        'recurring': [
            'Motor temperature: ensure the pump room has adequate ventilation. '
            'Is the motor hot to touch? It may need time to cool before restarting.',
            'Water temperature (PT100): check the PT100 reading on the CU352. '
            'If the water being pumped is warm, allow it to cool before restarting.',
            'Mechanical: a worn impeller, failing bearing, or blocked pump casing '
            'can cause repeated overloads.',
        ],
    },
    '12': {
        'what': (
            'Very low or no flow was detected. This is most commonly caused by a leak '
            'in the system maintaining just enough demand to prevent a clean stop, '
            'causing repeated cycling. The controller drops the cut-in pressure slightly '
            'to reduce stop-starts - fix the leak to resolve this.'
        ),
        'check': [
            'Walk the irrigation network and look for water appearing in unexpected places '
            '- broken heads, weeping joins, or a stuck-open solenoid zone.',
            'To find which side the leak is on: isolate the discharge valve and watch if '
            'pressure holds. If it holds, the leak is on the discharge/irrigation side. '
            'If pressure still drops, the leak is on the suction or tank inlet side.',
        ],
        'recurring': [
            'Fix the leak - the low-flow stop is a symptom, not the cause.',
            'Check all solenoid valves are closing fully when zones finish.',
            'Check non-return valves are not allowing backflow when pumps are off.',
        ],
    },
    '40': {
        'what': (
            'The supply voltage to the pump room dropped below the minimum acceptable '
            'level for the controller.'
        ),
        'check': [
            'Check the main switchboard and supply isolator to the pump room.',
            'Has any other equipment on the same circuit tripped or just started up?',
        ],
        'recurring': [
            'Have an electrician log the supply voltage quality - the pump room circuit '
            'may be undersized, or shared with other large loads causing sags.',
            'Large motors starting on the same circuit can cause brief voltage drops '
            'that the pump controller detects as undervoltage.',
        ],
    },
    '190': {
        'what': (
            'An analog input has exceeded the "Limit 1" threshold set in the CU352. '
            'On this system, this is most likely a water temperature trip from the PT100 '
            'sensors fitted at the top of each pump - though it can also relate to '
            'discharge pressure depending on how Limit 1 is configured.'
        ),
        'check': [
            'Check the CU352 display - it will show which analog input triggered the '
            'limit (water temperature via PT100, or discharge pressure).',
            'If water temperature (PT100): is the water being pumped unusually warm? '
            'Extended running at low or no flow can heat water in the pump casing.',
            'If discharge pressure: check for any downstream isolation valves that are '
            'partially or fully closed.',
            'Press Reset Alarms on the dashboard once the cause is identified and fixed.',
        ],
        'recurring': [
            'If water temperature is repeatedly high: check the pump is getting adequate '
            'flow and not running dry or at very low flow for extended periods.',
            'Check PT100 sensor wiring and connections at the pump head - a faulty '
            'sensor can give false high readings and trigger this alarm incorrectly.',
            'This alarm has been active since 7 Aug - it should be investigated as a priority.',
        ],
    },
    '210': {
        'what': (
            'System pressure exceeded the high-pressure alarm setpoint on the CU352.'
        ),
        'check': [
            'Did a zone solenoid valve close unexpectedly while a pump was running? '
            'Check all active irrigation zones are open.',
            'Is any manual isolation valve on the main line partially or fully closed?',
            'Check for blocked filters or strainers between the pumps and the network. '
            'Pressure will normally drop once flow can resume.',
        ],
        'recurring': [
            'The high-pressure setpoint in the CU352 may be set too close to normal '
            'operating pressure - small pressure variations keep triggering it.',
            'Check the pressure relief valve is not stuck closed.',
            'A zone solenoid valve that sticks shut intermittently will cause this repeatedly.',
        ],
    },
    '214': {
        'what': (
            'The electrical float valve in the supply tank has signalled a low-level '
            'condition. This is monitored via DI2 (Water Shortage / Tank Float input '
            'on the CU352).'
        ),
        'check': [
            'Check the tank level on the Pump Station dashboard.',
            'Check the float valve is moving freely and its electrical connection is '
            'intact - a stuck or waterlogged float can give a false low-level alarm.',
            'If the tank is genuinely low, check the town water supply to the tank '
            'is flowing and the inlet is open.',
        ],
        'recurring': [
            'Irrigation demand may be outpacing the tank refill rate - review scheduling '
            'to spread the load across the day.',
            'Check the float valve ball is not waterlogged (sunken), which can hold '
            'the inlet valve open and waste town water.',
            'Check town water supply pressure and flow rate into the tank.',
        ],
    },
}


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


def _wrap(header_html, body_html, hint_html=''):
    return f'''<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center" style="padding:24px 0;">
<table width="620" cellpadding="0" cellspacing="0"
       style="background:#ffffff;border-radius:6px;overflow:hidden;">
<tr><td>
{header_html}
{body_html}
{hint_html}
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


def _hint_html(code):
    hint = ALARM_HINTS.get(str(code))
    if not hint:
        return ''
    def _li(items):
        return ''.join(
            f'<li style="margin-bottom:5px;">{i}</li>' for i in items
        )
    LBL = ('font-family:Arial,sans-serif;font-size:11px;font-weight:bold;'
           'color:#7a4500;letter-spacing:1px;text-transform:uppercase;margin-bottom:6px;')
    TXT = 'font-family:Arial,sans-serif;font-size:13px;color:#444;'
    return f'''<table width="100%" cellpadding="0" cellspacing="0"
        style="padding:4px 32px 20px;">
<tr><td><div style="background:#fffbf0;border:1px solid #f0d878;border-radius:4px;
               padding:16px 20px;">
<div style="{LBL}">What this alarm means</div>
<p style="{TXT}margin:0 0 14px;">{hint['what']}</p>
<div style="{LBL}">What to check first</div>
<ul style="{TXT}margin:0 0 14px;padding-left:18px;">{_li(hint['check'])}</ul>
<div style="{LBL}">If it keeps happening</div>
<ul style="{TXT}margin:0;padding-left:18px;">{_li(hint['recurring'])}</ul>
</div></td></tr></table>'''


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


def _alarm_rows(alarm_or_entry, latest, extra_rows=None):
    sys   = latest.get('system', {})
    p     = sys.get('pressure_actual_bar')
    p_str = f'{p:.2f} bar' if p is not None else 'unknown'
    desc  = (alarm_or_entry.get('description') or alarm_or_entry.get('alarm_desc') or '-')
    src   = (alarm_or_entry.get('pump_label')  or alarm_or_entry.get('alarm_label') or 'System')
    ts    = parse_iso(alarm_or_entry.get('timestamp_iso') or alarm_or_entry.get('appeared_at'))
    rows  = [
        ('Time detected',   fmt_aest(ts), True),
        ('Description',     desc, False),
        ('Source',          src, True),
        ('System pressure', p_str, False),
    ]
    if extra_rows:
        rows.extend(extra_rows)
    return rows


def fault_html(alarm, latest):
    code = str(alarm.get('code', ''))
    name = alarm.get('type_name', 'Fault Alarm')
    tbl  = _table(_alarm_rows(alarm, latest))
    note = 'A fault alarm has been detected on the Grundfos pump station. Review the dashboard for current system status.'
    return _wrap(_header('#c0392b', f'PUMP STATION - {name.upper()}'),
                 _body_section(tbl, note),
                 _hint_html(code))


def reminder_html(alarm_or_entry, latest, first_alerted_at, code=''):
    name = (alarm_or_entry.get('type_name') or alarm_or_entry.get('alarm_name') or 'Fault Alarm')
    first_dt   = parse_iso(first_alerted_at)
    now        = datetime.now(timezone.utc)
    active_dur = duration_str((now - first_dt).total_seconds()) if first_dt else '?'
    extra = [('Active for', active_dur, True)]
    tbl   = _table(_alarm_rows(alarm_or_entry, latest, extra))
    note  = 'This fault is still active and has not been cleared. Next reminder in 6 hours.'
    return _wrap(_header('#e67e22', f'REMINDER - {name.upper()} STILL ACTIVE'),
                 _body_section(tbl, note),
                 _hint_html(code))


def cleared_html(entry, cleared_event, latest):
    name      = entry.get('alarm_name') or entry.get('type_name') or 'Fault Alarm'
    first_dt  = parse_iso(entry.get('first_alerted_at') or entry.get('appeared_at'))
    clear_dt  = parse_iso((cleared_event or {}).get('timestamp_iso'))
    now       = datetime.now(timezone.utc)
    total_dur = duration_str((now - first_dt).total_seconds()) if first_dt else '?'
    sys       = latest.get('system', {})
    p         = sys.get('pressure_actual_bar')
    p_str     = f'{p:.2f} bar' if p is not None else 'unknown'
    rows = [
        ('Alarm',           name, True),
        ('Source',          entry.get('alarm_label', 'System'), False),
        ('Cleared at',      fmt_aest(clear_dt) if clear_dt else 'Unknown', True),
        ('Total duration',  total_dur, False),
        ('System pressure', p_str, True),
    ]
    note = 'The fault alarm has been cleared. No further action required.'
    return _wrap(_header('#27ae60', f'PUMP STATION - {name.upper()} CLEARED'),
                 _body_section(_table(rows), note))


# ---------------------------------------------------------------------------
# SendGrid
# ---------------------------------------------------------------------------

def alerts_enabled():
    try:
        cfg = json.loads(EMCFG_FILE.read_text())
        return cfg.get('email_enabled', {}).get('pump_alerts', True)
    except Exception:
        return True


def send_email(subject, html, email_type='pump_alert'):
    to_list  = _addr(_TO_RAW)
    cc_list  = _addr(_CC_RAW)
    bcc_list = _addr(_BCC_RAW)
    everyone = to_list + cc_list + bcc_list
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
        log_email(email_type, subject, everyone, f'sent ({resp.status_code})')
        return True
    except Exception as e:
        log.error(f'Send error: {e}')
        log_email(email_type, subject, everyone, f'failed: {e}')
        return False


# ---------------------------------------------------------------------------
# Alarm history helpers
# ---------------------------------------------------------------------------

def active_alarms_from_history(events):
    """Return {str(code): last_appeared_event} for codes whose most recent
    event is type_code==1 (appeared). Processes history oldest-first."""
    last_by_code = {}
    for e in reversed(events):   # reversed = oldest first (history is newest first)
        last_by_code[str(e.get('code', '?'))] = e
    return {code: e for code, e in last_by_code.items()
            if e.get('type_code') == 1}


def last_cleared_event(events, code):
    """Most recent type_code==2 event for this alarm code, or None."""
    for e in events:   # newest first
        if str(e.get('code')) == str(code) and e.get('type_code') == 2:
            return e
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not alerts_enabled():
        log.info('pump_alerts disabled in email_config.json - skipping.')
        return

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
        'active_alarms':      {},
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

        # -- Fault alarm tracking (only when connected - data is current) ----
        current_active = active_alarms_from_history(events)
        state_active   = state.get('active_alarms', {})

        # 1. Newly appeared alarms (in current but not tracked in state)
        for code, alarm in current_active.items():
            if code not in state_active:
                name = alarm.get('type_name', 'Alarm appeared')
                desc = alarm.get('description', '')
                src  = alarm.get('pump_label', 'System')
                log.info(f'New fault code {code} ({desc}) - sending alert')
                html    = fault_html(alarm, latest)
                subject = f'PUMP STATION FAULT - {desc or name}'
                if send_email(subject, html):
                    state_active[code] = {
                        'appeared_at':     alarm.get('timestamp_iso'),
                        'alarm_name':      name,
                        'alarm_desc':      desc,
                        'alarm_label':     src,
                        'first_alerted_at': now.isoformat(),
                        'last_alerted_at':  now.isoformat(),
                    }
                    changed = True

        # 2. Still-active alarms - check for 6h reminder
        for code, entry in list(state_active.items()):
            if code in current_active:
                last_dt = parse_iso(entry.get('last_alerted_at'))
                if last_dt and (now - last_dt).total_seconds() >= REMINDER_HOURS * 3600:
                    alarm = current_active[code]
                    desc  = entry.get('alarm_desc') or entry.get('alarm_name', 'Fault')
                    log.info(f'Fault code {code} still active - sending 6h reminder')
                    html    = reminder_html(entry, latest, entry.get('first_alerted_at'), code)
                    subject = f'PUMP STATION REMINDER - {desc} still active'
                    if send_email(subject, html):
                        state_active[code]['last_alerted_at'] = now.isoformat()
                        changed = True

        # 3. Cleared alarms (were tracked, now gone from current active)
        for code in list(state_active.keys()):
            if code not in current_active:
                entry        = state_active[code]
                cleared_evt  = last_cleared_event(events, code)
                desc         = entry.get('alarm_desc') or entry.get('alarm_name', 'Fault')
                log.info(f'Fault code {code} cleared - sending cleared email')
                html    = cleared_html(entry, cleared_evt, latest)
                subject = f'PUMP STATION CLEARED - {desc}'
                send_email(subject, html)
                del state_active[code]
                changed = True

        state['active_alarms'] = state_active

    if changed:
        save_json(STATE_FILE, state)
        log.info('State file updated.')


if __name__ == '__main__':
    main()
