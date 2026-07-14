#!/usr/bin/env python3
"""
Lake Level Alert
================
Detects when the lake pump rate zone changes and sends an alert email
to the GK and committee mailing list.

Runs after each FarmBot poll via farmbot-poll.yml.

Zone state file: data/lake_pump_zone.json
  {
    "zone": 2, "rate": "1.00 ML/day", "ahd": 190.123,
    "changed_at": "2026-07-14T07:32:00+10:00",
    "last_alert_at": "2026-07-14T07:32:00+00:00"
  }

On first run (no zone file) the current zone is recorded and no email is sent.
"""

import csv, json, os, sys, logging
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

SYDNEY_TZ = ZoneInfo('Australia/Sydney')
DATA_DIR  = Path(__file__).parent.parent / 'data'
ZONE_FILE        = DATA_DIR / 'lake_pump_zone.json'
ZONE_HISTORY_CSV = DATA_DIR / 'lake_zone_history.csv'

_HISTORY_HEADERS = [
    'timestamp_aest', 'old_zone', 'new_zone', 'old_rate', 'new_rate',
    'ahd', 'event', 'email_sent', 'recipients',
]

_WHITE_LOGO_URL = 'https://bidgee182.github.io/wwcc-weather-page/assets/images/logo-white.png'


def _white_logo_html():
    return (f'<img src="{_WHITE_LOGO_URL}" width="194" height="44" alt="Wagga Wagga Country Club"'
            f' style="display:block;border:0;">')

SENDGRID_API_KEY          = os.environ.get('SENDGRID_API_KEY', '')
EMAIL_FROM                = os.environ.get('EMAIL_FROM', '')
EMAIL_GK_RECIPIENTS       = [a.strip() for a in os.environ.get('EMAIL_GK_RECIPIENTS', '').split(',') if a.strip()]
EMAIL_COMMITTEE_RECIPIENTS = [a.strip() for a in os.environ.get('EMAIL_COMMITTEE_RECIPIENTS', '').split(',') if a.strip()]
EMAIL_RECIPIENTS_ALL      = EMAIL_GK_RECIPIENTS + EMAIL_COMMITTEE_RECIPIENTS

# Level definitions — must match _LAKE_LEVELS in daily_report.py
# (min_ahd, level_num, rate_str, bg_hex, fg_hex)
LAKE_LEVELS = [
    (190.250, 1, '1.50 ML/day', '#00762A', '#ffffff'),
    (190.050, 2, '1.00 ML/day', '#8AC63F', '#111111'),
    (189.850, 3, '0.75 ML/day', '#FFDD00', '#111111'),
    (189.650, 4, '0.50 ML/day', '#F58E1E', '#111111'),
    (0,       5, '0 ML/day',    '#EB1E23', '#ffffff'),
]

# Threshold below which pumping must cease
CEASE_AHD = 189.650

# Hysteresis deadband: lake must move this far past a threshold before a zone
# change is registered. Prevents alert spam when the level oscillates at a boundary.
# Zone 5 (cease/resume pumping) transitions always bypass this — compliance-critical.
HYSTERESIS_M = 0.01   # 10 mm

# Minimum hours between non-critical zone change alerts.
# Cease (->Zone 5) and resume (from Zone 5) always send immediately.
MIN_ALERT_HOURS = 12

# Pre-built lookup: zone_num -> (min_ahd, rate, bg, fg)
_ZONE_INFO = {n: (m, r, bg, fg) for m, n, r, bg, fg in LAKE_LEVELS}


def _level_info_raw(ahd):
    """Return (num, rate, bg, fg) with no hysteresis — used for first-run init and test mode."""
    for min_ahd, num, rate, bg, fg in LAKE_LEVELS:
        if ahd >= min_ahd:
            return num, rate, bg, fg
    return LAKE_LEVELS[-1][1:]


def _level_info(ahd, current_zone=None):
    """Return (num, rate, bg, fg) applying a HYSTERESIS_M deadband when current_zone is given.

    - Dropping to a worse zone: AHD must be HYSTERESIS_M below the current zone's lower boundary.
    - Rising to a better zone: AHD must be HYSTERESIS_M above the better zone's lower boundary.
    - Zone 5 (cease/resume) transitions always use raw thresholds — compliance-critical.
    """
    raw_num, raw_rate, raw_bg, raw_fg = _level_info_raw(ahd)

    if current_zone is None or raw_num == current_zone:
        return raw_num, raw_rate, raw_bg, raw_fg

    # Never buffer cease or resume transitions — they are licence compliance events
    if raw_num == 5 or current_zone == 5:
        return raw_num, raw_rate, raw_bg, raw_fg

    cur_min, cur_rate, cur_bg, cur_fg = _ZONE_INFO[current_zone]

    if raw_num > current_zone:
        # Dropping to worse zone: only change if HYSTERESIS_M below current zone's floor
        if ahd > cur_min - HYSTERESIS_M:
            return current_zone, cur_rate, cur_bg, cur_fg
    else:
        # Rising to better zone: only change if HYSTERESIS_M above the better zone's floor
        new_min = _ZONE_INFO[raw_num][0]
        if ahd < new_min + HYSTERESIS_M:
            return current_zone, cur_rate, cur_bg, cur_fg

    return raw_num, raw_rate, raw_bg, raw_fg


