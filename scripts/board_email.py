#!/usr/bin/env python3
"""
board_email.py — Weekly Lake Albert board update email.

Sends Monday 7 AM AEST. Shows current lake level, licence zone,
maximum extraction rate, and estimated days until the next zone
threshold (based on monthly evaporation + maximum pump rate, no rain).

Environment variables:
    SENDGRID_API_KEY   — SendGrid API key
    EMAIL_FROM         — sender address
    EMAIL_BOARD_TO     — comma-separated To recipients
    EMAIL_BOARD_CC     — comma-separated CC recipients (optional)
    EMAIL_BOARD_BCC    — comma-separated BCC recipients (optional)
"""

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
_BODY_BG  = '#f0f4f8'
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
_SENT_FILE = _DATA_DIR / 'board_sent_week.json'


def _already_sent_this_week(now_syd):
    iso_week = now_syd.strftime('%G-W%V')
    try:
        data = json.loads(_SENT_FILE.read_text())
        return data.get('sent_week') == iso_week
    except Exception:
        return False


def _mark_sent_this_week(now_syd):
    iso_week = now_syd.strftime('%G-W%V')
    _SENT_FILE.write_text(json.dumps({'sent_week': iso_week}))


# ── HTML helpers ───────────────────────────────────────────────────────────────

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
    .mob-stat {{ width:100% !important; display:block !important; margin-bottom:10px !important; }}
  }}
  </style>
</head>
<body style="margin:0;padding:16px;background-color:{_BODY_BG};">
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       bgcolor="{_BODY_BG}" style="background-color:{_BODY_BG};">
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
            <h1 style="margin:0;font-size:24px;color:#ffffff;font-weight:bold;
                font-family:Arial,sans-serif;line-height:1.2;">Board Lake Update</h1>
            <p style="margin:4px 0 0 0;font-size:12px;color:#a9cce3;
                font-family:Arial,sans-serif;font-weight:normal;">{subtitle}</p>
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
    <td bgcolor="{_SEC_BG}" style="background-color:{_SEC_BG};padding:7px 20px;">
      <p style="margin:0;font-size:13px;color:#ffffff;font-weight:bold;
          letter-spacing:0.5px;font-family:Arial,sans-serif;">{text}</p>
    </td>
  </tr>
</table>"""


# ── Chart ──────────────────────────────────────────────────────────────────────

def _seven_day_chart(readings):
    """QuickChart.io PNG — daily average AHD for past 7 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    by_day = defaultdict(list)
    for r in readings:
        try:
            ts = datetime.fromisoformat(r['date'].replace('Z', '+00:00'))
            if ts >= cutoff and r.get('ahd'):
                by_day[ts.date().isoformat()].append(float(r['ahd']))
        except Exception:
            pass

    days  = sorted(by_day.keys())
    vals  = [round(sum(by_day[d]) / len(by_day[d]), 3) for d in days]
    labels = [datetime.fromisoformat(d).strftime('%-d %b') for d in days]

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
    return (f'<img src="{url}" width="100%" alt="Lake level — past 7 days"'
            f' style="display:block;border-radius:6px;max-width:100%;">')


# ── Email body ─────────────────────────────────────────────────────────────────

