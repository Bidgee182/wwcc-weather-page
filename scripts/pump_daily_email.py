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
import csv, json, logging, os, re, sys
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
HISTORY_FILE    = DATA_DIR / 'pump_station_history.json'
WATER_USAGE_FILE = DATA_DIR / 'water_usage.json'
DAILY_LOG_FILE  = DATA_DIR / 'daily_log.csv'

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


def real_alarm_events(events):
    """Keep only genuine alarms, dropping routine Low-Flow Stop/Boost operation
    (source 'SystemFunction'), which floods the log (often 170+ of the last 200
    events) and buried the real alarms in the summary. The poller logs each real
    alarm twice (once as 'Pump', once as 'System'), so collapse duplicates by
    (second-resolution timestamp, code, type_code)."""
    out, seen = [], set()
    for e in events:
        if e.get('source_name') == 'SystemFunction':
            continue
        k = (str(e.get('timestamp_iso', ''))[:19], e.get('code'), e.get('type_code'))
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def active_alarms_from_latest(latest):
    """Currently-active alarms/faults from the live snapshot - the same source the
    admin health card reads, so an ongoing fault shows even when its onset predates
    the reporting period. The same alarm is often reported both as a system alarm
    and as a pump fault, so merge by code into one line, keeping the most specific
    pump label and the real description."""
    by_code = {}

    def _add(code, pump, desc):
        pump = pump or None
        desc = desc if (desc and not str(desc).startswith('Alarm code') and desc != 'Fault active') else None
        cur = by_code.get(code)
        if cur is None:
            by_code[code] = {'code': code, 'pump_label': pump or 'System',
                             'description': desc or (f'Alarm code {code}' if code else 'Fault active')}
        else:
            if pump and pump != 'System' and cur['pump_label'] in (None, 'System', 'Pump'):
                cur['pump_label'] = pump
            if desc and (cur['description'].startswith('Alarm code') or cur['description'] == 'Fault active'):
                cur['description'] = desc

    for a in (latest.get('alarms') or []):
        if a.get('active'):
            _add(a.get('code'), a.get('pump'), a.get('description'))
    for p in (latest.get('pumps') or []):
        if p.get('fault'):
            _add(p.get('alarm_code'), p.get('label'), p.get('alarm_desc'))
    return list(by_code.values())


def water_pumped(d_from, d_to):
    """Irrigation water drawn in [d_from, d_to], split by source:
      lake_ml - WaterNSW-metered dam extraction (corrected meter, ML)
      tank_l  - tank drawdown from the daily log's tank_used_l (litres)
    1 ML = 1,000,000 L, so total_ml = lake_ml + tank_l/1e6."""
    f, t = d_from.isoformat(), d_to.isoformat()
    wu = load_json(WATER_USAGE_FILE, {})
    lake_ml = round(sum(ml for d, ml in (wu.get('daily') or []) if f <= d <= t), 3)
    tank_l = 0.0
    try:
        with open(DAILY_LOG_FILE, encoding='utf-8') as fh:
            for r in csv.DictReader(fh):
                d = (r.get('date') or '')[:10]
                if f <= d <= t:
                    try:
                        tank_l += float(r.get('tank_used_l') or 0)
                    except ValueError:
                        pass
    except Exception:
        pass
    return lake_ml, tank_l


def _hist_readings(d_from, d_to):
    """pump_station_history readings whose Sydney date is in [d_from, d_to],
    returned as (sydney_datetime, reading) and oldest-first."""
    h = load_json(HISTORY_FILE, {})
    rows = h.get('readings') if isinstance(h, dict) else (h or [])
    f, t = d_from.isoformat(), d_to.isoformat()
    out = []
    for r in rows:
        try:
            dt = datetime.fromisoformat(str(r['ts']).replace('Z', '+00:00')).astimezone(SYDNEY_TZ)
        except Exception:
            continue
        if f <= dt.date().isoformat() <= t:
            out.append((dt, r))
    out.sort(key=lambda x: x[0])
    return out


def overnight_pressure(readings):
    """Average discharge pressure vs setpoint during night hours (19:00-07:00
    Sydney, when the course irrigates) while at least one pump is running."""
    pa, ps = [], []
    for dt, r in readings:
        night = dt.hour >= 19 or dt.hour < 7
        if night and (r.get('nr') or 0) > 0:
            if r.get('pa') is not None:
                pa.append(r['pa'])
            if r.get('ps') is not None:
                ps.append(r['ps'])
    if not pa:
        return None
    avg_pa = sum(pa) / len(pa)
    avg_ps = (sum(ps) / len(ps)) if ps else None
    return {'avg_pa': round(avg_pa, 2),
            'avg_ps': round(avg_ps, 2) if avg_ps is not None else None,
            'n': len(pa),
            'deficit': round(avg_ps - avg_pa, 2) if avg_ps is not None else None}


