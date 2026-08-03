"""
Grundfos Hydro MPC CU 352 / CIM 500 Modbus TCP poller.

Register addressing: Grundfos doc register X = Modbus telegram address X-1.
pymodbus uses 0-based addresses, so doc register 00202 = addr 201.

Key register map (all are FC03 holding registers):
  Status block (addr 200-231 = doc regs 00201-00232):
    200: Status bitfield (bit9=OnOff, bit10=Alarm)
    201: ProcessFeedback (0.01% of sensor max)
    202: ControlMode
    204: AlarmCode
    205: WarningCode
    207: PumpsPresent (bitmask, bit0=P1, bit1=P2, bit2=P3, bit6=Pilot)
    208: PumpsRunning (bitmask, same bit layout)
    219: FeedBackSensorUnit (1=mbar)
    221: FeedBackSensorMax (e.g. 16000 mbar)

  Data block (addr 300-349 = doc regs 00301-00350):
    300: Head (0.001 bar)
    301: VolumeFlow (0.1 m3/h)
    307: ActualSetpoint (0.01%)
    311: PowerHI, 312: PowerLO (combined Watts, 32-bit)
    314: InletPressure (0.001 bar)
    340: OutletPressure (0.001 bar)

  Pump blocks (addr 400-479 = doc regs 00401-00480, 10 regs per pump):
    +0: Status bits (bit1=OnOff/running, bit2=Alarm)
    +1: AlarmCode
    +2: OperationTimeHI, +3: OperationTimeLO (0.01 h, 32-bit)
    +4: Speed (0.01%)
    +5: LineCurrent (0.1 A)
    +6: Power (10 W)
    +7: MotorTemperature (0.01 K, convert to C: val*0.01 - 273.15)
    +8: NumberOfStartsHI, +9: NumberOfStartsLO (32-bit)
  Pilot pump block starts at addr 460 (same 10-reg layout).
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from pymodbus.client import ModbusTcpClient

HOST = os.environ.get("GRUNDFOS_HOST", "bidgee-pumps.duckdns.org")
PORT = int(os.environ.get("GRUNDFOS_PORT", "502"))
NA   = 65535

PUMP_DEFS = [
    {"id": 1, "label": "P1 - CRNE45", "model": "CRNE45-3", "rated_kw": 18.5, "block": 400, "bit": 0},
    {"id": 2, "label": "P2 - CRNE45", "model": "CRNE45-3", "rated_kw": 18.5, "block": 410, "bit": 1},
    {"id": 3, "label": "P3 - CRNE45", "model": "CRNE45-3", "rated_kw": 18.5, "block": 420, "bit": 2},
    {"id": 4, "label": "Jockey",       "model": "CRNE10-9", "rated_kw": 5.5,  "block": 460, "bit": 6},
]

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
LATEST_FILE  = os.path.join(DATA_DIR, "pump_station_latest.json")
HISTORY_FILE = os.path.join(DATA_DIR, "pump_station_history.json")
DAILY_FILE   = os.path.join(DATA_DIR, "pump_station_daily.json")

MAX_24H   = 288   # 5-min polls for 24 h
MAX_90D   = 8640  # 15-min polls for 90 d
HIST_MIN_INTERVAL_S = 720  # 12 min between 15-min history writes

CONTROL_MODE_NAMES = {
    1: "constant differential pressure",
    2: "constant pressure setpoint",
    3: "constant head",
    4: "constant pressure",
    5: "constant flow",
    6: "constant temperature",
    7: "duty-standby",
    8: "constant level",
}


def rhr(client, addr, count):
    r = client.read_holding_registers(addr, count=count, device_id=1)
    if r.isError():
        return [None] * count
    return r.registers


def valid(raw):
    return None if raw is None or raw == NA else raw


def pct_to_bar(pct_raw, sensor_max_mbar):
    if pct_raw is None or not sensor_max_mbar:
        return None
    return round(pct_raw * 0.0001 * sensor_max_mbar / 1000, 3)


def hi_lo_32(hi, lo):
    if hi is None or lo is None:
        return None
    return hi * 65536 + lo


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path, data):
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)


def parse_ts(ts):
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    client = ModbusTcpClient(HOST, port=PORT, timeout=10)
    connected = client.connect()
    now_iso = datetime.now(timezone.utc).isoformat()

    if not connected:
        prev = load_json(LATEST_FILE, {})
        prev["connected"] = False
        prev["last_seen"]  = prev.get("timestamp")
        prev["timestamp"]  = now_iso
        write_json(LATEST_FILE, prev)
        print(f"OFFLINE: could not connect to {HOST}:{PORT}", file=sys.stderr)
        sys.exit(1)

    try:
        status_regs = rhr(client, 200, 32)  # regs 00201-00232
        data_regs   = rhr(client, 300, 50)  # regs 00301-00350
        pump_regs   = rhr(client, 400, 80)  # regs 00401-00480 (8 pump blocks x 10)
    finally:
        client.close()

    # -- Sensor config -----------------------------------------------------------
    sensor_unit     = valid(status_regs[19])   # 00220: 0=bar, 1=mbar, 3=kPa
    sensor_max_raw  = valid(status_regs[21])   # 00222

    if sensor_max_raw is None:
        sensor_max_mbar = 16000  # fallback: 16 bar
    elif sensor_unit == 0:
        sensor_max_mbar = sensor_max_raw * 1000  # bar -> mbar
    elif sensor_unit == 3:
        sensor_max_mbar = sensor_max_raw * 10    # kPa -> mbar
    else:
        sensor_max_mbar = sensor_max_raw          # already mbar

    # -- Status block ------------------------------------------------------------
    status_bits   = valid(status_regs[0])    # 00201
    process_fb    = valid(status_regs[1])    # 00202: ProcessFeedback
    control_mode  = valid(status_regs[2])    # 00203
    alarm_code    = valid(status_regs[4])    # 00205
    warning_code  = valid(status_regs[5])    # 00206
    pumps_present = valid(status_regs[7])    # 00208
    pumps_running = valid(status_regs[8])    # 00209

    system_on  = bool(status_bits & (1 << 9))  if status_bits is not None else False
    alarm_act  = bool(status_bits & (1 << 10)) if status_bits is not None else False

    # -- Data block --------------------------------------------------------------
    head_raw     = valid(data_regs[0])   # 00301: Head (0.001 bar)
    flow_raw     = valid(data_regs[1])   # 00302: VolumeFlow (0.1 m3/h)
    setpoint_raw = valid(data_regs[7])   # 00308: ActualSetpoint (0.01%)
    power_hi     = valid(data_regs[11])  # 00312: PowerHI
    power_lo     = valid(data_regs[12])  # 00313: PowerLO
    inlet_raw    = valid(data_regs[14])  # 00315: InletPressure (0.001 bar)
    outlet_raw   = valid(data_regs[40])  # 00341: OutletPressure (0.001 bar)

    actual_bar   = round(head_raw * 0.001, 3) if head_raw is not None else pct_to_bar(process_fb, sensor_max_mbar)
    _sp_raw      = pct_to_bar(setpoint_raw, sensor_max_mbar)
    setpoint_bar = round(_sp_raw, 1) if _sp_raw is not None else None  # round to 1dp: 7.811 -> 7.8 -> displays as 7.80
    flow_m3h     = round(flow_raw * 0.1, 2) if flow_raw is not None else None
    inlet_bar    = round(inlet_raw * 0.001, 3) if inlet_raw is not None else None

    power_combined = hi_lo_32(power_hi, power_lo)
    total_kw = round(power_combined / 1000, 2) if power_combined is not None else None

    mode_name = CONTROL_MODE_NAMES.get(control_mode, f"mode {control_mode}") if control_mode else "unknown"

    # -- Per-pump data -----------------------------------------------------------
    def parse_pump(defn):
        ofs = defn["block"] - 400
        if ofs < 0 or ofs + 9 >= len(pump_regs):
            return _offline_pump(defn)

        bits_raw = pump_regs[ofs]
        if bits_raw is None:
            bits_raw = 0

        is_running = bool(bits_raw & 2)
        is_alarm   = bool(bits_raw & 4)
        is_present = bool((pumps_present or 0) & (1 << defn["bit"]))
        is_standby = is_present and not is_running and not is_alarm and system_on

        op_hi   = valid(pump_regs[ofs + 2])
        op_lo   = valid(pump_regs[ofs + 3])
        spd_raw = valid(pump_regs[ofs + 4])
        cur_raw = valid(pump_regs[ofs + 5])
        pwr_raw = valid(pump_regs[ofs + 6])
        tmp_raw = valid(pump_regs[ofs + 7])
        sts_hi  = valid(pump_regs[ofs + 8])
        sts_lo  = valid(pump_regs[ofs + 9])

        op_combined = hi_lo_32(op_hi, op_lo)
        sts_combined = hi_lo_32(sts_hi, sts_lo)

        return {
            **defn,
            "running":      is_running,
            "fault":        is_alarm,
            "standby":      is_standby,
            "speed_pct":    round(spd_raw * 0.01, 1) if spd_raw is not None else None,
            "power_kw":     round(pwr_raw * 10 / 1000, 2) if pwr_raw is not None else None,
            "current_a":    round(cur_raw * 0.1, 2) if cur_raw is not None else None,
            "run_hours":    round(op_combined * 0.01, 1) if op_combined is not None else None,
            "starts_total": sts_combined,
            "temp_c":       round(tmp_raw * 0.01 - 273.15, 1) if tmp_raw is not None else None,
        }

    pump_data = [parse_pump(pd) for pd in PUMP_DEFS]

    # -- Alarms ------------------------------------------------------------------
    alarms = []
    if alarm_code:
        alarms.append({"active": True, "code": alarm_code,
                        "description": f"Alarm code {alarm_code}", "pump": "System", "timestamp": now_iso})
    if warning_code:
        alarms.append({"active": True, "code": f"W{warning_code}",
                        "description": f"Warning code {warning_code}", "pump": "System", "timestamp": now_iso})

    # -- Build new reading -------------------------------------------------------
    # rh/st are cumulative counters from the CU352 non-volatile memory.
    # Daily summaries use delta(max-min) within each day for accurate hrs/starts.
    new_reading = {
        "ts": now_iso,
        "pa": actual_bar,
        "ps": setpoint_bar,
        "pi": inlet_bar,           # inlet/suction pressure bar
        "fl": flow_m3h,
        "nr": sum(1 for p in pump_data if p["running"]),
        "pk": total_kw,            # total system power kW
        "sp": [p["speed_pct"]    for p in pump_data],
        "pw": [p["power_kw"]     for p in pump_data],
        "rh": [p["run_hours"]    for p in pump_data],  # cumulative h since install
        "st": [p["starts_total"] for p in pump_data],  # cumulative starts since install
    }

    system = {
        "pressure_actual_bar":   actual_bar,
        "pressure_suction_bar":  inlet_bar,
        "pressure_setpoint_bar": setpoint_bar,
        "flow_m3h":              flow_m3h,
        "power_kw":              total_kw,
        "mode":                  mode_name,
        "fault":                 alarm_act,
        "system_on":             system_on,
    }

    # -- Update 24h history in latest.json --------------------------------------
    prev = load_json(LATEST_FILE, {"history": []})
    history_24h = prev.get("history", [])
    history_24h.append(new_reading)
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    history_24h = [r for r in history_24h if parse_ts(r["ts"]) >= cutoff_24h][-MAX_24H:]

    write_json(LATEST_FILE, {
        "connected": True,
        "timestamp": now_iso,
        "last_seen": now_iso,
        "system":    system,
        "pumps":     pump_data,
        "alarms":    alarms,
        "history":   history_24h,
    })

    # -- Update 90-day history (15-min cadence) ----------------------------------
    hist_data = load_json(HISTORY_FILE, {"readings": []})
    long_readings = hist_data.get("readings", [])

    do_append_long = True
    if long_readings:
        last_ts = parse_ts(long_readings[-1]["ts"])
        if (datetime.now(timezone.utc) - last_ts).total_seconds() < HIST_MIN_INTERVAL_S:
            do_append_long = False

    if do_append_long:
        long_readings.append(new_reading)
        cutoff_90d = datetime.now(timezone.utc) - timedelta(days=90)
        long_readings = [r for r in long_readings if parse_ts(r["ts"]) >= cutoff_90d][-MAX_90D:]
        write_json(HISTORY_FILE, {"readings": long_readings})

    # -- Update daily summaries --------------------------------------------------
    update_daily(long_readings, history_24h)

    n_run = sum(1 for p in pump_data if p["running"])
    print(f"OK: discharge={actual_bar} bar, setpoint={setpoint_bar} bar, "
          f"flow={flow_m3h} m3/h, running={n_run}/{len(pump_data)}")


def _offline_pump(defn):
    return {**defn, "running": False, "fault": False, "standby": False,
            "speed_pct": None, "power_kw": None, "current_a": None,
            "run_hours": None, "starts_total": None, "temp_c": None}


def update_daily(long_readings, short_readings):
    all_readings = long_readings + [r for r in short_readings
                                     if not long_readings or parse_ts(r["ts"]) > parse_ts(long_readings[-1]["ts"])]

    by_day = defaultdict(list)
    for r in all_readings:
        try:
            dt = parse_ts(r["ts"]) + timedelta(hours=10)  # AEST (UTC+10)
            by_day[dt.strftime("%Y-%m-%d")].append(r)
        except Exception:
            continue

    interval_h = 5 / 60  # 5-min poll interval in hours
    new_days = {}
    for day_str in sorted(by_day.keys())[-90:]:
        pts = by_day[day_str]
        pa_vals = [r["pa"] for r in pts if r.get("pa") is not None]
        fl_vals = [r["fl"] for r in pts if r.get("fl") is not None]

        hrs = []
        sts = []
        kwh = []
        for i in range(4):
            # Run hours: delta of cumulative counter (accurate regardless of poll interval)
            rh_s = [r["rh"][i] for r in pts if r.get("rh") and i < len(r["rh"]) and r["rh"][i] is not None]
            if len(rh_s) >= 2:
                hrs.append(round(max(rh_s) - min(rh_s), 2))
            elif rh_s:
                hrs.append(0.0)
            else:
                # Fallback: count 5-min windows where speed > 1%
                sp_s = [r["sp"][i] for r in pts if r.get("sp") and i < len(r["sp"]) and r["sp"][i] is not None]
                hrs.append(round(sum(1 for x in sp_s if x > 1) * interval_h, 2))

            # Starts: delta of cumulative counter
            st_s = [r["st"][i] for r in pts if r.get("st") and i < len(r["st"]) and r["st"][i] is not None]
            if len(st_s) >= 2:
                sts.append(max(st_s) - min(st_s))
            else:
                sts.append(0)

            # Energy: integrate power readings over 5-min intervals
            pw_s = [r["pw"][i] for r in pts if r.get("pw") and i < len(r["pw"]) and r["pw"][i] is not None]
            kwh.append(round(sum(pw_s) * interval_h, 2) if pw_s else 0)

        new_days[day_str] = {
            "date":     day_str,
            "p_min":    round(min(pa_vals), 3) if pa_vals else None,
            "p_max":    round(max(pa_vals), 3) if pa_vals else None,
            "p_avg":    round(sum(pa_vals) / len(pa_vals), 3) if pa_vals else None,
            "fl_max":   round(max(fl_vals), 1) if fl_vals else None,
            "fl_total": round(sum(fl_vals) * interval_h, 1) if fl_vals else None,
            "hrs":      hrs,
            "sts":      sts,
            "kwh":      kwh,
        }

    existing = load_json(DAILY_FILE, {"days": []})
    merged = {d["date"]: d for d in existing.get("days", [])}
    merged.update(new_days)
    write_json(DAILY_FILE, {"days": sorted(merged.values(), key=lambda x: x["date"])})


if __name__ == "__main__":
    main()
