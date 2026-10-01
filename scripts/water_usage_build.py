#!/usr/bin/env python3
"""Build data/water_usage.json from the WaterNSW DAS export.

The DAS compliance telemetry (LID 120922-3-LID01, WAL 40AL413687) over-reads
EXACTLY 10x (confirmed Oct 2026 against the physical meter). We NEVER trust the
export's own "flow(inML)" column: before the DAS reconfigured the LID on
2022-11-24 that column was not divided down and reads ~1000x too high, and after
that date it is still 10x the real volume. Instead we read the raw cumulative
meter register (the "flow" kL / "meter displayed" column, which is continuous and
reset-free across the whole 5 years) and apply ONE correction:

    real kL (cumulative) = register * das.scale + das.offset_kl
    usage over a period  = delta(register) * das.scale / 1000  ML   (offset cancels)

das.scale / das.offset_kl come from data/pump_meter.json so that when WaterNSW
corrects the LID (scale -> 1, offset -> 0) this pipeline follows automatically.

Output data/water_usage.json is small (daily usage pairs + monthly + water-year
rollups) so board.html can load it without parsing the 2.4 MB CSV client-side.

Drop a newer/wider DAS export into data/reference/ and re-run; the newest export
file (by embedded end date, else mtime) is used automatically.
"""
import csv
import glob
import json
import os
from collections import OrderedDict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REF_DIR = os.path.join(ROOT, "data", "reference")
OUT = os.path.join(ROOT, "data", "water_usage.json")
PUMP_METER = os.path.join(ROOT, "data", "pump_meter.json")

# Column indices in the DAS Eagle.io export (3 header rows: Id, keys, Units)
COL_TS = 0
COL_REGISTER = 5   # "flow" in kL = raw cumulative meter register (continuous)
ALLOCATION_ML = 193.0


def _latest_export():
    files = glob.glob(os.path.join(REF_DIR, "das_export_*.csv"))
    if not files:
        raise SystemExit("No DAS export found in data/reference/ (das_export_*.csv)")
    # Prefer the file whose name encodes the latest end date; fall back to mtime.
    def key(p):
        base = os.path.basename(p)
        parts = base.replace(".csv", "").split("_")
        end = parts[-1] if parts else ""
        return (end, os.path.getmtime(p))
    return max(files, key=key)


def _das_cfg():
    scale, offset = 0.1, 10475.7
    try:
        with open(PUMP_METER, encoding="utf-8") as fh:
            das = (json.load(fh) or {}).get("das", {})
        scale = float(das.get("scale", scale))
        offset = float(das.get("offset_kl", offset))
    except Exception:
        pass
    return scale, offset


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _water_year(d):
    """d = 'YYYY-MM-DD'; returns water-year label (Jul 1 - Jun 30)."""
    y, m = int(d[:4]), int(d[5:7])
    wy = y if m >= 7 else y - 1
    return f"{wy}-{str(wy + 1)[2:]}"


def main():
    src = _latest_export()
    scale, offset = _das_cfg()

    # Last register reading per calendar day (register is monotonic, reset-free).
    daily_reg = OrderedDict()
    with open(src, encoding="utf-8-sig") as fh:
        r = csv.reader(fh)
        next(r, None); next(r, None); next(r, None)  # Id / keys / Units rows
        for row in r:
            if len(row) <= COL_REGISTER:
                continue
            v = _num(row[COL_REGISTER])
            if v is None:
                continue
            day = row[COL_TS][:10]
            if len(day) == 10:
                daily_reg[day] = v  # later rows overwrite -> end-of-day value

    days = sorted(daily_reg)
    if not days:
        raise SystemExit("No register readings parsed from " + src)

    def to_ml(delta_register):
        return round(delta_register * scale / 1000.0, 4)

    # Daily usage = delta of end-of-day register vs previous day present.
    daily = []
    prev = None
    for d in days:
        reg = daily_reg[d]
        if prev is not None:
            ml = to_ml(reg - prev)
            if ml < 0:
                ml = 0.0  # guard; register is reset-free but clamp any blip
            daily.append([d, ml])
        prev = reg

    # Monthly rollup
    monthly = OrderedDict()
    for d, ml in daily:
        monthly.setdefault(d[:7], 0.0)
        monthly[d[:7]] += ml
    monthly_list = [[k, round(v, 3)] for k, v in monthly.items()]

    # Water-year rollup (Jul-Jun) vs allocation
    wy = OrderedDict()
    for d, ml in daily:
        wy.setdefault(_water_year(d), 0.0)
        wy[_water_year(d)] += ml
    water_years = [
        {"label": k, "usedMl": round(v, 2), "pct": round(v / ALLOCATION_ML * 100, 1)}
        for k, v in wy.items()
    ]

    # Calendar-year rollup
    cy = OrderedDict()
    for d, ml in daily:
        cy.setdefault(d[:4], 0.0)
        cy[d[:4]] += ml
    calendar_years = [{"year": k, "usedMl": round(v, 2)} for k, v in cy.items()]

    first_reg, last_reg = daily_reg[days[0]], daily_reg[days[-1]]
    out = {
        "_note": (
            "Built by scripts/water_usage_build.py from the WaterNSW DAS export. "
            "All volumes are the REAL (corrected) extraction: register * scale / 1000 ML. "
            "The export's own flow(inML) column is NOT used (10x over-read, and ~1000x "
            "before the LID reconfig on 2022-11-24). When WaterNSW fixes the LID, update "
            "pump_meter.json das.scale/offset_kl and re-run."
        ),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": os.path.basename(src),
        "correction": {"scale": scale, "offset_kl": offset,
                       "reconfig_date": "2022-11-24",
                       "note": "DAS over-reads 10x; corrected here. Reverts when LID fixed."},
        "allocationMl": ALLOCATION_ML,
        "firstDay": days[0],
        "lastDay": days[-1],
        "currentMeterFaceKl": round(last_reg * scale + offset, 1),
        "totalMlSinceStart": round((last_reg - first_reg) * scale / 1000.0, 2),
        "waterYears": water_years,
        "calendarYears": calendar_years,
        "monthly": monthly_list,
        "daily": daily,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"Wrote {OUT}")
    print(f"  source: {os.path.basename(src)}")
    print(f"  range : {days[0]} -> {days[-1]} ({len(daily)} days)")
    print(f"  total : {out['totalMlSinceStart']} ML; current face {out['currentMeterFaceKl']} kL")
    for w in water_years:
        print(f"    {w['label']}: {w['usedMl']} ML ({w['pct']}%)")


if __name__ == "__main__":
    main()