def gather_metrics(d_from, d_to, latest, raw_events, alarms_period, active_now, days):
    """All the health + water figures for the period, plus an overall verdict."""
    f, t = d_from.isoformat(), d_to.isoformat()
    lake_ml, tank_l = water_pumped(d_from, d_to)
    total_ml = round(lake_ml + tank_l / 1e6, 3)

    readings = _hist_readings(d_from, d_to)
    press = overnight_pressure(readings)

    # Per-pump run hours and energy summed from the daily rollups (per-day deltas,
    # same source as the run-hours table).
    run_hours = [0.0, 0.0, 0.0, 0.0]
    kwh = 0.0
    for d in days:
        if not (f <= (d.get('date') or '') <= t):
            continue
        for i, h in enumerate((d.get('hrs') or [])[:4]):
            run_hours[i] += h or 0
        kwh += sum(v for v in (d.get('kwh') or []) if v)
    run_hours = [round(x, 1) for x in run_hours]
    kwh = round(kwh)

    # Starts per pump: st[] is a cumulative "starts today" meter that resets at
    # midnight, so each Sydney day's total is the day's MAX; sum those per pump
    # (the daily rollup's sts field is not populated).
    daymax = {}
    for dt, r in readings:
        st = r.get('st')
        if not st:
            continue
        cur = daymax.setdefault(dt.date().isoformat(), [0, 0, 0, 0])
        for i in range(min(4, len(st))):
            if st[i] is not None and st[i] > cur[i]:
                cur[i] = st[i]
    starts = [0, 0, 0, 0]
    for v in daymax.values():
        for i in range(4):
            starts[i] += v[i]
    kwh_per_ml = round(kwh / lake_ml) if lake_ml > 0 else None

    # Low-flow stops (SystemFunction 'activated') - frequent cycling can mean a
    # leak or zero-demand running; counted from the RAW events before filtering.
    low_flow = 0
    for e in raw_events:
        if e.get('source_name') == 'SystemFunction' and e.get('type_code') == 3 \
                and 'Stop' in (e.get('type_name') or ''):
            try:
                dt = datetime.fromisoformat(e['timestamp_iso'].replace('Z', '+00:00')).astimezone(SYDNEY_TZ)
            except Exception:
                continue
            if f <= dt.date().isoformat() <= t:
                low_flow += 1

    alarms_count = sum(1 for e in alarms_period if e.get('type_code') == 1)
    total_starts = sum(starts)

    # Overall verdict: struggling if a fault is active, pressure is well under
    # setpoint, or alarms are many; watch for milder signs. Low-flow stops and
    # starts are normal operation, so they only flag at high per-day rates.
    days_n = max((d_to - d_from).days + 1, 1)
    bad, warn = [], []
    if active_now:
        bad.append(f"{len(active_now)} active fault(s)")
    if press and press.get('deficit') is not None and press['deficit'] >= 0.5:
        bad.append(f"overnight pressure {press['deficit']:.1f} bar under setpoint")
    elif press and press.get('deficit') is not None and press['deficit'] >= 0.25:
        warn.append("overnight pressure slightly under setpoint")
    if alarms_count >= 8:
        bad.append(f"{alarms_count} alarms")
    elif alarms_count >= 3:
        warn.append(f"{alarms_count} alarms")
    if low_flow / days_n >= 8:
        warn.append(f"{low_flow} low-flow stops (frequent cycling)")
    if bad:
        verdict = ('struggling', '#c0392b', 'Needs attention: ' + '; '.join(bad + warn))
    elif warn:
        verdict = ('watch', '#e67e22', 'Mostly OK, worth a look: ' + '; '.join(warn))
    else:
        verdict = ('healthy', '#27ae60', 'Running normally - pressure held and no significant alarms.')

    return {
        'lake_ml': lake_ml, 'tank_l': tank_l, 'total_ml': total_ml,
        'press': press, 'run_hours': run_hours, 'starts': starts, 'total_starts': total_starts,
        'kwh': kwh, 'kwh_per_ml': kwh_per_ml, 'low_flow': low_flow,
        'alarms_count': alarms_count, 'active_count': len(active_now),
        'setpoint': (latest.get('system') or {}).get('pressure_setpoint_bar'),
        'verdict': verdict,
    }


