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

  Data block (addr 300-363 = doc regs 00301-00364):
    300: Head (0.001 bar)
    301: VolumeFlow (0.1 m3/h)
    302: RelativePerformance (0.01%)
    305: DigitalInput bits
    306: DigitalOutput bits
    307: ActualSetpoint (0.01%)
    311: PowerHI, 312: PowerLO (combined Watts, 32-bit)
    314: InletPressure (0.001 bar)
    326: OperationTimeHI, 327: OperationTimeLO (hours, 32-bit)
    328: TotalPoweredTimeHI, 329: TotalPoweredTimeLO (hours, 32-bit)
    331: EnergyHI, 332: EnergyLO (kWh, 32-bit)
    340: OutletPressure (0.001 bar)
    344: NumberOfPowerOns
    345: SpecificEnergy (0.1 Wh/m3)
    346: SpecificEnergyAverage (0.1 Wh/m3)
    362: VolumeHI, 363: VolumeLO (0.1 m3, 32-bit)

  Pump blocks (addr 400-479 = doc regs 00401-00480, 10 regs per pump):
    +0: Status bits (bit1=OnOff/running, bit2=Alarm)
    +1: AlarmCode
    +2: OperationTimeHI, +3: OperationTimeLO (0.01 h, 32-bit)
    +4: Speed (0.01%)
    +5: LineCurrent (0.1 A)
    +6: Power (10 W)
    +7: MotorTemperature (0.01 K) - booster profile returns 0; MGE profile at device_id 2-5 addr 211
    +8: ControlSource (2=GENIbus normal, NOT starts counter)
  Pilot pump block starts at addr 460 (same 10-reg layout).

  Per-pump energy (addr 480-487 = doc regs 00481-00488, 1 kWh):
    480: Pump1, 481: Pump2, 482: Pump3, 486: Pilot, 487: Backup

  Event log (addr 6000-6282 = doc regs 06001-06283):
    6000: NoOfEventsInLog (max 40)
    6001+: 7 regs per event: EventID, Code, Source, DeviceNo, TypeAndCondition, TimestampHI, TimestampLO

  MGE single-pump profile (device_id 2-5 on same CIM500):
    addr 211 (doc 00212): MotorTemperature (0.01 K)
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
LATEST_FILE        = os.path.join(DATA_DIR, "pump_station_latest.json")
HISTORY_FILE       = os.path.join(DATA_DIR, "pump_station_history.json")
DAILY_FILE         = os.path.join(DATA_DIR, "pump_station_daily.json")
ALARM_HISTORY_FILE = os.path.join(DATA_DIR, "pump_alarm_history.json")

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

ALARM_DESCRIPTIONS = {
    10:  "GENIbus comms fault",
    15:  "Modbus watchdog timeout",
    32:  "Overvoltage",
    40:  "Undervoltage",
    45:  "Voltage asymmetry",
    48:  "Overload",
    49:  "Overcurrent",
    51:  "Blocked motor",
    56:  "Underload",
    57:  "Dry-running",
    64:  "Overtemperature",
    65:  "Motor temperature high",
    148: "PT100 bearing temperature high (DE)",
    149: "PT100 bearing temperature high (NDE)",
}

EVENT_TYPE_NAMES = {
    1: "Alarm appeared",
    2: "Alarm cleared",
    3: "Warning appeared",
    4: "Warning cleared",
}

EVENT_SOURCE_NAMES = {
    0: "System",
    6: "Pump",
    9: "Analog input",
    10: "Pilot pump",
}

PUMP_DEVICE_LABELS = {
    1: "P1", 2: "P2", 3: "P3", 4: "P4",
    5: "P5", 6: "P6", 7: "Pilot", 8: "Backup",
}


def rhr(client, addr, count):
    result = []
    while count > 0:
        chunk = min(count, 125)
        r = client.read_holding_registers(addr, count=chunk, device_id=1)
        if r.isError():
            result.extend([None] * chunk)
        else:
            result.extend(r.registers)
        addr += chunk
        count -= chunk
    return result


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


