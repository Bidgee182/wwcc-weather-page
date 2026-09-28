"""
pump_meter.py - turn cumulative pump-meter readings into daily water pumped (ML).

Source of truth is the physical pump meter (a cumulative totaliser), entered in
the admin dashboard and stored in data/pump_meter.json. The telemetry flow figure
is deliberately NOT used - it is only Grundfos's amps/pump-curve estimate.

Each pair of consecutive readings gives the water pumped in that interval
(reading delta). That volume is spread across the IRRIGATION-SEASON days in the
interval (config irrigation_season.active_months), so a winter baseline reading
back to today's reading correctly loads all the water onto the days pumping
actually ran and leaves the off-season days at zero.

data/pump_meter.json format:
{
  "readings": [
    {"date": "2026-07-01", "value": 12345.0, "unit": "kL", "note": "winter baseline"},
    {"date": "2026-09-29", "value": 27890.0, "unit": "ML"|"kL", "note": "week 1"}
  ],
  "season_start": "2026-09-02"   # optional: tighten the spread to on/after this date
}
"""
import json
from datetime import date, timedelta
from pathlib import Path

import lake_utils as _lu

_METER_PATH = Path(__file__).parent.parent / 'data' / 'pump_meter.json'


def _to_ml(value, unit):
    """Normalise a reading value to megalitres. kL -> ML is /1000; ML stays."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    u = (unit or 'ML').strip().lower()
    if u in ('kl', 'kilolitre', 'kilolitres', 'kilo'):
        return v / 1000.0
    if u in ('l', 'litre', 'litres'):
        return v / 1_000_000.0
    return v  # ML (default)


def _parse_date(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def load_meter(path=None):
    """Return (readings, season_start) - readings sorted by date, each with a
    normalised ml value. Never raises: missing/invalid file -> ([], None)."""
    p = Path(path) if path else _METER_PATH
    try:
        blob = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return [], None
    out = []
    for r in (blob.get('readings') or []):
        d = _parse_date(r.get('date'))
        ml = _to_ml(r.get('value'), r.get('unit'))
        if d is not None and ml is not None:
            out.append({'date': d, 'ml': ml, 'unit': r.get('unit', 'ML'),
                        'note': r.get('note', '')})
    out.sort(key=lambda x: x['date'])
    return out, _parse_date(blob.get('season_start'))


def _active_months():
    try:
        return set(_lu.get_config()['irrigation_season']['active_months'])
    except Exception:
        return {9, 10, 11, 12, 1, 2, 3, 4}  # sensible default (no Jun/Jul/Aug)


def daily_pumping_ml(path=None):
    """Map {date: ML pumped} derived from the meter readings.

    For each consecutive reading pair the delta ML is spread evenly across the
    irrigation-season days in (prev, cur] (>= season_start when set). A negative
    delta (meter reset/rollover) is ignored for that interval. Returns {} when
    there are fewer than two readings.
    """
    readings, season_start = load_meter(path)
    if len(readings) < 2:
        return {}
    active = _active_months()
    out = {}
    for prev, cur in zip(readings, readings[1:]):
        delta = cur['ml'] - prev['ml']
        if delta <= 0:
            continue  # meter reset or no movement
        span = [prev['date'] + timedelta(days=i + 1)
                for i in range((cur['date'] - prev['date']).days)]
        if not span:
            continue
        elig = [d for d in span if d.month in active
                and (season_start is None or d >= season_start)]
        target = elig or span            # fallback: spread over all days if none eligible
        per = delta / len(target)
        for d in target:
            out[d] = out.get(d, 0.0) + per
    return out


def pumping_ml_between(start_date, end_date, path=None):
    """Total ML pumped over [start_date, end_date] inclusive, from the meter."""
    daily = daily_pumping_ml(path)
    return sum(ml for d, ml in daily.items() if start_date <= d <= end_date)


def recent_daily_ml(as_of, lookback_days=14, path=None):
    """Average ML/day pumped over the lookback window ending as_of (for forward
    projections). Averaged over active-season days only, so an off-season lull
    does not drag the rate to zero. Returns None when there is no meter data."""
    daily = daily_pumping_ml(path)
    if not daily:
        return None
    active = _active_months()
    lo = as_of - timedelta(days=lookback_days)
    vals = [ml for d, ml in daily.items() if lo <= d <= as_of and d.month in active]
    if not vals:
        return None
    return sum(vals) / len(vals)


def has_reading_covering(start_date, end_date, path=None):
    """True when the meter has readings that bracket the window (so the window's
    pumping is real, not provisional): a reading on/before start and on/after end."""
    readings, _ = load_meter(path)
    if len(readings) < 2:
        return False
    dates = [r['date'] for r in readings]
    return any(d <= start_date for d in dates) and any(d >= end_date for d in dates)
