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

import json, os, sys, logging
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
ZONE_FILE = DATA_DIR / 'lake_pump_zone.json'

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


def _build_email(ahd, new_num, new_rate, new_bg, new_fg, old_num, old_rate, now_syd):
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
        kv_row('Previous Rate',      old_rate, 3) +
        kv_row('As at',              f'{now_str} AEST', 4)
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
  <tr><td style="background:white;padding:0 24px 28px;">
    <table width="100%" cellpadding="0" cellspacing="0"
        style="border-collapse:collapse;border:1px solid {BORDER};border-radius:8px;overflow:hidden;">
      {table_rows}
    </table>
  </td></tr>

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
        mail = Mail(from_email=Email(EMAIL_FROM), subject=subject, html_content=html)
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
        return

    old_num  = saved.get('zone')
    old_rate = saved.get('rate', '-')

    new_num, new_rate, new_bg, new_fg = _level_info(ahd, current_zone=old_num)

    if new_num == old_num:
        log.info(f'Zone unchanged: Level {new_num} ({ahd:.3f}m AHD) — no alert.')
        return

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
                    return  # Don't update zone file — preserves pending change for next check
            except Exception as e:
                log.warning(f'Could not parse last_alert_at: {e}')

    log.info(f'Zone changed: Level {old_num} -> Level {new_num} ({ahd:.3f}m AHD) — sending alert.')
    html, subject = _build_email(ahd, new_num, new_rate, new_bg, new_fg,
                                  old_num, old_rate, now_syd)
    sent = send_email(subject, html, EMAIL_RECIPIENTS_ALL)
    if sent:
        _save_zone(new_num, new_rate, ahd,
                   changed_at=now_syd,
                   last_alert_at=datetime.now(timezone.utc))
    else:
        log.warning('Email failed — zone file not updated; will retry on next poll.')


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
                                      old_num, old_rate, now_syd)
        send_email(f'[TEST] {subject}', html, ['andrew@bidgeepumps.com.au'])
        log.info('Test alert sent to andrew@bidgeepumps.com.au')
    else:
        main()
