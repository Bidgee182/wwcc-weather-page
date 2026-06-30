#!/usr/bin/env python3
"""
FarmBot Tank Poll
=================
Runs every 30 minutes via GitHub Actions.
- Fetches latest tank level from FarmBot API
- Writes data/farmbot_latest.json (served by GitHub Pages)
- Sends SendGrid alert emails when tank crosses threshold levels
- Backfill mode: fetches all history from a start date into data/farmbot_history.json
"""

import os, sys, json, logging, argparse
import requests
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
FARMBOT_CLIENT_ID     = os.environ.get('FARMBOT_CLIENT_ID',    '')
FARMBOT_CLIENT_SECRET = os.environ.get('FARMBOT_CLIENT_SECRET','')
FARMBOT_TANK_SID      = os.environ.get('FARMBOT_TANK_SID',     '')
TANK_CAPACITY_L       = int(os.environ.get('FARMBOT_TANK_CAPACITY_L', '250000'))

SENDGRID_API_KEY    = os.environ.get('SENDGRID_API_KEY',    '')
EMAIL_FROM          = os.environ.get('EMAIL_FROM',          '')
EMAIL_GK_RECIPIENTS = os.environ.get('EMAIL_GK_RECIPIENTS', '')

FB_AUTH_URL = 'https://auth.fmbt.io/oauth2/token'
FB_API_BASE = 'https://api.myxbot-production-au.fmbt.io/public-api/v1'
SYDNEY_TZ   = ZoneInfo('Australia/Sydney')

DATA_DIR     = Path('data')
LATEST_JSON  = DATA_DIR / 'farmbot_latest.json'
HISTORY_JSON = DATA_DIR / 'farmbot_history.json'

# Alert thresholds — email fires when tank DROPS INTO each band
ALERT_LEVELS = [
    ('critical', 10,  '🔴 URGENT: Water tank critically low'),
    ('low',      20,  '🔴 Water tank LOW alert'),
    ('warning',  40,  '🟡 Water tank warning'),
]

# ── Auth ──────────────────────────────────────────────────────────────────────
def get_token():
    r = requests.post(FB_AUTH_URL, data={
        'grant_type':    'client_credentials',
        'client_id':     FARMBOT_CLIENT_ID,
        'client_secret': FARMBOT_CLIENT_SECRET,
    }, timeout=30)
    r.raise_for_status()
    token = r.json().get('access_token')
    if not token:
        raise RuntimeError(f'No access_token in response: {r.text}')
    return token

