#!/usr/bin/env python3
"""Build the WaterNSW water-usage files for the board dashboard.

Two artifacts:
  * data/water_register_daily.json - the DURABLE raw store: the end-of-day
    cumulative meter register per day (raw, exactly as the DAS logs it). This is
    the source of truth; the admin page merges weekly CSV uploads into it.
  * data/water_usage.json - DERIVED rollups the board reads (daily/monthly/
    water-year), with the 10x over-read corrected.

Why the register and not the export's flow(inML) column: the DAS over-reads
EXACTLY 10x (confirmed Oct 2026 against the physical meter), and before the LID
was reconfigured on 2022-11-24 its flow(inML) column was not divided down and
reads ~1000x high. The raw register ("flow" kL / "meter displayed", col 5) is
continuous and reset-free across all 5 years, so we read that and apply ONE
correction from data/pump_meter.json's das block:

    usage over a period = delta(register) * das.scale / 1000  ML  (= register/10000)

The +offset_kl cancels in deltas. When WaterNSW fixes the LID (das.scale -> 1,
offset -> 0) a rebuild re-applies automatically. NOTE: raw register values from
before and after a real LID fix are in different units and must not be merged -
re-seed from a fresh full export at that point.

Usage:
  python scripts/water_usage_build.py                 # build from the register
      store if present, else seed it from the newest data/reference CSV
  python scripts/water_usage_build.py --import-csv F   # merge CSV F into the
      register store, then rebuild
  python scripts/water_usage_build.py --reseed         # rebuild the register
      store from the newest data/reference CSV (discards the stored register)

The admin page performs the --import-csv + rebuild in-browser; this script is the
canonical/offline path and keeps the arithmetic in one documented place.
"""
import csv
import glob
import json
import math
import os
import sys
from collections import OrderedDict
from datetime import datetime, timezone


def _r(x, n):
    """Round half-up, matching JS Math.round, so the admin in-browser build and
    this offline build produce byte-identical files (no churn). Python's built-in
    round() uses banker's rounding and would differ on .5 boundaries."""
    p = 10 ** n
    return math.floor(x * p + 0.5) / p


def _jsnum(x):
    """Emit integral floats as ints (46.0 -> 46) so json output matches JS
    JSON.stringify, keeping the admin-upload and offline builds byte-identical."""
    return int(x) if float(x) == int(x) else x

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REF_DIR = os.path.join(ROOT, "data", "reference")
OUT = os.path.join(ROOT, "data", "water_usage.json")
REGISTER = os.path.join(ROOT, "data", "water_register_daily.json")
PUMP_METER = os.path.join(ROOT, "data", "pump_meter.json")

COL_TS = 0
COL_REGISTER = 5   # "flow" in kL = raw cumulative meter register (continuous)
ALLOCATION_ML = 193.0


def _latest_export():
    files = glob.glob(os.path.join(REF_DIR, "das_export_*.csv"))
    if not files:
        raise SystemExit("No DAS export found in data/reference/ (das_export_*.csv)")

    def key(p):
        parts = os.path.basename(p).replace(".csv", "").split("_")
        return (parts[-1] if parts else "", os.path.getmtime(p))
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
    y, m = int(d[:4]), int(d[5:7])
    wy = y if m >= 7 else y - 1
    return f"{wy}-{str(wy + 1)[2:]}"


def register_from_csv(path):
    """Parse a DAS export -> {YYYY-MM-DD: end-of-day raw register}."""
    daily = {}
    with open(path, encoding="utf-8-sig") as fh:
        r = csv.reader(fh)
        next(r, None); next(r, None); next(r, None)   # Id / keys / Units rows
        for row in r:
            if len(row) <= COL_REGISTER:
                continue
            v = _num(row[COL_REGISTER])
            if v is None:
                continue
            day = row[COL_TS][:10]
            if len(day) == 10:
                daily[day] = v   # later rows overwrite -> end-of-day value
    return daily


def load_register():
    try:
        with open(REGISTER, encoding="utf-8") as fh:
            blob = json.load(fh) or {}
        reg = blob.get("register") or {}
        return {k: float(v) for k, v in reg.items()}
    except Exception:
        return {}


