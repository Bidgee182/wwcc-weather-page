"""
pump_cost_summary.py - accurate, outage-proof pump energy + cost rollups.

Energy is taken from the CU352 CUMULATIVE counters (sys_energy_kwh and the per-pump
kWh registers), NOT by integrating per-minute power. The counter keeps counting while
the Pi is offline, so a dropout never loses energy - the next reading's delta recovers
it. Time-of-use cost keeps the shape of the minutes we did capture (peak/shoulder/off
from power x time) and scales it to the true counter delta, so the gap energy is still
costed. Volume pumped comes from the physical pump meter (data/pump_meter.json), never
the Grundfos flow estimate.

Writes:
  data/pump_cost_daily.json   - one durable record per day (survives Supabase pruning)
  data/pump_cost_summary.json - week / month / season rollups for the board dashboard

Runs daily via .github/workflows/pump-cost-summary.yml. Env: SUPABASE_KEY optional
(defaults to the public anon key, same as the Pi poller and pump-station.html).
"""
import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    SYD = ZoneInfo("Australia/Sydney")
except Exception:
    SYD = None

sys.path.insert(0, str(Path(__file__).parent))
import pump_meter  # noqa: E402  (ML pumped from the physical meter)

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data"
DAILY_PATH = DATA / "pump_cost_daily.json"
SUMMARY_PATH = DATA / "pump_cost_summary.json"

SUPA_URL = os.environ.get("SUPABASE_URL") or "https://sduzxijjvpbfgvlwcwpp.supabase.co"
SUPA_KEY = os.environ.get("SUPABASE_KEY") or (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNkdXp4aWpqdnBiZmd2bHdjd3BwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY1ODE2NzgsImV4cCI6MjA5MjE1NzY3OH0"
    ".fbYf9-F987DUSlsibuGnqGYEQe6tsQsOf7NMmNMrBT8")

MAX_BACKFILL_DAYS = 120


def _syd_today():
    return (datetime.now(SYD) if SYD else datetime.now()).date()


def _season_start(d):
    return date(d.year if d.month >= 9 else d.year - 1, 9, 1)


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _tariff():
    t = _load(DATA / "electricity_tariff.json", {})
    r = t.get("rates_c_per_kwh") or {"peak": 32.3, "shoulder": 30.7, "off": 27.6}
    return r


def _cost_per_kl():
    cfg = _load(DATA / "lake_config.json", {})
    try:
        return float(cfg["town_water"]["cost_per_kl"])
    except Exception:
        return 1.77