def build_html(now_syd):
    latest   = json.loads((_DATA_DIR / 'farmbot_lake_latest.json').read_text())
    readings = json.loads((_DATA_DIR / 'farmbot_lake_readings.json').read_text())

    ahd = latest.get('lake_ahd')
    if ahd is None:
        log.error('No lake_ahd in farmbot_lake_latest.json')
        return None

    month     = now_syd.month
    zone      = lu.current_zone_info(ahd)
    z_num     = zone['zone']
    z_name    = zone['name']
    z_pump    = zone['max_pump_ml_day']
    z_bg      = zone['color_bg']
    z_txt     = zone['color_text']

    nxt               = lu.next_zone_below(ahd)
    days, _           = lu.days_to_next_zone(ahd, month)

    # 7-day AHD change
    cutoff  = datetime.now(timezone.utc) - timedelta(days=7)
    recent  = sorted(
        [r for r in readings
         if datetime.fromisoformat(r['date'].replace('Z', '+00:00')) >= cutoff
         and r.get('ahd')],
        key=lambda r: r['date']
    )
    week_change = (ahd - float(recent[0]['ahd'])) if recent else None

    # Evaporation detail for disclaimer
    cfg      = lu.get_config()
    pan_mm   = cfg['evaporation']['monthly_pan_mm_day'][str(month)]
    lake_mm  = pan_mm * cfg['evaporation']['pan_factor']
    evap_ml  = lu.evap_ml_day(ahd, month)
    mth_name = now_syd.strftime('%B')

    # Days display
    if days is None:
        days_display = '—'
        days_sub     = 'No lower zone — cease to pump'
        days_col     = '#EB1E23'
    elif days == float('inf'):
        days_display = 'Rising'
        days_sub     = 'Net lake gain — level increasing'
        days_col     = '#00762A'
    else:
        days_int     = int(days)
        days_display = f'~{days_int:,}'
        next_name    = nxt['name'] if nxt else 'next zone'
        days_sub     = f'days until {next_name}'
        days_col     = '#00762A' if days_int > 90 else ('#F58E1E' if days_int > 30 else '#EB1E23')

    # 7-day trend string
    if week_change is not None:
        arrow = '&uarr;' if week_change > 0.001 else ('&darr;' if week_change < -0.001 else '&rarr;')
        trend_str = f'{arrow} {abs(week_change):.3f}&nbsp;m past 7&nbsp;days'
    else:
        trend_str = ''

    date_str  = now_syd.strftime('%-d %B %Y')
    chart_html = _seven_day_chart(readings)

    # ── Zone reference rows ────────────────────────────────────────────────────
    zone_rows = ''
    for z in cfg['zone_thresholds']:
        is_cur  = (z['zone'] == z_num)
        row_bg  = _ROW_A if z['zone'] % 2 == 1 else _ROW_B
        weight  = '700' if is_cur else '400'
        marker  = '&nbsp;&nbsp;&#9664; current' if is_cur else ''
        t_str   = (f'&ge;&nbsp;{z["min_ahd"]:.3f}&nbsp;m&nbsp;AHD'
                   if z['min_ahd'] is not None else '&lt;&nbsp;189.650&nbsp;m&nbsp;AHD')
        pill    = (f'<span style="display:inline-block;background:{z["color_bg"]};'
                   f'color:{z["color_text"]};font-size:10px;font-weight:700;'
                   f'padding:2px 8px;border-radius:4px;">Zone&nbsp;{z["zone"]}</span>')
        zone_rows += f"""
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

    # ── Next zone info banner ──────────────────────────────────────────────────
    next_banner = ''
    if nxt and days not in (None, float('inf')):
        next_banner = f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:10px;">
  <tr>
    <td style="background:#fff8e1;padding:12px 20px;border-left:4px solid #F58E1E;">
      <p style="margin:0;font-family:Arial,sans-serif;font-size:12px;color:#78350f;">
        <strong>Next zone: Zone&nbsp;{nxt['zone']} — {nxt['name']}</strong>
        &nbsp;&bull;&nbsp; Threshold: {nxt['min_ahd']:.3f}&nbsp;m AHD
        &nbsp;&bull;&nbsp; Max extraction drops to <strong>{nxt['max_pump_ml_day']:.2f}&nbsp;ML/day</strong>
      </p>
    </td>
  </tr>
</table>"""

    body = (
        _header(f'Week ending {date_str}')

        # Zone badge
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    <td bgcolor="{z_bg}" style="background-color:{z_bg};padding:18px 24px;text-align:center;">
      <p style="margin:0 0 3px 0;font-family:Arial,sans-serif;font-size:10px;
          font-weight:700;color:{z_txt};letter-spacing:1.5px;text-transform:uppercase;
          opacity:0.75;">Current Licence Zone</p>
      <p style="margin:0;font-family:Arial,sans-serif;font-size:21px;font-weight:700;
          color:{z_txt};">Zone&nbsp;{z_num} &mdash; {z_name}</p>
      <p style="margin:6px 0 0 0;font-family:Arial,sans-serif;font-size:14px;
          color:{z_txt};opacity:0.9;">{ahd:.3f}&nbsp;m&nbsp;AHD
          {"&nbsp;&nbsp;" + trend_str if trend_str else ""}</p>
    </td>
  </tr>
</table>"""

        # Stats row
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:10px;">
  <tr>
    <td class="mob-stat" width="50%" bgcolor="{_ROW_A}"
        style="background-color:{_ROW_A};padding:18px 20px;border:1px solid {_BORDER};
               border-radius:0;vertical-align:top;">
      <p style="margin:0 0 4px 0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;
          color:#64748b;letter-spacing:0.8px;text-transform:uppercase;">Max Extraction Rate</p>
      <p style="margin:0;font-family:Arial,sans-serif;font-size:28px;font-weight:700;
          color:{_HDR_BG};">{z_pump:.2f}&nbsp;ML/day</p>
      <p style="margin:4px 0 0 0;font-family:Arial,sans-serif;font-size:12px;
          color:#64748b;">{z_pump * 1000:.0f}&nbsp;kL/day under current licence</p>
    </td>
    <td class="mob-stat" width="50%" bgcolor="{_ROW_B}"
        style="background-color:{_ROW_B};padding:18px 20px;border:1px solid {_BORDER};
               border-left:none;vertical-align:top;">
      <p style="margin:0 0 4px 0;font-family:Arial,sans-serif;font-size:10px;font-weight:700;
          color:#64748b;letter-spacing:0.8px;text-transform:uppercase;">Days to Next Zone</p>
      <p style="margin:0;font-family:Arial,sans-serif;font-size:28px;font-weight:700;
          color:{days_col};">{days_display}</p>
      <p style="margin:4px 0 0 0;font-family:Arial,sans-serif;font-size:12px;
          color:#64748b;">{days_sub}</p>
    </td>
  </tr>
</table>"""

        + next_banner

        # Chart
        + _section('Lake Level &mdash; Past 7 Days')
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    <td style="background:#0d1b2a;padding:12px;">
      {chart_html if chart_html else '<p style="color:#94a3b8;font-family:Arial;font-size:12px;margin:0;">No chart data available.</p>'}
    </td>
  </tr>
</table>"""

        # Zone reference
        + _section('Water Licence Zones')
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;">
  <tr>
    <th bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:8px 10px;
        font-family:Arial,sans-serif;font-size:11px;color:#ffffff;font-weight:700;
        text-align:left;border-right:1px solid {_BORDER};">Zone</th>
    <th bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:8px 10px;
        font-family:Arial,sans-serif;font-size:11px;color:#ffffff;font-weight:700;
        text-align:left;border-right:1px solid {_BORDER};">Threshold</th>
    <th bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:8px 10px;
        font-family:Arial,sans-serif;font-size:11px;color:#ffffff;font-weight:700;
        text-align:right;">Max Extraction</th>
  </tr>
{zone_rows}
</table>"""

        # Disclaimer
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:14px;">
  <tr>
    <td style="padding:12px 20px;">
      <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:#64748b;
          line-height:1.6;">
        <strong>Projection methodology:</strong> Days-to-next-zone assumes pumping at the
        maximum current licence rate ({z_pump:.2f}&nbsp;ML/day) with <em>no rainfall</em> —
        a conservative planning figure. {mth_name} open-water evaporation based on
        BOM Wagga Wagga Airport long-term average
        ({pan_mm:.2f}&nbsp;mm/day pan &times;&nbsp;0.70&nbsp;lake factor
        = {lake_mm:.2f}&nbsp;mm/day = {evap_ml:.2f}&nbsp;ML/day at current lake area).
        Lake surface area adjusts as level changes. All figures are estimates only.
      </p>
    </td>
  </tr>
</table>"""

        # Footer
        + f"""
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="border-collapse:collapse;margin-top:4px;">
  <tr>
    <td bgcolor="{_HDR_BG}" style="background-color:{_HDR_BG};padding:14px 24px;text-align:center;">
      <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;
          color:rgba(255,255,255,0.45);">Wagga Wagga Country Club &middot; Lake Albert Irrigation System</p>
      <p style="margin:4px 0 0 0;font-family:Arial,sans-serif;font-size:10px;
          color:rgba(255,255,255,0.3);">Data sourced from on-site FarmBot sensor &middot; Updated weekly</p>
    </td>
  </tr>
</table>"""
    )

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
        log.info(f'TEST MODE — sending only to {to_list[0]}')
    else:
        to_list  = [e.strip() for e in EMAIL_BOARD_TO.split(',')  if e.strip()]
        cc_list  = [e.strip() for e in EMAIL_BOARD_CC.split(',')  if e.strip()] if EMAIL_BOARD_CC  else []
        bcc_list = [e.strip() for e in EMAIL_BOARD_BCC.split(',') if e.strip()] if EMAIL_BOARD_BCC else []

    if not to_list:
        log.error('No To recipients — cannot send')
        return False

    mail = Mail(from_email=Email(EMAIL_FROM), subject=subject, html_content=html_content)
    mail.to = [To(e) for e in to_list]
    if cc_list:
        mail.cc = [Cc(e) for e in cc_list]
    if bcc_list:
        mail.bcc = [Bcc(e) for e in bcc_list]

    try:
        resp = SendGridAPIClient(SENDGRID_API_KEY).send(mail)
        log.info(f'Sent "{subject}" — status {resp.status_code} — to: {", ".join(to_list)}')
        return True
    except Exception as e:
        log.error(f'SendGrid error: {e}')
        return False


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run',  action='store_true', help='Save HTML preview, do not send')
    parser.add_argument('--test',     action='store_true', help='Send to andrew@bidgeepumps.com.au only')
    parser.add_argument('--force',    action='store_true', help='Ignore dedup guard and send regardless')
    args = parser.parse_args()

    now_syd = datetime.now(SYDNEY_TZ)

    if not args.dry_run and not args.test and not args.force:
        if _already_sent_this_week(now_syd):
            log.info('Board email already sent this week — skipping (use --force to override)')
            return

    subject = f'Lake Albert Board Update — {now_syd.strftime("%-d %B %Y")}'
    html    = build_html(now_syd)

    if html is None:
        log.error('Could not build email — no lake data')
        return

    if args.dry_run:
        out = _DATA_DIR / 'reports' / f'board_preview_{now_syd.strftime("%Y-%m-%d")}.html'
        out.parent.mkdir(exist_ok=True)
        out.write_text(html, encoding='utf-8')
        log.info(f'Dry run — saved to {out}')
        return

    sent = send_email(subject, html, test_mode=args.test)

    if sent and not args.test:
        _mark_sent_this_week(now_syd)
        log.info('Sent-this-week guard updated')


if __name__ == '__main__':
    main()
