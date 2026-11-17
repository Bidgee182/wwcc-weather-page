#!/usr/bin/env python3
"""Build the daily tank-water series -> data/water_tank_daily.json.

Each day's figure is the water pumped FROM the tank ONTO the course = the SUM of
every drop (dip) in tank volume over that Sydney calendar day. Rises (the tank
refilling) are ignored, because a start-to-end net would hide water pumped while
the tank was also refilling (e.g. 28 Apr 2026: 0.394 ML pumped out while 0.187 ML
refilled - the net would show only 0.207). A drop is attributed to the Sydney day
of its LATER reading, so the window matches the lake daily series.

This is the exact same calculation the greenkeeper "Water Used for Irrigation" and
the board Lake Extraction tank overlays do in-browser, so those charts read this
file instead of recomputing it from every raw reading.

Source: data/farmbot_readings.json (every tank reading: {date UTC-ISO, pct, volume_l}).
Run it whenever readings update; the DAS auto-export job calls it once a day.
"""
import json
import pathlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parent.parent
READINGS = ROOT / "data" / "farmbot_readings.json"
OUT = ROOT / "data" / "water_tank_daily.json"
SYD = ZoneInfo("Australia/Sydney")


def syd_day(iso):
    """Sydney calendar date 'YYYY-MM-DD' for a UTC/ISO reading timestamp."""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(SYD).strftime("%Y-%m-%d")
    except Exception:
        return None


def build():
    with open(READINGS, encoding="utf-8") as fh:
        readings = json.load(fh)

    rs = [r for r in readings if r and r.get("volume_l") is not None]
    rs.sort(key=lambda r: r["date"])

    litres = {}
    for i in range(1, len(rs)):
        drop = rs[i - 1]["volume_l"] - rs[i]["volume_l"]
        if drop > 0:
            d = syd_day(rs[i]["date"])
            if d:
                litres[d] = litres.get(d, 0) + drop

    daily = [[d, int(round(litres[d]))] for d in sorted(litres)]
    out = {
        "_note": ("Daily tank water pumped onto the course = SUM of every drop (dip) "
                  "in tank volume per Sydney calendar day, from farmbot_readings.json; "
                  "rises (refilling) ignored. daily = [Sydney date, litres]; ML = litres/1e6."),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "data/farmbot_readings.json",
        "firstDay": daily[0][0] if daily else None,
        "lastDay": daily[-1][0] if daily else None,
        "totalMl": round(sum(v for _, v in daily) / 1e6, 4),
        "daily": daily,
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
        fh.write("\n")
    print(f"Wrote {OUT}: {len(daily)} days, {out['firstDay']} -> {out['lastDay']}, "
          f"total {out['totalMl']} ML")
    return out


if __name__ == "__main__":
    build()