def _metrics_html(m):
    """Render the Water / Pressure / Health section from gather_metrics()."""
    v_name, v_col, v_text = m['verdict']
    tank_kl = m['tank_l'] / 1000.0

    def tile(label, value, sub):
        return (f'<td width="33%" style="padding:10px 14px;vertical-align:top;'
                f'border:1px solid #eee;">'
                f'<div style="font-family:Arial,sans-serif;font-size:11px;color:#888;'
                f'text-transform:uppercase;letter-spacing:.4px;">{label}</div>'
                f'<div style="font-family:Arial,sans-serif;font-size:19px;font-weight:bold;'
                f'color:#003366;margin:3px 0 1px;">{value}</div>'
                f'<div style="font-family:Arial,sans-serif;font-size:11px;color:#999;">{sub}</div></td>')

    # Pressure tile
    if m['press']:
        p = m['press']
        if p['avg_ps'] is not None:
            pcol = '#27ae60' if (p['deficit'] or 0) < 0.25 else ('#e67e22' if p['deficit'] < 0.5 else '#c0392b')
            press_val = f'<span style="color:{pcol};">{p["avg_pa"]:.2f} bar</span>'
            press_sub = f'setpoint {p["avg_ps"]:.2f} bar ({"+" if -p["deficit"]>=0 else ""}{-p["deficit"]:.2f})'
        else:
            press_val = f'{p["avg_pa"]:.2f} bar'
            press_sub = 'setpoint n/a'
    else:
        press_val, press_sub = '-', 'no overnight pumping recorded'

    # Water tiles
    water = (
        '<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;"><tr>'
        + tile('Water Pumped (total)', f'{m["total_ml"]:.2f} ML', 'lake meter + tank')
        + tile('From Lake (metered)', f'{m["lake_ml"]:.2f} ML', 'WaterNSW compliance meter')
        + tile('From Tank', f'{tank_kl:,.0f} kL', f'{m["tank_l"]/1e6:.3f} ML equivalent')
        + '</tr></table>')

    # Performance tiles
    eff_sub = (f'{m["kwh_per_ml"]:,} kWh per ML' if m['kwh_per_ml'] else 'no metered flow')
    perf = (
        '<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin-top:6px;"><tr>'
        + tile('Overnight Pressure', press_val, press_sub)
        + tile('Energy Used', f'{m["kwh"]:,} kWh', eff_sub)
        + tile('Pump Starts', f'{m["total_starts"]:,}', f'{m["alarms_count"]} alarm(s), {m["low_flow"]} low-flow stops')
        + '</tr></table>')

    # Run-hour split
    rh = ''
    if m['run_hours'] and any(m['run_hours']):
        parts = []
        for i, h in enumerate(m['run_hours']):
            parts.append(f'{PUMP_LABELS[i]} {h:.1f} h')
        rh = ('<div style="font-family:Arial,sans-serif;font-size:12px;color:#666;'
              'padding:8px 2px 0;">Run hours this period: ' + ' &middot; '.join(parts) + '</div>')

    verdict_box = (
        f'<div style="margin:0 0 10px;padding:10px 14px;border-radius:4px;'
        f'background:{v_col}14;border-left:4px solid {v_col};">'
        f'<span style="font-family:Arial,sans-serif;font-size:14px;font-weight:bold;color:{v_col};">'
        f'Pump status: {v_name.upper()}</span>'
        f'<span style="font-family:Arial,sans-serif;font-size:12px;color:#555;"> - {v_text}</span></div>')

    return (
        '<table width="100%" cellpadding="0" cellspacing="0">'
        f'{_section_heading("Water & Pump Health")}'
        '<tr><td style="padding:0 32px 4px;">'
        f'{verdict_box}{water}{perf}{rh}'
        '</td></tr></table>')