def save_register(reg, source):
    days = sorted(reg)
    blob = {
        "_note": ("Durable raw end-of-day meter register (as the DAS logs it, "
                  "uncorrected). Source of truth for data/water_usage.json; the "
                  "admin DAS-CSV upload merges new days in. Correction (10x + units) "
                  "is applied when building water_usage.json."),
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "firstDay": days[0] if days else None,
        "lastDay": days[-1] if days else None,
        "register": {d: reg[d] for d in days},
    }
    with open(REGISTER, "w", encoding="utf-8") as fh:
        json.dump(blob, fh, separators=(",", ":"))


def build_usage(reg):
    scale, offset = _das_cfg()
    days = sorted(reg)
    if not days:
        raise SystemExit("Register store is empty - nothing to build")

    def to_ml(delta):
        return _r(delta * scale / 1000.0, 4)

    daily, prev = [], None
    for d in days:
        v = reg[d]
        if prev is not None:
            ml = to_ml(v - prev)
            # Emit integral values as ints (0 not 0.0) so json matches JS
            # JSON.stringify exactly and the admin-upload rebuild causes no churn.
            daily.append([d, _jsnum(ml if ml > 0 else 0)])
        prev = v

    monthly = OrderedDict()
    for d, ml in daily:
        monthly[d[:7]] = monthly.get(d[:7], 0.0) + ml
    wy = OrderedDict()
    for d, ml in daily:
        wy[_water_year(d)] = wy.get(_water_year(d), 0.0) + ml
    cy = OrderedDict()
    for d, ml in daily:
        cy[d[:4]] = cy.get(d[:4], 0.0) + ml

    first_reg, last_reg = reg[days[0]], reg[days[-1]]
    out = {
        "_note": ("Built by scripts/water_usage_build.py from the raw meter register. "
                  "Volumes are the REAL (corrected) extraction. The DAS flow(inML) "
                  "column is not used (10x high, ~1000x before the 2022-11-24 LID reconfig)."),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "correction": {"scale": scale, "offset_kl": offset, "reconfig_date": "2022-11-24"},
        "allocationMl": _jsnum(ALLOCATION_ML),
        "firstDay": days[0],
        "lastDay": days[-1],
        "currentMeterFaceKl": _jsnum(_r(last_reg * scale + offset, 1)),
        "totalMlSinceStart": _jsnum(_r((last_reg - first_reg) * scale / 1000.0, 2)),
        "waterYears": [{"label": k, "usedMl": _jsnum(_r(v, 2)), "pct": _jsnum(_r(v / ALLOCATION_ML * 100, 1))}
                       for k, v in wy.items()],
        "calendarYears": [{"year": k, "usedMl": _jsnum(_r(v, 2))} for k, v in cy.items()],
        "monthly": [[k, _jsnum(_r(v, 3))] for k, v in monthly.items()],
        "daily": daily,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    return out


def main():
    args = sys.argv[1:]
    if "--import-csv" in args:
        path = args[args.index("--import-csv") + 1]
        reg = load_register()
        new = register_from_csv(path)
        # Merge by day. The register is absolute cumulative, so overlapping dates
        # cannot double-count. Keep the LARGER value per day so a partial re-read
        # (e.g. an export whose last day stops mid-afternoon) never lowers a day
        # that a complete read already filled. New days fill gaps; missing days keep.
        added = overlap = 0
        for day, val in new.items():
            if day in reg:
                overlap += 1
                reg[day] = max(reg[day], val)
            else:
                added += 1
                reg[day] = val
        save_register(reg, "merged:" + os.path.basename(path))
        print(f"Merged {os.path.basename(path)}: +{added} new, {overlap} overlapping "
              f"(kept the more complete reading); {len(reg)} days total")
    elif "--reseed" in args or not load_register():
        src = _latest_export()
        reg = register_from_csv(src)
        save_register(reg, os.path.basename(src))
        print(f"Seeded register from {os.path.basename(src)} ({len(reg)} days)")
    else:
        reg = load_register()

    reg = load_register()
    out = build_usage(reg)
    print(f"Wrote {OUT}: {out['firstDay']} -> {out['lastDay']}, "
          f"total {out['totalMlSinceStart']} ML, face {out['currentMeterFaceKl']} kL")
    for w in out["waterYears"]:
        print(f"    {w['label']}: {w['usedMl']} ML ({w['pct']}%)")


if __name__ == "__main__":
    main()