def _load_current_ahd():
    try:
        with open(DATA_DIR / 'farmbot_lake_latest.json') as f:
            data = json.load(f)
        ahd = data.get('lake_ahd')
        return float(ahd) if ahd is not None else None
    except Exception as e:
        log.warning(f'Could not load lake data: {e}')
        return None


def _load_zone():
    try:
        with open(ZONE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _save_zone(zone_num, rate, ahd, changed_at=None, last_alert_at=None):
    try:
        data = {'zone': zone_num, 'rate': rate, 'ahd': round(ahd, 3)}
        if changed_at is not None:
            data['changed_at'] = changed_at.isoformat()
        if last_alert_at is not None:
            data['last_alert_at'] = last_alert_at.replace(microsecond=0).isoformat()
        ZONE_FILE.write_text(json.dumps(data, indent=2), encoding='utf-8')
    except Exception as e:
        log.warning(f'Could not save zone file: {e}')


def _append_history(now_syd, old_zone, new_zone, old_rate, new_rate, ahd, event, email_sent, recipients):
    """Append one row to the zone history CSV. Creates file with headers if needed."""
    try:
        write_header = not ZONE_HISTORY_CSV.exists() or ZONE_HISTORY_CSV.stat().st_size == 0
        with ZONE_HISTORY_CSV.open('a', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(_HISTORY_HEADERS)
            w.writerow([
                now_syd.strftime('%Y-%m-%d %H:%M:%S'),
                old_zone if old_zone is not None else '',
                new_zone,
                old_rate or '',
                new_rate or '',
                f'{ahd:.3f}' if ahd is not None else '',
                event,
                'true' if email_sent else 'false',
                '; '.join(recipients) if recipients else '',
            ])
    except Exception as e:
        log.warning(f'Could not write zone history: {e}')


def _lake_chart_html(days=14):
    """QuickChart image of the last N days of daily-average lake AHD with
    zone threshold lines. Returns '' on any failure - the alert must never
    be blocked by chart trouble."""
    try:
        import urllib.parse as _up
        from collections import defaultdict
        readings = json.loads((DATA_DIR / 'farmbot_lake_readings.json').read_text(encoding='utf-8'))
        by_day = defaultdict(list)
        for r in readings:
            if r.get('date') and r.get('ahd') is not None:
                by_day[r['date'][:10]].append(float(r['ahd']))
        day_keys = sorted(by_day)[-days:]
        if len(day_keys) < 2:
            return ''
        vals   = [round(sum(by_day[d]) / len(by_day[d]), 3) for d in day_keys]
        labels = [datetime.strptime(d, '%Y-%m-%d').strftime('%d %b') for d in day_keys]
        ymin, ymax = round(min(vals) - 0.05, 3), round(max(vals) + 0.05, 3)

        datasets = [{
            'label': 'Lake Level', 'data': vals, 'borderColor': '#1abc9c',
            'backgroundColor': 'rgba(26,188,156,0.12)', 'fill': True,
            'lineTension': 0.3, 'pointRadius': 0, 'borderWidth': 2,
        }]
        for min_ahd, num, _, bg, _fg in LAKE_LEVELS:
            if num == 5:
                continue
            col = '#EB1E23' if min_ahd == CEASE_AHD else bg
            if ymin - 0.1 <= min_ahd <= ymax + 0.1:
                datasets.append({'data': [min_ahd] * len(day_keys), 'borderColor': col,
                                 'borderWidth': 1, 'borderDash': [5, 4], 'fill': False,
                                 'pointRadius': 0, 'lineTension': 0})
        config = {
            'type': 'line',
            'data': {'labels': labels, 'datasets': datasets},
            'options': {
                'legend': {'display': False},
                'scales': {
                    'xAxes': [{'gridLines': {'color': '#e2e8e4'},
                               'ticks': {'fontColor': '#64748b', 'maxRotation': 0}}],
                    'yAxes': [{'gridLines': {'color': '#e2e8e4'},
                               'ticks': {'fontColor': '#64748b', 'min': ymin, 'max': ymax}}],
                },
            },
        }
        cfg_json = json.dumps(config, separators=(',', ':'))
        url = f'https://quickchart.io/chart?bkg=white&w=552&h=180&c={_up.quote(cfg_json)}'
        return (
            f'<tr><td style="background:white;padding:0 24px 20px;">'
            f'<p style="margin:0 0 6px 0;font-family:Arial,sans-serif;font-size:12px;'
            f'font-weight:700;color:#1a4a2e;">Lake level - last {len(day_keys)} days</p>'
            f'<img src="{url}" width="100%" alt="Lake level chart - last {len(day_keys)} days"'
            f' style="display:block;border:1px solid #e2e8e4;border-radius:8px;max-width:100%;">'
            f'</td></tr>'
        )
    except Exception as e:
        log.warning(f'Could not build alert chart: {e}')
        return ''


def _build_email(ahd, new_num, new_rate, new_bg, new_fg, old_num, old_rate, now_syd,
                 days_in_prev=None):
    rising   = new_num < old_num
    ceasing  = new_num == 5
    resuming = old_num == 5

    if ceasing:
        icon = '&#9888;'
        body_text = (
            f'The lake level at Lake Albert has fallen to <strong>{ahd:.3f}m AHD</strong>, '
            f'entering <strong>Level {new_num}</strong>. Pumping must cease immediately in '
            f'accordance with licence conditions. Do not resume pumping until the lake level '
            f'recovers above <strong>{CEASE_AHD}m AHD</strong>.'
        )
    elif resuming:
        icon = '&#9650;'
        body_text = (
            f'The lake level at Lake Albert has risen to <strong>{ahd:.3f}m AHD</strong>, '
            f'moving into <strong>Level {new_num}</strong>. Pumping may resume at a permitted '
            f'rate of <strong>{new_rate}</strong>.'
        )
    elif rising:
        icon = '&#9650;'
        body_text = (
            f'The lake level at Lake Albert has risen to <strong>{ahd:.3f}m AHD</strong>, '
            f'moving into <strong>Level {new_num}</strong>. The permitted pumping rate has '
            f'increased from <strong>{old_rate}</strong> to <strong>{new_rate}</strong> '
            f'effective immediately.'
        )
    else:
        icon = '&#9660;'
        body_text = (
            f'The lake level at Lake Albert has fallen to <strong>{ahd:.3f}m AHD</strong>, '
            f'moving into <strong>Level {new_num}</strong>. The permitted pumping rate has '
            f'decreased from <strong>{old_rate}</strong> to <strong>{new_rate}</strong> '
            f'effective immediately. Please ensure pumping operations are adjusted accordingly.'
        )

    now_str = f"{now_syd.day} {now_syd.strftime('%b %Y %H:%M')}"

    BORDER = '#e2e8e4'
    ROW_A  = '#f8f8f8'
    ROW_B  = '#ffffff'

    def kv_row(label, value, i):
        bg = ROW_A if i % 2 == 0 else ROW_B
        return (
            f'<tr>'
            f'<td bgcolor="{bg}" style="background-color:{bg};padding:9px 16px;width:180px;'
            f'font-family:Arial,sans-serif;font-size:13px;font-weight:700;color:#1a4a2e;'
            f'border-bottom:1px solid {BORDER};">{label}</td>'
            f'<td bgcolor="{bg}" style="background-color:{bg};padding:9px 16px;'
            f'font-family:Arial,sans-serif;font-size:13px;color:#111827;'
            f'border-bottom:1px solid {BORDER};">{value}</td>'
            f'</tr>'
        )

    table_rows = (
        kv_row('Current Lake Level', f'{ahd:.3f}m AHD', 0) +
        kv_row('New Zone',           f'Level {new_num}', 1) +
        kv_row('Permitted Rate',     new_rate, 2) +
        kv_row('Previous Rate',      old_rate, 3)
    )
    row_i = 4
    if days_in_prev is not None:
        dur = 'less than 1 day' if days_in_prev < 1 else (
              '1 day' if days_in_prev == 1 else f'{days_in_prev} days')
        table_rows += kv_row('Time at Previous Level', dur, row_i)
        row_i += 1
    table_rows += kv_row('As at', f'{now_str} AEST', row_i)

    chart_html = _lake_chart_html()

    dashboard_html = (
        '<tr><td style="background:white;padding:0 24px 24px;" align="center">'
        '<a href="https://bidgee182.github.io/wwcc-weather-page/board.html"'
        ' style="display:inline-block;background:#1a4a2e;color:#ffffff;'
        'font-family:Arial,sans-serif;font-size:13px;font-weight:700;'
        'text-decoration:none;padding:11px 26px;border-radius:8px;">'
        'View Live Lake Dashboard</a></td></tr>'
    )

    _mob_style = """\
<style type="text/css">
@media only screen and (max-width:620px) {
  table[width="600"] { width:100% !important; max-width:100% !important; }
  .mob-logo-cell { display:block !important; width:100% !important; text-align:center !important; padding-right:0 !important; padding-bottom:14px !important; }
  .mob-logo-cell img { height:44px !important; width:auto !important; max-width:100% !important; }
  .mob-text-cell { display:block !important; width:100% !important; }
}
</style>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{_mob_style}
</head>
<body style="margin:0;padding:0;background:#f0f4f8;font-family:Arial,Helvetica,sans-serif;">
{_mob_style}
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
              text-transform:uppercase;font-family:Arial,sans-serif;">WAGGA WAGGA COUNTRY CLUB</p>
          <h1 style="margin:8px 0 0 0;font-size:22px;color:#ffffff;font-weight:bold;
              font-family:Arial,sans-serif;">Lake Level Alert</h1>
          <p style="margin:6px 0 0 0;font-size:13px;color:#a8d8bc;font-family:Arial,sans-serif;">
            Pumping rate change &nbsp;&bull;&nbsp; {now_str} AEST</p>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- ZONE COLOUR BANNER -->
  <tr><td bgcolor="{new_bg}" style="background-color:{new_bg};padding:14px 20px;text-align:center;">
    <p style="margin:0;font-size:15px;color:{new_fg};font-weight:bold;
        letter-spacing:0.5px;font-family:Arial,sans-serif;">
      {icon} &nbsp;LAKE LEVEL ALERT &nbsp;&bull;&nbsp; PUMPING RATE CHANGE
    </p>
  </td></tr>

  <!-- BODY TEXT -->
  <tr><td style="background:white;padding:20px 24px 16px;">
    <p style="margin:0;font-family:Arial,sans-serif;font-size:14px;color:#111827;line-height:1.6;">
      {body_text}
    </p>
  </td></tr>

  <!-- DETAIL TABLE -->
  <tr><td style="background:white;padding:0 24px 24px;">
    <table width="100%" cellpadding="0" cellspacing="0"
        style="border-collapse:collapse;border:1px solid {BORDER};border-radius:8px;overflow:hidden;">
      {table_rows}
    </table>
  </td></tr>

  <!-- 14-DAY CHART -->
  {chart_html}

  <!-- DASHBOARD LINK -->
  {dashboard_html}

  <!-- FOOTER -->
  <tr><td bgcolor="#1a4a2e" style="background-color:#1a4a2e;padding:18px 28px;text-align:center;">
    <div style="font-size:10px;color:#6ee7b7;letter-spacing:2px;text-transform:uppercase;
        margin-bottom:6px;">Wagga Wagga Country Club - Automated Alert</div>
    <div style="font-size:11px;color:rgba(255,255,255,0.35);">
      Generated {now_str} AEST - do not reply to this email.</div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""

    if ceasing:
        subject = f'URGENT - Cease Pumping - Lake Level {ahd:.3f}m AHD'
    elif resuming:
        subject = f'Lake Level Alert - Pumping May Resume - Level {new_num} - {ahd:.3f}m AHD'
    elif rising:
        subject = f'Lake Level Alert - Pumping Rate Increased to {new_rate} - Level {new_num}'
    else:
        subject = f'Lake Level Alert - Pumping Rate Decreased to {new_rate} - Level {new_num}'

    return html, subject


def send_email(subject, html, recipients):
    if not SENDGRID_API_KEY:
        log.warning('No SENDGRID_API_KEY — skipping send.')
        return False
    if not recipients:
        log.warning('No recipients — skipping send.')
        return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, To, Email
        from lake_utils import html_to_text
        mail = Mail(from_email=Email(EMAIL_FROM), subject=subject,
                    plain_text_content=html_to_text(html), html_content=html)
        mail.to = [To(e) for e in recipients]
        sg   = SendGridAPIClient(SENDGRID_API_KEY)
        resp = sg.send(mail)
        log.info(f'Sent "{subject}" — {resp.status_code} — to {recipients}')
        return True
    except Exception as e:
        log.error(f'Send error: {e}')
        return False


def main():
    now_syd = datetime.now(tz=SYDNEY_TZ)
    ahd = _load_current_ahd()
    if ahd is None:
        log.info('No lake AHD available — skipping alert check.')
        return

    saved = _load_zone()

    if saved is None:
        # First run: initialise zone file silently without sending an alert
        new_num, new_rate, new_bg, new_fg = _level_info_raw(ahd)
        log.info(f'No zone file — initialising to Level {new_num} ({ahd:.3f}m AHD). No alert sent.')
        _save_zone(new_num, new_rate, ahd)
        _append_history(now_syd, None, new_num, '', new_rate, ahd, 'initialised', False, [])
        return

    old_num  = saved.get('zone')
    old_rate = saved.get('rate', '-')

    new_num, new_rate, new_bg, new_fg = _level_info(ahd, current_zone=old_num)

    if new_num == old_num:
        log.info(f'Zone unchanged: Level {new_num} ({ahd:.3f}m AHD) — no alert.')
        return

    # Determine event type for logging and email subject
    if new_num == 5:
        event_type = 'cease_pumping'
    elif old_num == 5:
        event_type = 'resume_pumping'
    else:
        event_type = 'zone_change'

    # Cease (->Zone 5) and resume (from Zone 5) always send immediately — no buffer
    is_critical = (new_num == 5 or old_num == 5)

    if not is_critical:
        last_alert_str = saved.get('last_alert_at')
        if last_alert_str:
            try:
                last_alert = datetime.fromisoformat(last_alert_str)
                if last_alert.tzinfo is None:
                    last_alert = last_alert.replace(tzinfo=timezone.utc)
                hours_since = (datetime.now(timezone.utc) - last_alert).total_seconds() / 3600
                if hours_since < MIN_ALERT_HOURS:
                    log.info(
                        f'Alert suppressed: {hours_since:.1f}h since last alert '
                        f'(min {MIN_ALERT_HOURS}h) — Level {old_num} -> {new_num} '
                        f'at {ahd:.3f}m AHD. Zone file unchanged; will retry next poll.'
                    )
                    _append_history(now_syd, old_num, new_num, old_rate, new_rate, ahd,
                                    'suppressed', False, [])
                    return  # Don't update zone file — preserves pending change for next check
            except Exception as e:
                log.warning(f'Could not parse last_alert_at: {e}')

    # Days the previous zone was in effect (from the zone file's changed_at)
    days_in_prev = None
    changed_str = saved.get('changed_at')
    if changed_str:
        try:
            changed_at = datetime.fromisoformat(changed_str)
            if changed_at.tzinfo is None:
                changed_at = changed_at.replace(tzinfo=SYDNEY_TZ)
            days_in_prev = max(0, (now_syd - changed_at).days)
        except Exception as e:
            log.warning(f'Could not parse changed_at: {e}')

    log.info(f'Zone changed: Level {old_num} -> Level {new_num} ({ahd:.3f}m AHD) — sending alert.')
    html, subject = _build_email(ahd, new_num, new_rate, new_bg, new_fg,
                                  old_num, old_rate, now_syd,
                                  days_in_prev=days_in_prev)
    sent = send_email(subject, html, EMAIL_RECIPIENTS_ALL)
    if sent:
        _save_zone(new_num, new_rate, ahd,
                   changed_at=now_syd,
                   last_alert_at=datetime.now(timezone.utc))
        _append_history(now_syd, old_num, new_num, old_rate, new_rate, ahd,
                        event_type, True, EMAIL_RECIPIENTS_ALL)
    else:
        log.warning('Email failed — zone file not updated; will retry on next poll.')
        _append_history(now_syd, old_num, new_num, old_rate, new_rate, ahd,
                        event_type, False, EMAIL_RECIPIENTS_ALL)


if __name__ == '__main__':
    args = sys.argv[1:]
    if '--test' in args:
        # Simulate a zone change using the current lake level.
        # Pretends the previous zone was one step lower (falling scenario).
        now_syd = datetime.now(tz=SYDNEY_TZ)
        ahd = _load_current_ahd()
        if ahd is None:
            log.error('No lake AHD data available for test.')
            sys.exit(1)
        new_num, new_rate, new_bg, new_fg = _level_info_raw(ahd)
        old_num  = max(1, new_num - 1)   # simulate coming from one level better
        old_rate = next((r for _, n, r, _, _ in LAKE_LEVELS if n == old_num), '-')
        log.info(f'[TEST] Simulating zone change Level {old_num} -> Level {new_num} at {ahd:.3f}m AHD')
        html, subject = _build_email(ahd, new_num, new_rate, new_bg, new_fg,
                                      old_num, old_rate, now_syd, days_in_prev=12)
        send_email(f'[TEST] {subject}', html, ['andrew@bidgeepumps.com.au'])
        log.info('Test alert sent to andrew@bidgeepumps.com.au')
    else:
        main()