def build_html(day_data, latest, period_label, alarms_24h, active_now=None, metrics=None):
    date_label = period_label
    active_now = active_now or []
    metrics_html = _metrics_html(metrics) if metrics else ''

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

    TH = ('padding:9px 16px;font-family:Arial,sans-serif;font-size:13px;'
          'font-weight:bold;color:#fff;background:#003366;')

    # Currently-active alarms banner (ongoing faults, from the live snapshot)
    active_html = ''
    if active_now:
        arows = ''
        for a in active_now:
            code = a.get('code')
            code_str = f' (code {code})' if code else ''
            arows += (f'<tr><td style="padding:7px 16px;font-family:Arial,sans-serif;'
                      f'font-size:13px;color:#c0392b;font-weight:bold;">{a.get("description","Fault")}{code_str}</td>'
                      f'<td style="padding:7px 16px;font-family:Arial,sans-serif;'
                      f'font-size:13px;color:#c0392b;">{a.get("pump_label","System")}</td></tr>')
        active_html = (
            '<table width="100%" cellpadding="0" cellspacing="0">'
            f'{_section_heading("Active Now")}'
            '<tr><td style="padding:0 32px 4px;">'
            '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="border:1px solid #e0b4b4;border-radius:4px;background:#fdf3f3;">'
            f'<tr><td style="{TH}background:#c0392b;">Active Alarm / Fault</td>'
            f'<td style="{TH}background:#c0392b;">Source</td></tr>'
            f'{arows}</table></td></tr></table>')

    # Alarm event table rows (real alarms only; Low-Flow churn already removed).
    # Show the DATE as well as the time since the period spans a week/month.
    alarm_rows = ''
    for i, a in enumerate(alarms_24h[:20]):
        ts = a.get('timestamp_iso', '')
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(SYDNEY_TZ)
            ts_str = dt.strftime('%a %-d %b, %-I:%M %p')
        except Exception:
            ts_str = ts[:16]
        t_code = a.get('type_code', 3)
        color  = '#c0392b' if t_code == 1 else '#e67e22'
        name   = a.get('type_name', 'Event')
        src    = a.get('pump_label', 'System')
        bg     = 'background:#f9f9f9;' if i % 2 == 0 else ''
        alarm_rows += (f'<tr style="{bg}">'
                       f'<td style="padding:7px 16px;font-family:Arial,sans-serif;'
                       f'font-size:13px;color:#666;white-space:nowrap;">{ts_str}</td>'
                       f'<td style="padding:7px 16px;font-family:Arial,sans-serif;'
                       f'font-size:13px;color:{color};">{name}</td>'
                       f'<td style="padding:7px 16px;font-family:Arial,sans-serif;'
                       f'font-size:13px;">{src}</td></tr>')
    if not alarm_rows:
        msg = ('No alarms this period (routine Low-Flow Stop/Boost operation is not listed).'
               if not active_now else 'No new alarm events this period.')
        alarm_rows = ('<tr><td colspan="3" style="padding:10px 16px;'
                      f'font-family:Arial,sans-serif;font-size:14px;color:#999;">{msg}</td></tr>')

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

<!-- Water & Pump Health -->
{metrics_html}

<!-- Alarms -->
{active_html}
<table width="100%" cellpadding="0" cellspacing="0">
{_section_heading("Alarm Events This Period")}
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


def _alarms_in(all_events, d_from, d_to):
    """Real alarm events whose Sydney time falls in [d_from, d_to], oldest-first."""
    f = datetime(d_from.year, d_from.month, d_from.day, tzinfo=SYDNEY_TZ)
    t = datetime(d_to.year, d_to.month, d_to.day, tzinfo=SYDNEY_TZ) + timedelta(days=1)
    out = []
    for e in all_events:
        try:
            ts = datetime.fromisoformat(e['timestamp_iso'].replace('Z', '+00:00')).astimezone(SYDNEY_TZ)
            if f <= ts < t:
                out.append(e)
        except Exception:
            pass
    out.sort(key=lambda e: e.get('timestamp_iso', ''))
    return out


def build_period_html(d_from, d_to, period_label, days, latest, raw_events):
    """Assemble one pump summary (weekly or monthly) for [d_from, d_to]."""
    day_data, _ = aggregate_week(days, d_from, d_to)
    alarms = _alarms_in(real_alarm_events(raw_events), d_from, d_to)
    active_now = active_alarms_from_latest(latest)
    metrics = gather_metrics(d_from, d_to, latest, raw_events, alarms, active_now, days)
    return build_html(day_data or {}, latest, period_label, alarms, active_now, metrics)


def main():
    monthly = '--monthly' in sys.argv or os.environ.get('PUMP_MONTHLY', 'false').lower() == 'true'

    if not FORCE_SEND and not TEST_SEND and not daily_enabled():
        log.info('pump_daily disabled in email_config.json - skipping.')
        return

    now_syd = datetime.now(tz=SYDNEY_TZ)
    days       = load_json(DAILY_FILE, {}).get('days', [])
    latest     = load_json(LATEST_FILE, {})
    raw_events = (load_json(ALARM_FILE, {}) or {}).get('events', [])

    if monthly:
        d_to   = now_syd.date().replace(day=1) - timedelta(days=1)   # last day of prior month
        d_from = d_to.replace(day=1)                                 # first day of prior month
        period_label = d_from.strftime('%B %Y')
        subject = f'Pump Station Monthly Summary - {period_label}'
    else:
        d_to   = (now_syd - timedelta(days=1)).date()                # yesterday (Sunday)
        d_from = d_to - timedelta(days=6)                            # previous Monday
        if d_from.month == d_to.month:
            period_label = f"{d_from.strftime('%-d')} - {d_to.strftime('%-d %B %Y')}"
        else:
            period_label = f"{d_from.strftime('%-d %b')} - {d_to.strftime('%-d %b %Y')}"
        subject = f'Pump Station Weekly Summary - {period_label}'

    html = build_period_html(d_from, d_to, period_label, days, latest, raw_events)
    send_email(subject, html)


if __name__ == '__main__':
    main()