def rpc_day(d):
    """Call the pump_cost_day(date) Postgres function; returns the single row dict or None."""
    url = f"{SUPA_URL}/rest/v1/rpc/pump_cost_day?" + urllib.parse.urlencode({"d": d.isoformat()})
    req = urllib.request.Request(url, headers={
        "apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        rows = json.loads(r.read().decode("utf-8"))
    return rows[0] if rows else None


def compute_day(d, prev, rates, ml_by_date):
    """Build the durable record for day d from the RPC row + the previous day's counters."""
    row = rpc_day(d)
    if not row or not row.get("mins"):
        return None
    end = row.get("end_counter")
    start = row.get("start_counter")
    if end is None:
        return None
    prev_end = prev.get("endCounter") if prev else None
    kwh = (end - prev_end) if prev_end is not None else (end - start if start is not None else 0)
    if kwh < 0:                       # counter reset/rollover - fall back to this day only
        kwh = max(0.0, (end - start) if start is not None else 0.0)

    integ = float(row.get("integ_total") or 0)
    scale = (kwh / integ) if integ > 0 else 0
    peak = float(row.get("peak_kwh") or 0) * scale
    shoulder = float(row.get("shoulder_kwh") or 0) * scale
    off = float(row.get("off_kwh") or 0) * scale
    if integ <= 0 and kwh > 0:        # counter moved but no power rows (full outage) -> off-peak
        off = kwh
    cost = (peak * rates["peak"] + shoulder * rates["shoulder"] + off * rates["off"]) / 100.0

    def pdelta(k, cur):
        p = prev.get(k) if prev else None
        v = row.get(cur)
        if p is None or v is None:
            return None
        return max(0, v - p)

    return {
        "date": d.isoformat(), "mins": row.get("mins"),
        "kwh": round(kwh, 1), "cost": round(cost, 2),
        "peakKwh": round(peak, 1), "shoulderKwh": round(shoulder, 1), "offKwh": round(off, 1),
        "endCounter": end, "p1End": row.get("p1_end"), "p2End": row.get("p2_end"),
        "p3End": row.get("p3_end"), "jockeyEnd": row.get("jockey_end"),
        "p1Kwh": pdelta("p1End", "p1_end"), "p2Kwh": pdelta("p2End", "p2_end"),
        "p3Kwh": pdelta("p3End", "p3_end"), "jockeyKwh": pdelta("jockeyEnd", "jockey_end"),
        "ml": round(ml_by_date.get(d, 0.0), 3),
    }


def rollup(records, start_d, end_d, cost_per_kl):
    sel = [r for r in records if start_d.isoformat() <= r["date"] <= end_d.isoformat()]
    kwh = sum(r["kwh"] for r in sel)
    cost = sum(r["cost"] for r in sel)          # tariff rates are GST-inclusive
    ml = sum(r["ml"] for r in sel)
    town = ml * 1000.0 * cost_per_kl
    return {
        "kwh": round(kwh, 1), "cost": round(cost, 2), "costExGst": round(cost / 1.1, 2),
        "ml": round(ml, 3),
        "townWaterEquiv": round(town, 2), "netSaving": round(town - cost, 2),
        "costPerMl": round(cost / ml, 2) if ml > 0 else None,
        "from": start_d.isoformat(), "to": end_d.isoformat(), "days": len(sel),
    }


def _fy_start(d):
    """Australian financial year start (1 July)."""
    return date(d.year if d.month >= 7 else d.year - 1, 7, 1)


def main():
    today = _syd_today()
    yesterday = today - timedelta(days=1)
    rates = _tariff()
    cost_per_kl = _cost_per_kl()

    daily = _load(DAILY_PATH, [])
    if not isinstance(daily, list):
        daily = []
    by_date = {r["date"]: r for r in daily}

    # ML pumped per calendar day from the physical meter
    ml_map = {}
    try:
        for d0, ml in pump_meter.daily_pumping_ml().items():
            ml_map[d0] = ml
    except Exception as e:
        print(f"pump_meter unavailable: {e}")

    # Which days to (re)compute: from the day after the last record (or the season
    # start / lookback cap on first run) through yesterday.
    last_recorded = max(by_date) if by_date else None
    # First run backfills from the FY start so the P&L total covers the whole
    # financial year (days with no pump data return nothing and are skipped).
    start_from = (date.fromisoformat(last_recorded) + timedelta(days=1)) if last_recorded \
        else _fy_start(today)
    floor = today - timedelta(days=MAX_BACKFILL_DAYS)
    if start_from < floor:
        start_from = floor

    ordered = sorted(daily, key=lambda r: r["date"])
    prev = ordered[-1] if ordered else None
    d = start_from
    wrote = 0
    while d <= yesterday:
        try:
            rec = compute_day(d, prev, rates, ml_map)
        except Exception as e:
            print(f"  {d}: RPC failed ({e})")
            rec = None
        if rec:
            by_date[rec["date"]] = rec
            prev = rec
            wrote += 1
            print(f"  {d}: {rec['kwh']} kWh  ${rec['cost']}  {rec['ml']} ML  ({rec['mins']} min)")
        d += timedelta(days=1)

    daily = [by_date[k] for k in sorted(by_date)]
    DAILY_PATH.write_text(json.dumps(daily, separators=(",", ":")) + "\n", encoding="utf-8")

    ss = _season_start(today)
    fy = _fy_start(today)
    fy_label = f"{fy.year}-{str(fy.year + 1)[2:]}"
    data_from = daily[0]["date"] if daily else None
    summary = {
        "asOf": today.isoformat(),
        "generatedAt": (datetime.now(SYD) if SYD else datetime.now()).replace(microsecond=0).isoformat(),
        "tariff": rates, "costPerKl": cost_per_kl,
        "dataFrom": data_from,
        "fyLabel": fy_label,
        "financialYtd": rollup(daily, fy, yesterday, cost_per_kl),
        "calendarYtd":  rollup(daily, date(today.year, 1, 1), yesterday, cost_per_kl),
        "week":   rollup(daily, yesterday - timedelta(days=6), yesterday, cost_per_kl),
        "month":  rollup(daily, today.replace(day=1), yesterday, cost_per_kl),
        "season": rollup(daily, ss, yesterday, cost_per_kl),
        "note": "Energy from the CU352 cumulative counter (outage-proof). Volume from the physical pump meter. Cost is the marginal pump running (energy) cost at the time-of-use tariff; excludes site demand/supply/fixed charges on the shared club meter. Tariff rates are GST-inclusive; costExGst is the ex-GST figure for the P&L.",
    }
    SUMMARY_PATH.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    fytd = summary["financialYtd"]
    print(f"Wrote {len(daily)} daily records ({wrote} new/updated).")
    print(f"FY {fy_label} to date: {fytd['kwh']} kWh, ${fytd['cost']} incl GST "
          f"(${fytd['costExGst']} ex GST), {fytd['ml']} ML, net saving ${fytd['netSaving']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
