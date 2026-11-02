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
import urllib.parse, urllib.request
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
EMAIL_LOG_FILE  = DATA_DIR / 'email_log.csv'
PUMP_STATE_FILE = DATA_DIR / 'pump_email_state.json'

SUPA_URL = os.environ.get('SUPABASE_URL') or 'https://sduzxijjvpbfgvlwcwpp.supabase.co'
SUPA_KEY = os.environ.get('SUPABASE_KEY') or (
    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'
    '.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNkdXp4aWpqdnBiZmd2bHdjd3BwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY1ODE2NzgsImV4cCI6MjA5MjE1NzY3OH0'
    '.fbYf9-F987DUSlsibuGnqGYEQe6tsQsOf7NMmNMrBT8')
# The greenkeeper mailbox - alarm "emailed?" status reflects alerts that reached it.
GREENKEEPER_ADDR = os.environ.get('PUMP_GK_ADDR', 'greenshed@wwcc.com.au').lower()

# Overnight pressure-dip thresholds (a small loss at the pump multiplies out to the
# last sprinkler, so overnight pressure matters). A "missed irrigation event" is a
# sustained collapse; a "dip" is a milder shortfall under the setpoint.
MISSED_BAR = 4.5     # pressure below this ...
MISSED_MIN = 5       # ... for more than this many minutes = a missed irrigation event
DIP_UNDER_BAR = 1.0  # more than this many bar under setpoint counts as a dip

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