def fb_get(token, path, params=None):
    r = requests.get(
        f'{FB_API_BASE}/{path}',
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

# ── Calculations ──────────────────────────────────────────────────────────────
def calc_pct(rw_value, total_height):
    if not total_height or rw_value is None:
        return None
    return round(min(100.0, max(0.0, (rw_value / total_height) * 100)), 1)

def calc_volume(pct):
    if pct is None:
        return None
    return round((pct / 100) * TANK_CAPACITY_L)

def status_label(pct):
    if pct is None: return 'Unknown'
    if pct >= 80:   return 'Full'
    if pct >= 50:   return 'Good'
    if pct >= 20:   return 'Low'
    return 'Critical'

def alert_state_for(pct):
    if pct is None:  return 'unknown'
    if pct < 10:     return 'critical'
    if pct < 20:     return 'low'
    if pct < 40:     return 'warning'
    return 'ok'

STATE_ORDER = {'unknown': -1, 'ok': 0, 'warning': 1, 'low': 2, 'critical': 3}

# ── Alert email ───────────────────────────────────────────────────────────────
def send_alert(pct, volume_l, state):
    if not SENDGRID_API_KEY or not EMAIL_GK_RECIPIENTS:
        log.warning('SendGrid not configured — skipping alert email')
        return
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail, To, Email

    volume_kl = (volume_l or 0) / 1000
    color     = '#dc2626' if state in ('critical', 'low') else '#d97706'
    urgency   = 'URGENT — ' if state == 'critical' else ''
    subject   = f'{urgency}Water tank {state} alert: {pct:.0f}% — Wagga CC'

    action_note = {
        'critical': 'The water tank is critically low. Immediate action required — check supply lines and arrange emergency top-up.',
        'low':      'The water tank level is low. Schedule a top-up soon to avoid disruption to irrigation.',
        'warning':  'The water tank is below 40%. Monitor closely and plan a top-up.',
    }.get(state, '')

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f4f8;padding:28px 0;">
<tr><td align="center">
<table width="500" cellpadding="0" cellspacing="0"
    style="max-width:500px;width:100%;background:white;border-radius:14px;overflow:hidden;
    box-shadow:0 4px 20px rgba(0,0,0,0.08);">
  <tr><td bgcolor="{color}" style="background-color:{color};padding:28px 24px;">
    <div style="font-size:22px;font-weight:700;color:white;">{urgency}Water Tank Alert</div>
    <div style="font-size:13px;color:rgba(255,255,255,0.85);margin-top:4px;">Wagga Wagga Country Club</div>
  </td></tr>
  <tr><td style="padding:28px 24px;text-align:center;">
    <div style="font-size:64px;font-weight:700;color:{color};line-height:1;">{pct:.0f}%</div>
    <div style="font-size:14px;color:#64748b;margin-top:6px;">{volume_kl:.1f} kL remaining of 250 kL</div>
    <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;
        padding:14px 16px;margin-top:20px;font-size:13px;color:#991b1b;text-align:left;">
      {action_note}
    </div>
    <a href="https://bidgee182.github.io/wwcc-weather-page/?gk=1"
        style="display:inline-block;background:#2980b9;color:white;text-decoration:none;
        font-size:13px;font-weight:700;padding:12px 28px;border-radius:20px;margin-top:20px;">
        View Dashboard</a>
  </td></tr>
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:16px 24px;text-align:center;">
    <div style="font-size:11px;color:rgba(255,255,255,0.5);">
      Wagga Wagga Country Club — Automated FarmBot Alert</div>
  </td></tr>
</table></td></tr></table>
</body></html>"""

    recipients = [r.strip() for r in EMAIL_GK_RECIPIENTS.split(',') if r.strip()]
    sg   = SendGridAPIClient(SENDGRID_API_KEY)
    mail = Mail(from_email=Email(EMAIL_FROM), subject=subject, html_content=html)
    for addr in recipients:
        mail.add_to(To(addr))
    sg.send(mail)
    log.info(f'Alert email sent ({state}) to {len(recipients)} recipient(s)')

# ── Fetch graph samples ────────────────────────────────────────────────────────
def fetch_graph_samples(token, total_height, hours=24):
    """Fetch up to 3 pages of samples covering the last N hours for the trend graph."""
    start = (datetime.utcnow() - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    graph = []
    for page in range(1, 5):
        resp = fb_get(token, f'sensor/{FARMBOT_TANK_SID}/sample', {
            'pageSize': 10, 'order': 'ASC', 'page': page, 'startDate': start,
        })
        for s in resp.get('data', []):
            p = calc_pct(s.get('rwValue'), total_height)
            if p is not None:
                graph.append({'date': s['date'], 'pct': p, 'volume_l': calc_volume(p)})
        if page >= resp.get('totalPages', 1):
            break
    return graph

# ── Regular poll ──────────────────────────────────────────────────────────────
def poll():
    log.info('FarmBot poll starting')
    token = get_token()
    log.info('Authenticated OK')

    # Sensor config
    sensor       = fb_get(token, f'sensor/{FARMBOT_TANK_SID}')
    total_height = sensor.get('config', {}).get('totalHeight') or 170
    log.info(f'Sensor: {sensor.get("name")} | totalHeight={total_height}cm')

    # Latest reading
    resp   = fb_get(token, f'sensor/{FARMBOT_TANK_SID}/sample', {'pageSize': 10, 'order': 'DESC', 'page': 1})
    latest = (resp.get('data') or [None])[0]

    pct = vol = rw = sample_date = battery = None
    if latest:
        rw          = latest.get('rwValue')
        pct         = calc_pct(rw, total_height)
        vol         = calc_volume(pct)
        sample_date = latest.get('date')
        battery     = latest.get('extraValues', {}).get('batteryLevel')
        log.info(f'Latest: rwValue={rw}cm | {pct}% | {vol}L | {sample_date}')

    # Graph data (24h)
    graph = fetch_graph_samples(token, total_height, hours=24)
    log.info(f'Graph: {len(graph)} points')

    # Alert threshold check
    current_state = alert_state_for(pct)
    prev_state    = 'ok'
    if LATEST_JSON.exists():
        try:
            prev_state = json.loads(LATEST_JSON.read_text()).get('tank_alert_state', 'ok')
        except Exception:
            pass

    if STATE_ORDER.get(current_state, 0) > STATE_ORDER.get(prev_state, 0):
        try:
            send_alert(pct, vol, current_state)
        except Exception as e:
            log.error(f'Alert email failed: {e}')

    # Write output
    DATA_DIR.mkdir(exist_ok=True)
    out = {
        'tank_pct':          pct,
        'tank_volume_l':     vol,
        'tank_rwvalue_cm':   rw,
        'tank_total_height': total_height,
        'tank_total_volume': TANK_CAPACITY_L,
        'tank_date':         sample_date,
        'tank_status':       status_label(pct),
        'tank_alert_state':  current_state,
        'tank_battery':      battery,
        'tank_graph':        graph,
        'updated_at':        datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    LATEST_JSON.write_text(json.dumps(out, indent=2))
    log.info(f'Wrote {LATEST_JSON}')

# ── Historical backfill ────────────────────────────────────────────────────────
def backfill(from_date='2025-01-01'):
    log.info(f'FarmBot backfill from {from_date}')
    token = get_token()
    log.info('Authenticated OK')

    sensor       = fb_get(token, f'sensor/{FARMBOT_TANK_SID}')
    total_height = sensor.get('config', {}).get('totalHeight') or 170

    start_str = f'{from_date}T00:00:00Z'

    all_samples = []
    page = 1
    while True:
        resp = fb_get(token, f'sensor/{FARMBOT_TANK_SID}/sample', {
            'pageSize': 10, 'order': 'ASC', 'page': page,
            'startDate': start_str,
        })
        data = resp.get('data', [])
        all_samples.extend(data)
        total_pages = resp.get('totalPages', 1)
        log.info(f'  Page {page}/{total_pages} — {len(all_samples)} samples total')
        if page >= total_pages:
            break
        page += 1

    log.info(f'Fetched {len(all_samples)} samples — grouping by day (Sydney time)')

    # Group by Sydney date
    by_date = {}
    for s in all_samples:
        dt_utc = datetime.fromisoformat(s['date'].replace('Z', '+00:00'))
        dt_syd = dt_utc.astimezone(SYDNEY_TZ)
        d      = dt_syd.date().isoformat()
        by_date.setdefault(d, []).append(s)

    history = []
    for d in sorted(by_date.keys()):
        day = sorted(by_date[d], key=lambda s: s['date'])
        morning_pct = calc_pct(day[0].get('rwValue'),  total_height)
        evening_pct = calc_pct(day[-1].get('rwValue'), total_height)
        all_pcts    = [calc_pct(s.get('rwValue'), total_height) for s in day]
        all_pcts    = [p for p in all_pcts if p is not None]
        used_pct    = max(0.0, (morning_pct or 0) - (evening_pct or 0))
        used_l      = round(used_pct / 100 * TANK_CAPACITY_L)
        refill      = (evening_pct or 0) > (morning_pct or 0) + 2

        history.append({
            'date':        d,
            'morning_pct': morning_pct,
            'evening_pct': evening_pct,
            'min_pct':     round(min(all_pcts), 1) if all_pcts else None,
            'max_pct':     round(max(all_pcts), 1) if all_pcts else None,
            'used_l':      used_l,
            'used_pct':    round(used_pct, 1),
            'refill':      refill,
            'readings':    len(day),
        })

    DATA_DIR.mkdir(exist_ok=True)
    HISTORY_JSON.write_text(json.dumps(history, indent=2))
    log.info(f'Wrote {len(history)} daily records to {HISTORY_JSON}')

# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='FarmBot tank data poller')
    parser.add_argument('--backfill',  action='store_true', help='Run historical backfill')
    parser.add_argument('--from',      dest='from_date', default='2025-01-01',
                        help='Backfill start date YYYY-MM-DD (default: 2025-01-01)')
    args = parser.parse_args()

    if not FARMBOT_CLIENT_ID or not FARMBOT_CLIENT_SECRET or not FARMBOT_TANK_SID:
        log.error('Missing FARMBOT_CLIENT_ID, FARMBOT_CLIENT_SECRET or FARMBOT_TANK_SID')
        sys.exit(1)

    if args.backfill:
        backfill(args.from_date)
    else:
        poll()
