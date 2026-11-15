"""
Build data/lake_meter.json for the Lake Albert extraction explorer from WaterNSW DAS exports.

Usage:  python build_lake_data.py <folder of DAS CSV exports> <output json>
Example: python build_lake_data.py data/das_exports data/lake_meter.json

Rules (these reproduce the explorer exactly):
- Read every CSV in the folder (DAS export for logger 120922-3-LID01). Overlapping exports are fine:
  readings are merged and de-duplicated by timestamp and register value.
- Use the cumulative register column whose header ends in "/flow" (units kL). Do NOT use
  "flow (delta)": before late 2022 the delta column was recorded about 1,000 times larger.
- Each change in the register between two readings is shared across the local clock hours the interval
  covers, in proportion to the time in each hour. This puts the daily ~03:36/04:36 housekeeping reading
  back into its hour and spreads any gap evenly.
- A reading is the flow SINCE the previous reading (the 11:00 pm reading is the 10 to 11 pm hour).
- Timestamps are local time with daylight saving. If time goes backwards (April change), the whole
  register change goes into the hour of the later reading.
- An interval longer than 3 hours is a data gap: every date it touches is listed in "gaps".
- An hour with less than 30 minutes of coverage is null.
- Days run from 2021-10-01 to the day before the last reading (the last, partial day is left out).
- Values are RAW telemetry kL per hour, rounded to 0.1. The page divides by 10 for corrected m3
  (the telemetry over-reads by a factor of ten).
"""
import csv, json, sys, glob, os, datetime as dt, collections

src, out = sys.argv[1], sys.argv[2]
readings = set()
for path in sorted(glob.glob(os.path.join(src, '*.csv'))):
    rows = list(csv.reader(open(path, encoding='utf-8-sig')))
    head = rows[1]
    col = next(i for i, h in enumerate(head) if h.endswith('/flow'))
    for r in rows[3:]:
        if len(r) > col and r[0][:2] == '20' and r[col] != '':
            readings.add((r[0], float(r[col])))  # keep repeated clock times at the April change

pts = sorted((dt.datetime.strptime(k, '%Y-%m-%d %H:%M:%S'), v) for k, v in readings)
val = collections.defaultdict(float); cov = collections.defaultdict(float); gaps = set()
for (t1, r1), (t2, r2) in zip(pts, pts[1:]):
    d = r2 - r1; span = (t2 - t1).total_seconds()
    if span <= 0:
        val[t2.replace(minute=0, second=0)] += d; continue
    if span > 3 * 3600:
        day = t1.date()
        while day <= t2.date():
            gaps.add(day.isoformat()); day += dt.timedelta(days=1)
    t = t1
    while t < t2:
        hs = t.replace(minute=0, second=0, microsecond=0); he = hs + dt.timedelta(hours=1); e = min(he, t2)
        val[hs] += d * (e - t).total_seconds() / span; cov[hs] += (e - t).total_seconds(); t = e

days = collections.OrderedDict()
day, end = dt.date(2021, 10, 1), pts[-1][0].date()
while day < end:
    days[day.isoformat()] = [round(val[dt.datetime.combine(day, dt.time(h))], 1)
                             if cov.get(dt.datetime.combine(day, dt.time(h)), 0) >= 1800 else None for h in range(24)]
    day += dt.timedelta(days=1)

json.dump({'generated': dt.datetime.now().isoformat(timespec='minutes'), 'last_reading': str(pts[-1][0]),
           'days': days, 'gaps': sorted(gaps)}, open(out, 'w'), separators=(',', ':'))
print(f'{len(days)} days, {days and list(days)[0]} to {days and list(days)[-1]}, gaps: {len(gaps)} days')