def scan_mge_temperatures(client, pump_count=4):
    """Try reading MotorTemperature from individual MGE device IDs.

    In the single-pump CIM profile (doc 6012947), addr 211 (doc 00212) = MotorTemperature (0.01 K).
    When a CIM500 is on a CU352 GENIECON, device_id 2-N may expose per-pump MGE profiles.
    Returns list of temp_c values (or None) for each pump, ordered by device_id 2, 3, 4, ...
    """
    temps = []
    for device_id in range(2, 2 + pump_count):
        try:
            r = client.read_holding_registers(211, count=1, device_id=device_id)
            if r.isError():
                temps.append(None)
                continue
            raw = r.registers[0]
            # Valid motor temp range: 0°C to 120°C = 27315 to 39315 (in 0.01K)
            if raw and raw != NA and 27315 <= raw <= 39315:
                temps.append(round(raw * 0.01 - 273.15, 1))
            else:
                temps.append(None)
        except Exception:
            temps.append(None)
    return temps


MAX_ALARM_HISTORY = 200


def update_alarm_history(pump_data, prev_pumps, alarm_code, warning_code, prev_sys, now_iso):
    """Detect alarm state transitions vs previous poll and append to persistent history.

    The CU352 GENIECON booster profile does not support the event log at addr 6000
    (returns Modbus IO exception). We build our own history by comparing each poll's
    alarm codes against the previous poll's values saved in latest.json.
    """
    history = load_json(ALARM_HISTORY_FILE, {"events": []})
    events  = history.get("events", [])
    new_events = []

    # Per-pump alarm transitions
    for i, pump in enumerate(pump_data):
        curr_code = pump.get("alarm_code") or 0
        prev_code = 0
        if i < len(prev_pumps) and prev_pumps[i]:
            prev_code = prev_pumps[i].get("alarm_code") or 0

        if curr_code == prev_code:
            continue

        if curr_code and not prev_code:
            tc, tn = 1, "Alarm appeared"
            code_to_log = curr_code
        elif prev_code and not curr_code:
            tc, tn = 2, "Alarm cleared"
            code_to_log = prev_code
        else:
            tc, tn = 1, "Alarm changed"
            code_to_log = curr_code

        new_events.append({
            "timestamp_iso": now_iso,
            "type_code":     tc,
            "type_name":     tn,
            "code":          code_to_log,
            "description":   ALARM_DESCRIPTIONS.get(code_to_log, f"Alarm code {code_to_log}"),
            "pump_label":    pump["label"],
            "source_name":   "Pump",
        })

    # System alarm transition
    prev_alarm = (prev_sys or {}).get("alarm_code") or 0
    curr_alarm = alarm_code or 0
    if curr_alarm != prev_alarm:
        if curr_alarm:
            new_events.append({
                "timestamp_iso": now_iso,
                "type_code":     1,
                "type_name":     "Alarm appeared",
                "code":          curr_alarm,
                "description":   ALARM_DESCRIPTIONS.get(curr_alarm, f"Alarm code {curr_alarm}"),
                "pump_label":    "System",
                "source_name":   "System",
            })
        elif prev_alarm:
            new_events.append({
                "timestamp_iso": now_iso,
                "type_code":     2,
                "type_name":     "Alarm cleared",
                "code":          prev_alarm,
                "description":   ALARM_DESCRIPTIONS.get(prev_alarm, f"Alarm code {prev_alarm}"),
                "pump_label":    "System",
                "source_name":   "System",
            })

    # System warning transition
    prev_warn = (prev_sys or {}).get("warning_code") or 0
    curr_warn = warning_code or 0
    if curr_warn != prev_warn:
        if curr_warn:
            new_events.append({
                "timestamp_iso": now_iso,
                "type_code":     3,
                "type_name":     "Warning appeared",
                "code":          curr_warn,
                "description":   ALARM_DESCRIPTIONS.get(curr_warn, f"Warning code {curr_warn}"),
                "pump_label":    "System",
                "source_name":   "System",
            })
        elif prev_warn:
            new_events.append({
                "timestamp_iso": now_iso,
                "type_code":     4,
                "type_name":     "Warning cleared",
                "code":          prev_warn,
                "description":   ALARM_DESCRIPTIONS.get(prev_warn, f"Warning code {prev_warn}"),
                "pump_label":    "System",
                "source_name":   "System",
            })

    combined = (new_events + events)[:MAX_ALARM_HISTORY]
    write_json(ALARM_HISTORY_FILE, {"events": combined})
    return combined[:40]


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    # Load previous state before connecting - needed for alarm transition detection
    prev = load_json(LATEST_FILE, {"history": []})

    client = ModbusTcpClient(HOST, port=PORT, timeout=10)
    connected = client.connect()
    now_iso = datetime.now(timezone.utc).isoformat()

    if not connected:
        prev["connected"] = False
        prev["last_seen"]  = prev.get("timestamp")
        prev["timestamp"]  = now_iso
        write_json(LATEST_FILE, prev)
        print(f"OFFLINE: could not connect to {HOST}:{PORT}")
        sys.exit(0)

    try:
        status_regs   = rhr(client, 200, 32)   # regs 00201-00232
        data_regs     = rhr(client, 300, 64)   # regs 00301-00364 (extended from 50 to 64)
        pump_regs     = rhr(client, 400, 80)   # regs 00401-00480 (8 pump blocks x 10)
        energy_regs   = rhr(client, 480, 8)    # per-pump energy kWh (00481-00488)
        ain_unit_regs = rhr(client, 224, 7)    # regs 00225-00231: AnalogIn1-7 unit codes
        ain_val_regs  = rhr(client, 375, 7)    # regs 00376-00382: AnalogIn1-7 values
        mge_temps     = scan_mge_temperatures(client, len(PUMP_DEFS))
    finally:
        client.close()

    # -- Sensor config -----------------------------------------------------------
    sensor_unit     = valid(status_regs[19])   # 00220: 0=bar, 1=mbar, 3=kPa
    sensor_max_raw  = valid(status_regs[21])   # 00222

    if sensor_max_raw is None:
        sensor_max_mbar = 16000
    elif sensor_unit == 0:
        sensor_max_mbar = sensor_max_raw * 1000
    elif sensor_unit == 3:
        sensor_max_mbar = sensor_max_raw * 10
    else:
        sensor_max_mbar = sensor_max_raw

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
    rel_perf_raw = valid(data_regs[2])   # 00303: RelativePerformance (0.01%)
    di_raw       = valid(data_regs[5])   # 00306: DigitalInput bits
    do_raw       = valid(data_regs[6])   # 00307: DigitalOutput bits
    setpoint_raw = valid(data_regs[7])   # 00308: ActualSetpoint (0.01%)
    power_hi     = valid(data_regs[11])  # 00312: PowerHI
    power_lo     = valid(data_regs[12])  # 00313: PowerLO
    inlet_raw    = valid(data_regs[14])  # 00315: InletPressure (0.001 bar)
    outlet_raw   = valid(data_regs[40])  # 00341: OutletPressure (0.001 bar)

    op_time_hi      = valid(data_regs[26])  # 00327: OperationTimeHI (hours)
    op_time_lo      = valid(data_regs[27])  # 00328: OperationTimeLO (hours)
    powered_hi      = valid(data_regs[28])  # 00329: TotalPoweredTimeHI (hours)
    powered_lo      = valid(data_regs[29])  # 00330: TotalPoweredTimeLO (hours)
    sys_energy_hi   = valid(data_regs[31])  # 00332: EnergyHI (kWh)
    sys_energy_lo   = valid(data_regs[32])  # 00333: EnergyLO (kWh)
    power_ons_raw   = valid(data_regs[44])  # 00345: NumberOfPowerOns
    spec_e_raw      = valid(data_regs[45])  # 00346: SpecificEnergy (0.1 Wh/m3)
    spec_e_avg_raw  = valid(data_regs[46])  # 00347: SpecificEnergyAverage (0.1 Wh/m3)
    volume_hi       = valid(data_regs[62])  # 00363: VolumeHI (0.1 m3)
    volume_lo       = valid(data_regs[63])  # 00364: VolumeLO (0.1 m3)

    actual_bar   = round(head_raw * 0.001, 3) if head_raw is not None else pct_to_bar(process_fb, sensor_max_mbar)
    _sp_raw      = pct_to_bar(setpoint_raw, sensor_max_mbar)
    setpoint_bar = round(_sp_raw, 1) if _sp_raw is not None else None
    flow_m3h     = round(flow_raw * 0.1, 2) if flow_raw is not None else None
    inlet_bar    = round(inlet_raw * 0.001, 3) if inlet_raw is not None else None
    rel_perf_pct = round(rel_perf_raw * 0.01, 1) if rel_perf_raw is not None else None

    power_combined = hi_lo_32(power_hi, power_lo)
    total_kw = round(power_combined / 1000, 2) if power_combined is not None else None

    sys_run_hours     = hi_lo_32(op_time_hi, op_time_lo)
    sys_powered_hours = hi_lo_32(powered_hi, powered_lo)
    sys_energy_kwh    = hi_lo_32(sys_energy_hi, sys_energy_lo)
    spec_energy       = round(spec_e_raw * 0.1, 1) if spec_e_raw is not None else None
    spec_energy_avg   = round(spec_e_avg_raw * 0.1, 1) if spec_e_avg_raw is not None else None
    volume_combined   = hi_lo_32(volume_hi, volume_lo)
    volume_m3         = round(volume_combined * 0.1, 1) if volume_combined is not None else None

    mode_name = CONTROL_MODE_NAMES.get(control_mode, f"mode {control_mode}") if control_mode else "unknown"

    # -- Per-pump energy ---------------------------------------------------------
    pump_energy_kwh = []
    energy_map = {0: 0, 1: 1, 2: 2, 3: 6}  # PUMP_DEFS index -> energy_regs index
    for i in range(len(PUMP_DEFS)):
        ereg_idx = energy_map.get(i)
        if ereg_idx is not None and ereg_idx < len(energy_regs):
            raw = valid(energy_regs[ereg_idx])
            pump_energy_kwh.append(raw)
        else:
            pump_energy_kwh.append(None)

    # -- Per-pump data -----------------------------------------------------------
    def parse_pump(defn, idx):
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
        alarm_c = valid(pump_regs[ofs + 1])

        op_combined = hi_lo_32(op_hi, op_lo)

        # Motor temperature: prefer MGE device_id scan, fall back to booster profile (+7)
        mge_t = mge_temps[idx] if mge_temps and idx < len(mge_temps) else None
        if mge_t is not None:
            temp_c = mge_t
        elif tmp_raw is not None and tmp_raw > 0:
            temp_c = round(tmp_raw * 0.01 - 273.15, 1)
        else:
            temp_c = None

        return {
            **defn,
            "running":      is_running,
            "fault":        is_alarm,
            "standby":      is_standby,
            "speed_pct":    round(spd_raw * 0.01, 1) if spd_raw is not None else None,
            "power_kw":     round(pwr_raw * 10 / 1000, 2) if pwr_raw is not None else None,
            "current_a":    round(cur_raw * 0.1, 2) if cur_raw is not None else None,
            "run_hours":    round(op_combined * 0.01, 1) if op_combined is not None else None,
            "starts_total": None,   # not available in booster Modbus profile
            "temp_c":       temp_c,
            "alarm_code":   alarm_c,
        }

    pump_data = [parse_pump(pd, i) for i, pd in enumerate(PUMP_DEFS)]

    # -- Analog inputs (AnalogIn1-7) ---------------------------------------------
    TEMP_UNIT_CODES = {10, 13, 84, 110}
    analog_inputs = []
    for i in range(7):
        u_raw   = ain_unit_regs[i] if ain_unit_regs else None
        v_raw   = ain_val_regs[i]  if ain_val_regs  else None
        u_valid = valid(u_raw)
        v_valid = valid(v_raw)
        is_temp = u_valid in TEMP_UNIT_CODES
        if is_temp and v_valid is not None:
            temp_c = round(v_valid * 0.01 - 273.15, 1) if u_valid in (13, 84) else round(v_valid * 0.01, 1)
        else:
            temp_c = None
        analog_inputs.append({
            "index":  i + 1,
            "unit":   u_valid,
            "raw":    v_valid,
            "temp_c": temp_c,
        })

    # -- Alarms ------------------------------------------------------------------
    alarms = []
    if alarm_code:
        alarms.append({"active": True, "code": alarm_code,
                        "description": ALARM_DESCRIPTIONS.get(alarm_code, f"Alarm code {alarm_code}"),
                        "pump": "System", "timestamp": now_iso})
    if warning_code:
        alarms.append({"active": True, "code": f"W{warning_code}",
                        "description": ALARM_DESCRIPTIONS.get(warning_code, f"Warning code {warning_code}"),
                        "pump": "System", "timestamp": now_iso})

    # -- Build new reading -------------------------------------------------------
    new_reading = {
        "ts": now_iso,
        "pa": actual_bar,
        "ps": setpoint_bar,
        "pi": inlet_bar,
        "fl": flow_m3h,
        "nr": sum(1 for p in pump_data if p["running"]),
        "pk": total_kw,
        "sp": [p["speed_pct"]    for p in pump_data],
        "pw": [p["power_kw"]     for p in pump_data],
        "rh": [p["run_hours"]    for p in pump_data],
        "st": [p["starts_total"] for p in pump_data],
        "tm": [p["temp_c"]       for p in pump_data],
        "se": spec_energy,
    }

    system = {
        "pressure_actual_bar":       actual_bar,
        "pressure_suction_bar":      inlet_bar,
        "pressure_setpoint_bar":     setpoint_bar,
        "flow_m3h":                  flow_m3h,
        "power_kw":                  total_kw,
        "mode":                      mode_name,
        "fault":                     alarm_act,
        "system_on":                 system_on,
        "relative_performance_pct":  rel_perf_pct,
        "specific_energy_wh_m3":     spec_energy,
        "specific_energy_avg_wh_m3": spec_energy_avg,
        "energy_kwh":                sys_energy_kwh,
        "run_hours":                 sys_run_hours,
        "powered_hours":             sys_powered_hours,
        "power_ons":                 power_ons_raw,
        "volume_m3":                 volume_m3,
        "di_bits":                   di_raw,
        "do_bits":                   do_raw,
        "alarm_code":                alarm_code or 0,
        "warning_code":              warning_code or 0,
    }

    # -- Alarm history (local transition tracking - CU352 addr 6000 not supported on GENIECON)
    event_log = update_alarm_history(
        pump_data,
        prev.get("pumps", []),
        alarm_code or 0,
        warning_code or 0,
        prev.get("system"),
        now_iso,
    )

    # -- Update 24h history in latest.json --------------------------------------
    history_24h = prev.get("history", [])
    history_24h.append(new_reading)
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    history_24h = [r for r in history_24h if parse_ts(r["ts"]) >= cutoff_24h][-MAX_24H:]

    write_json(LATEST_FILE, {
        "connected":       True,
        "timestamp":       now_iso,
        "last_seen":       now_iso,
        "system":          system,
        "pumps":           pump_data,
        "alarms":          alarms,
        "analog_inputs":   analog_inputs,
        "history":         history_24h,
        "event_log":       event_log,
        "pump_energy_kwh": pump_energy_kwh,
        "mge_temps":       mge_temps,
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
    mge_str = ", ".join(f"P{i+1}={t}C" for i, t in enumerate(mge_temps) if t is not None) or "none"
    print(f"OK: discharge={actual_bar} bar, setpoint={setpoint_bar} bar, "
          f"flow={flow_m3h} m3/h, running={n_run}/{len(pump_data)}, "
          f"spec_energy={spec_energy} Wh/m3, mge_temps=[{mge_str}], "
          f"alarm_history={len(event_log)} events")


def _offline_pump(defn):
    return {**defn, "running": False, "fault": False, "standby": False,
            "speed_pct": None, "power_kw": None, "current_a": None,
            "run_hours": None, "starts_total": None, "temp_c": None, "alarm_code": None}


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
        se_vals = [r["se"] for r in pts if r.get("se") is not None]

        hrs = []
        sts = []
        kwh = []
        for i in range(4):
            rh_s = [r["rh"][i] for r in pts if r.get("rh") and i < len(r["rh"]) and r["rh"][i] is not None]
            if len(rh_s) >= 2:
                hrs.append(round(max(rh_s) - min(rh_s), 2))
            elif rh_s:
                hrs.append(0.0)
            else:
                sp_s = [r["sp"][i] for r in pts if r.get("sp") and i < len(r["sp"]) and r["sp"][i] is not None]
                hrs.append(round(sum(1 for x in sp_s if x > 1) * interval_h, 2))

            sts.append(0)  # starts counter not available in booster Modbus profile

            pw_s = [r["pw"][i] for r in pts if r.get("pw") and i < len(r["pw"]) and r["pw"][i] is not None]
            kwh.append(round(sum(pw_s) * interval_h, 2) if pw_s else 0)

        new_days[day_str] = {
            "date":     day_str,
            "p_min":    round(min(pa_vals), 3) if pa_vals else None,
            "p_max":    round(max(pa_vals), 3) if pa_vals else None,
            "p_avg":    round(sum(pa_vals) / len(pa_vals), 3) if pa_vals else None,
            "fl_max":   round(max(fl_vals), 1) if fl_vals else None,
            "fl_total": round(sum(fl_vals) * interval_h, 1) if fl_vals else None,
            "se_avg":   round(sum(se_vals) / len(se_vals), 1) if se_vals else None,
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