def _supa(table, query):
    url = f'{SUPA_URL}/rest/v1/{table}?{query}'
    req = urllib.request.Request(url, headers={
        'apikey': SUPA_KEY, 'Authorization': f'Bearer {SUPA_KEY}', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b'[]')


def supa_alarm_events(d_from, d_to):
    """Real alarm transitions for the whole period from Supabase pump_events_hires,
    in the repo event schema, oldest-first. The repo pump_alarm_history.json keeps
    only ~200 events (about two weeks), so a monthly report needs this for full
    coverage. Returns [] on any failure (the caller falls back to the repo file)."""
    lo = datetime(d_from.year, d_from.month, d_from.day, tzinfo=SYDNEY_TZ).astimezone(timezone.utc)
    hi = (datetime(d_to.year, d_to.month, d_to.day, tzinfo=SYDNEY_TZ)
          + timedelta(days=1)).astimezone(timezone.utc)
    try:
        q = ('event_type=in.(alarm_on,pump_alarm_on,alarm_off,pump_alarm_off)'
             f'&ts=gte.{urllib.parse.quote(lo.isoformat())}'
             f'&ts=lt.{urllib.parse.quote(hi.isoformat())}'
             '&order=ts.asc&limit=3000')
        rows = _supa('pump_events_hires', q)
    except Exception as e:
        log.warning(f'Supabase alarm fetch failed: {e}')
        return []
    out, seen = [], set()
    for r in rows:
        # Pump-level alarms (Too many restarts, Limit exceeded) are logged as
        # pump_alarm_on, sometimes WITHOUT a matching alarm_on, so both are "appeared".
        appeared = r.get('event_type') in ('alarm_on', 'pump_alarm_on')
        label = r.get('pump_label') or 'System'
        ev = {
            'timestamp_iso': r.get('ts'),
            'code': r.get('alarm_code'),
            'type_code': 1 if appeared else 2,
            'type_name': 'Alarm appeared' if appeared else 'Alarm cleared',
            'pump_label': label,
            'description': r.get('alarm_desc') or '',
            'source_name': 'Pump' if str(label).startswith('P') else 'System',
        }
        k = (str(ev['timestamp_iso'])[:19], ev['code'], ev['type_code'])
        if k not in seen:
            seen.add(k)
            out.append(ev)
    return out


def supa_low_flow_count(d_from, d_to):
    """Low-Flow Stop activations in the period from Supabase (full coverage; the
    repo events file only spans ~2 weeks). Returns None on failure so the caller
    can fall back to the repo count."""
    lo = datetime(d_from.year, d_from.month, d_from.day, tzinfo=SYDNEY_TZ).astimezone(timezone.utc)
    hi = (datetime(d_to.year, d_to.month, d_to.day, tzinfo=SYDNEY_TZ)
          + timedelta(days=1)).astimezone(timezone.utc)
    try:
        q = ('select=ts&event_type=eq.func_low-flow_stop_on'
             f'&ts=gte.{urllib.parse.quote(lo.isoformat())}'
             f'&ts=lt.{urllib.parse.quote(hi.isoformat())}&limit=10000')
        return len(_supa('pump_events_hires', q))
    except Exception as e:
        log.warning(f'Supabase low-flow fetch failed: {e}')
        return None


def emailed_index():
    """Per-code list of (appeared_dt, emailed_at_iso) from pump_email_state.json -
    the record of which alarms triggered an alert email. Pump alerts always include
    the greenkeeper mailbox, so an alert emailed = emailed to the greenkeeper."""
    st = load_json(PUMP_STATE_FILE, {})
    idx = {}

    def add(code, appeared, emailed):
        if not emailed:
            return
        try:
            ad = datetime.fromisoformat(str(appeared).replace('Z', '+00:00'))
        except Exception:
            return
        idx.setdefault(str(code), []).append((ad, emailed))

    for h in st.get('alarm_email_history', []):
        add(h.get('code'), h.get('appeared_at'), h.get('appear_emailed_at'))
    for code, a in (st.get('active_alarms') or {}).items():
        add(code, a.get('appeared_at'), a.get('first_alerted_at'))
    return idx


def emailed_for(ev, idx):
    """The emailed-at timestamp for an alarm 'appeared' event, or None."""
    if ev.get('type_code') != 1:
        return None
    try:
        t = datetime.fromisoformat(str(ev['timestamp_iso']).replace('Z', '+00:00'))
    except Exception:
        return None
    for ad, em in idx.get(str(ev.get('code')), []):
        if abs((ad - t).total_seconds()) <= 120:
            return em
    return None


def overnight_pressure_by_day(readings):
    """Per-night discharge pressure (min/avg/max) while pumping, keyed to the night's
    start date: readings from 00:00-06:59 count toward the previous evening."""
    by_day = {}
    for dt, r in readings:
        night = dt.hour >= 19 or dt.hour < 7
        if night and (r.get('nr') or 0) > 0 and r.get('pa') is not None:
            nd = dt.date() if dt.hour >= 19 else (dt.date() - timedelta(days=1))
            by_day.setdefault(nd.isoformat(), []).append(r['pa'])
    rows = []
    for d in sorted(by_day):
        v = by_day[d]
        rows.append((d, round(min(v), 2), round(sum(v) / len(v), 2), round(max(v), 2)))
    return rows


def overnight_pressure_chart(readings, setpoint):
    """QuickChart.io PNG - each night's overnight pumping pressure (min/avg/max)
    across the period, with the setpoint line. Light theme to suit the email."""
    rows = overnight_pressure_by_day(readings)
    if len(rows) < 2:
        return ''
    labels = [f'{datetime.fromisoformat(d).day} {datetime.fromisoformat(d).strftime("%b")}'
              for d, _, _, _ in rows]
    mn = [r[1] for r in rows]
    av = [r[2] for r in rows]
    mx = [r[3] for r in rows]
    allv = mn + mx + ([setpoint] if setpoint else [])
    ymin = round(min(allv) - 0.2, 1)
    ymax = round(max(allv) + 0.2, 1)
    datasets = [
        {'label': 'Highest', 'data': mx, 'borderColor': '#2980b9', 'backgroundColor': 'rgba(0,0,0,0)',
         'borderWidth': 2, 'fill': False, 'pointRadius': 2, 'lineTension': 0.3},
        {'label': 'Average', 'data': av, 'borderColor': '#27ae60', 'backgroundColor': 'rgba(39,174,96,0.10)',
         'borderWidth': 2, 'fill': True, 'pointRadius': 2, 'lineTension': 0.3},
        {'label': 'Lowest', 'data': mn, 'borderColor': '#e67e22', 'backgroundColor': 'rgba(0,0,0,0)',
         'borderWidth': 2, 'fill': False, 'pointRadius': 2, 'lineTension': 0.3},
    ]
    if setpoint:
        datasets.append({'label': f'Setpoint {setpoint:g} bar', 'data': [setpoint] * len(labels),
                         'borderColor': '#c0392b', 'backgroundColor': 'rgba(0,0,0,0)', 'borderWidth': 1,
                         'borderDash': [5, 4], 'fill': False, 'pointRadius': 0, 'lineTension': 0})
    config = {
        'type': 'line',
        'data': {'labels': labels, 'datasets': datasets},
        'options': {
            # Legend rendered as line swatches in HTML below the image, not boxes.
            'legend': {'display': False},
            'scales': {
                'xAxes': [{'gridLines': {'color': '#eee'},
                           'ticks': {'fontColor': '#888', 'maxRotation': 0, 'autoSkip': True, 'maxTicksLimit': 10}}],
                'yAxes': [{'gridLines': {'color': '#eee'},
                           'ticks': {'fontColor': '#888', 'min': ymin, 'max': ymax}}],
            },
        },
    }
    cfg = json.dumps(config, separators=(',', ':'))
    url = f'https://quickchart.io/chart?bkg=%23ffffff&w=560&h=230&c={urllib.parse.quote(cfg)}'
    return (f'<img src="{url}" width="100%" alt="Overnight pumping pressure by day"'
            f' style="display:block;border-radius:4px;max-width:100%;border:1px solid #eee;">')


def _dip_runs(pump, pred):
    """Lengths (minutes) of consecutive runs where pred(pressure) holds, allowing a
    small data gap (<=3 min) within a run. pump is [(syd_dt, pa, setpoint)]."""
    runs, cur, prev = [], 0, None
    for t, pa, _ in pump:
        if pred(pa) and prev is not None and (t - prev).total_seconds() <= 180:
            cur += 1
        elif pred(pa):
            if cur:
                runs.append(cur)
            cur = 1
        else:
            if cur:
                runs.append(cur)
            cur = 0
        prev = t
    if cur:
        runs.append(cur)
    return runs


def overnight_dip_stats(d_from, d_to):
    """One Supabase pass over the period's 1-minute pressure, producing:
      * OUTAGES (missed irrigation): continuous stretches below MISSED_BAR until
        pressure recovers to setpoint, measured over ALL hours. A pump fault leaves
        the pumps off and pressure collapsed, and these can run for hours or DAYS, so
        they must NOT be split per night. Each has start, end, duration, lowest.
      * per-night DIPS: minutes more than DIP_UNDER_BAR under setpoint while the main
        pumps are running (a delivery shortfall during active irrigation), keyed to
        the night's morning date.
    Returns None if Supabase is unavailable or there is no data."""
    lo = (datetime(d_from.year, d_from.month, d_from.day, tzinfo=SYDNEY_TZ)
          - timedelta(days=1)).astimezone(timezone.utc)
    hi = (datetime(d_to.year, d_to.month, d_to.day, tzinfo=SYDNEY_TZ)
          + timedelta(days=1)).astimezone(timezone.utc)
    rows, off = [], 0
    try:
        while True:
            q = ('select=ts,pressure_avg,p1_run_secs,p2_run_secs,p3_run_secs,setpoint_bar'
                 f'&ts=gte.{urllib.parse.quote(lo.isoformat())}&ts=lt.{urllib.parse.quote(hi.isoformat())}'
                 f'&order=ts.asc&limit=1000&offset={off}')
            chunk = _supa('pump_stats_v2', q)
            rows += chunk
            if len(chunk) < 1000:
                break
            off += 1000
            if off >= 120000:
                break
    except Exception as e:
        log.warning(f'dip/outage fetch failed: {e}')
        return None
    mins = []
    for r in rows:
        pa = r.get('pressure_avg')
        if pa is None:
            continue
        t = datetime.fromisoformat(r['ts'].replace('Z', '+00:00')).astimezone(SYDNEY_TZ)
        run = (r.get('p1_run_secs') or 0) + (r.get('p2_run_secs') or 0) + (r.get('p3_run_secs') or 0)
        mins.append((t, pa, run, r.get('setpoint_bar') or 7.8))
    if not mins:
        return None
    sp = mins[-1][3] or 7.8

    # Outages: a continuous stretch below MISSED_BAR, closed only when pressure
    # recovers to (setpoint - DIP_UNDER_BAR). A brief restart that does not fully
    # recover keeps it as one event, so a multi-day collapse is a single outage.
    outages, cur = [], None

    def _close(c):
        dur = (c['end'] - c['start']).total_seconds() / 60
        if dur > MISSED_MIN:
            outages.append({'start': c['start'], 'end': c['end'],
                            'lo': round(c['lo'], 2), 'mins': round(dur)})

    for t, pa, run, s in mins:
        if pa < MISSED_BAR:
            if cur is None:
                cur = {'start': t, 'end': t, 'lo': pa}
            else:
                cur['end'] = t
                cur['lo'] = min(cur['lo'], pa)
        elif cur is not None and pa >= sp - DIP_UNDER_BAR:
            _close(cur)
            cur = None
    if cur is not None:
        _close(cur)

    # Per-night dips (overnight + pumping), keyed to the morning date in range.
    nights = {}
    for t, pa, run, s in mins:
        if run > 30 and (t.hour >= 19 or t.hour < 7):
            morning = t.date() if t.hour < 7 else t.date() + timedelta(days=1)
            if d_from <= morning <= d_to:
                nights.setdefault(morning.isoformat(), []).append((t, pa, s))
    per_night = []
    thr = sp - DIP_UNDER_BAR
    for night in sorted(nights):
        pm = nights[night]
        runs = _dip_runs(pm, lambda pa: pa < thr)
        per_night.append({'night': night, 'lowest': round(min(p[1] for p in pm), 2),
                          'under': sum(runs), 'longest': max(runs) if runs else 0,
                          'pumpmin': len(pm)})
    if not per_night and not outages:
        return None
    worst = max(per_night, key=lambda p: p['longest']) if per_night else None
    return {
        'outages': outages,
        'missed': len(outages),
        'outage_total_min': sum(o['mins'] for o in outages),
        'outage_longest_min': max((o['mins'] for o in outages), default=0),
        'per_night': per_night,
        'total_under': sum(p['under'] for p in per_night),
        'longest': worst['longest'] if worst else 0,
        'longest_night': worst['night'] if worst else None,
        'longest_low': worst['lowest'] if worst else None,
        'setpoint': sp,
    }


def gather_metrics(d_from, d_to, latest, raw_events, alarms_period, active_now, days):
    """All the health + water figures for the period, plus an overall verdict."""
    f, t = d_from.isoformat(), d_to.isoformat()
    lake_ml, tank_l = water_pumped(d_from, d_to)
    total_ml = round(lake_ml + tank_l / 1e6, 3)

    readings = _hist_readings(d_from, d_to)
    press = overnight_pressure(readings)
    dips = overnight_dip_stats(d_from, d_to)

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

    # Low-flow stops - frequent cycling can mean a leak or zero-demand running.
    # Count from Supabase for full-month coverage; fall back to the repo events
    # (which only span ~2 weeks, so a monthly count would be undercounted).
    low_flow = supa_low_flow_count(d_from, d_to)
    if low_flow is None:
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
    if dips and dips.get('missed'):
        bad.append(f"{dips['missed']} missed irrigation outage(s)")
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

    setpoint = (latest.get('system') or {}).get('pressure_setpoint_bar')
    return {
        'lake_ml': lake_ml, 'tank_l': tank_l, 'total_ml': total_ml,
        'press': press, 'run_hours': run_hours, 'starts': starts, 'total_starts': total_starts,
        'kwh': kwh, 'kwh_per_ml': kwh_per_ml, 'low_flow': low_flow,
        'alarms_count': alarms_count, 'active_count': len(active_now),
        'setpoint': setpoint,
        'pressure_chart': overnight_pressure_chart(readings, setpoint),
        'dips': dips,
        'verdict': verdict,
    }


def _dips_html(dips, report_kind='Weekly'):
    """Overnight pressure-dip + missed-irrigation panel (tiles + per-night table)."""
    if not dips:
        return ''
    sp = dips.get('setpoint', 7.8)
    outages = dips.get('outages', [])
    missed = dips['missed']
    mcol = '#c0392b' if missed else '#27ae60'
    TH = 'padding:8px 12px;font-family:Arial,sans-serif;font-size:12px;font-weight:bold;color:#fff;background:#003366;'
    td = 'padding:6px 12px;font-family:Arial,sans-serif;font-size:12px;'

    def _nl(iso):
        d = datetime.fromisoformat(iso)
        return f'{d.strftime("%a")} {d.day} {d.strftime("%b")}'

    def _when(t):
        h = t.hour % 12 or 12
        return f'{t.strftime("%a")} {t.day} {t.strftime("%b")} {h}:{t.minute:02d}{"am" if t.hour < 12 else "pm"}'

    def _dur(mn):
        if mn >= 1440:
            return f'{mn / 1440:.1f} days'
        if mn >= 120:
            return f'{mn / 60:.1f} h'
        return f'{mn} min'

    def tile(label, value, sub, vcol='#003366'):
        return (f'<td width="33%" style="padding:10px 14px;vertical-align:top;border:1px solid #eee;">'
                f'<div style="font-family:Arial,sans-serif;font-size:11px;color:#888;text-transform:uppercase;'
                f'letter-spacing:.4px;">{label}</div>'
                f'<div style="font-family:Arial,sans-serif;font-size:19px;font-weight:bold;color:{vcol};'
                f'margin:3px 0 1px;">{value}</div>'
                f'<div style="font-family:Arial,sans-serif;font-size:11px;color:#999;">{sub}</div></td>')

    # Tiles: missed outages, total time down, longest dip (while pumping)
    down_min = dips.get('outage_total_min', 0)
    um = dips['total_under']
    under_val = f'{um} min' if um < 120 else f'{um / 60:.1f} h'
    longest_sub = (f"lowest {dips['longest_low']:.1f} bar, {_nl(dips['longest_night'])}"
                   if dips['longest'] else 'no dips while pumping')
    tiles = (
        '<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;"><tr>'
        + tile('Missed Irrigation', str(missed), f'outages below {MISSED_BAR:g} bar (pump down)', mcol)
        + tile('Total Time Down', (_dur(down_min) if down_min else '0 min'),
               f'pressure below {MISSED_BAR:g} bar', mcol if down_min else '#27ae60')
        + tile('Longest Dip (pumping)', f"{dips['longest']} min", longest_sub)
        + '</tr></table>')

    # Outages table (the missed irrigation events, with real durations)
    outage_html = ''
    if outages:
        orows = ''
        for i, o in enumerate(outages):
            bg = 'background:#fdf3f3;' if i % 2 == 0 else 'background:#fff7f7;'
            orows += (f'<tr style="{bg}">'
                      f'<td style="{td}">{_when(o["start"])} &rarr; {_when(o["end"])}</td>'
                      f'<td style="{td}text-align:right;font-weight:bold;color:#c0392b;">{_dur(o["mins"])}</td>'
                      f'<td style="{td}text-align:right;">{o["lo"]:.2f} bar</td></tr>')
        outage_html = (
            '<div style="font-family:Arial,sans-serif;font-size:12px;font-weight:bold;color:#c0392b;'
            'margin:12px 2px 4px;">Missed Irrigation - Pressure Outages</div>'
            '<table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #e0b4b4;border-radius:4px;">'
            f'<tr><td style="{TH}background:#c0392b;">When (continuous, until pressure recovered)</td>'
            f'<td style="{TH}background:#c0392b;text-align:right;">Time below {MISSED_BAR:g} bar</td>'
            f'<td style="{TH}background:#c0392b;text-align:right;">Lowest</td></tr>'
            f'{orows}</table>')

    # Per-night dip table (delivery pressure while pumping)
    src = dips['per_night']
    shown = src if report_kind == 'Weekly' else [n for n in src if n['under'] > 0]
    omitted = len(src) - len(shown)
    trs = ''
    for i, n in enumerate(shown):
        bg = 'background:#f9f9f9;' if i % 2 == 0 else ''
        lowcol = ('#c0392b' if n['lowest'] < MISSED_BAR else
                  '#e67e22' if n['lowest'] < sp - DIP_UNDER_BAR else '#1b2631')
        trs += (f'<tr style="{bg}">'
                f'<td style="{td}">{_nl(n["night"])}</td>'
                f'<td style="{td}color:{lowcol};text-align:right;">{n["lowest"]:.2f} bar</td>'
                f'<td style="{td}text-align:right;">{n["under"]} min</td>'
                f'<td style="{td}text-align:right;">{n["longest"]} min</td></tr>')
    if not trs:
        trs = ('<tr><td colspan="4" style="padding:10px 12px;font-family:Arial,sans-serif;'
               'font-size:13px;color:#999;">Pressure held within 1 bar of setpoint every night - no dips.</td></tr>')
    note = ('<p style="margin:6px 2px 0;font-family:Arial,sans-serif;font-size:11px;color:#999;">'
            f'Dips are measured while the pumps are running (each row is the night finishing that morning). '
            f'A small loss at the pump multiplies out to the furthest sprinkler.'
            + (f' {omitted} other night(s) ran normally.' if omitted else '') + '</p>')
    dip_html = (
        '<div style="font-family:Arial,sans-serif;font-size:12px;font-weight:bold;color:#003366;'
        'margin:12px 2px 4px;">Overnight Pressure Dips (while pumping)</div>'
        f'<table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #eee;border-radius:4px;">'
        f'<tr><td style="{TH}">Night (to morning)</td><td style="{TH}text-align:right;">Lowest</td>'
        f'<td style="{TH}text-align:right;">Min under {sp - DIP_UNDER_BAR:g} bar</td>'
        f'<td style="{TH}text-align:right;">Longest dip</td></tr>{trs}</table>{note}')

    return (
        '<div style="font-family:Arial,sans-serif;font-size:12px;font-weight:bold;color:#003366;'
        'margin:16px 2px 6px;">Overnight Pressure &amp; Missed Irrigation</div>'
        + tiles + outage_html + dip_html)


def _metrics_html(m, report_kind='Weekly'):
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

    chart = ''
    if m.get('pressure_chart'):
        def _lk(style, color, label, w=3):
            return (f'<span style="white-space:nowrap;"><span style="display:inline-block;width:22px;'
                    f'border-top:{w}px {style} {color};vertical-align:middle;line-height:0;">&nbsp;</span>'
                    f'<span style="color:#555;">&nbsp;{label}</span></span>')
        legend = (
            '<div style="font-family:Arial,sans-serif;font-size:11px;text-align:center;line-height:2;margin-top:4px;">'
            + _lk('solid', '#2980b9', 'Highest') + '&nbsp;&nbsp;&nbsp;'
            + _lk('solid', '#27ae60', 'Average') + '&nbsp;&nbsp;&nbsp;'
            + _lk('solid', '#e67e22', 'Lowest') + '&nbsp;&nbsp;&nbsp;'
            + _lk('dashed', '#c0392b', 'Setpoint', 2) + '</div>')
        chart = (
            '<div style="font-family:Arial,sans-serif;font-size:12px;font-weight:bold;'
            'color:#003366;margin:14px 2px 4px;">Overnight Pumping Pressure by Day</div>'
            '<div style="font-family:Arial,sans-serif;font-size:11px;color:#888;margin:0 2px 6px;">'
            'Discharge pressure while pumping, 7pm-7am each night, vs the setpoint.</div>'
            + m['pressure_chart'] + legend)

    dips_html = _dips_html(m.get('dips'), report_kind)
    return (
        '<table width="100%" cellpadding="0" cellspacing="0">'
        f'{_section_heading("Water & Pump Health")}'
        '<tr><td style="padding:0 32px 4px;">'
        f'{verdict_box}{water}{perf}{rh}{chart}{dips_html}'
        '</td></tr></table>')


def build_html(day_data, latest, period_label, alarms_24h, active_now=None, metrics=None,
               report_kind='Weekly'):
    date_label = period_label
    active_now = active_now or []
    gk_addr = GREENKEEPER_ADDR
    metrics_html = _metrics_html(metrics, report_kind) if metrics else ''

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
    # Columns: When, Error (what it was), Source, Emailed to the greenkeeper.
    idx = emailed_index()
    alarm_rows = ''
    for i, a in enumerate(alarms_24h[:40]):
        ts = a.get('timestamp_iso', '')
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(SYDNEY_TZ)
            ts_str = dt.strftime('%a %-d %b, %-I:%M %p')
        except Exception:
            ts_str = ts[:16]
        appeared = a.get('type_code', 3) == 1
        color    = '#c0392b' if appeared else '#27ae60'
        err      = a.get('description') or a.get('type_name', 'Event')
        if not appeared:
            err += ' (cleared)'
        src = a.get('pump_label', 'System')
        if appeared:
            em = emailed_for(a, idx)
            if em:
                try:
                    edt = datetime.fromisoformat(str(em).replace('Z', '+00:00')).astimezone(SYDNEY_TZ)
                    em_html = f'<span style="color:#27ae60;">Yes - {edt.strftime("%-d %b")}</span>'
                except Exception:
                    em_html = '<span style="color:#27ae60;">Yes</span>'
            else:
                em_html = '<span style="color:#c0392b;">No</span>'
        else:
            em_html = '<span style="color:#ccc;">-</span>'
        bg = 'background:#f9f9f9;' if i % 2 == 0 else ''
        alarm_rows += (f'<tr style="{bg}">'
                       f'<td style="padding:7px 16px;font-family:Arial,sans-serif;'
                       f'font-size:13px;color:#666;white-space:nowrap;">{ts_str}</td>'
                       f'<td style="padding:7px 16px;font-family:Arial,sans-serif;'
                       f'font-size:13px;color:{color};">{err}</td>'
                       f'<td style="padding:7px 16px;font-family:Arial,sans-serif;'
                       f'font-size:13px;">{src}</td>'
                       f'<td style="padding:7px 16px;font-family:Arial,sans-serif;'
                       f'font-size:13px;white-space:nowrap;">{em_html}</td></tr>')
    if not alarm_rows:
        msg = ('No alarms this period (routine Low-Flow Stop/Boost operation is not listed).'
               if not active_now else 'No new alarm events this period.')
        alarm_rows = ('<tr><td colspan="4" style="padding:10px 16px;'
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
               font-weight:bold;color:#ffffff;">Pump Station {report_kind} Summary</td></tr>
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
{_section_heading("System Pressure (all hours)")}
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
<td style="{TH}">When</td>
<td style="{TH}">Error</td>
<td style="{TH}">Source</td>
<td style="{TH}">Emailed to greens shed</td>
</tr>
{alarm_rows}
</table>
<p style="margin:6px 2px 0;font-family:Arial,sans-serif;font-size:11px;color:#999;">
"Emailed to greens shed" shows whether a fault alert for that alarm was emailed to
the greenkeeper ({gk_addr}). Routine Low-Flow Stop/Boost operation is not listed.</p>
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


def build_period_html(d_from, d_to, period_label, days, latest, raw_events, report_kind='Weekly'):
    """Assemble one pump summary (weekly or monthly) for [d_from, d_to]."""
    day_data, _ = aggregate_week(days, d_from, d_to)
    # Full-period alarms from Supabase (the repo pump_alarm_history.json keeps only
    # ~200 events / ~2 weeks, which made a monthly report show only part of the
    # month). Fall back to the repo file if Supabase is unavailable.
    alarms = supa_alarm_events(d_from, d_to)
    if not alarms:
        alarms = _alarms_in(real_alarm_events(raw_events), d_from, d_to)
    active_now = active_alarms_from_latest(latest)
    metrics = gather_metrics(d_from, d_to, latest, raw_events, alarms, active_now, days)
    return build_html(day_data or {}, latest, period_label, alarms, active_now, metrics, report_kind)


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

    html = build_period_html(d_from, d_to, period_label, days, latest, raw_events,
                             'Monthly' if monthly else 'Weekly')
    send_email(subject, html)


if __name__ == '__main__':
    main()
