#!/usr/bin/env python3
"""
Grundfos 1-second Modbus poller for Raspberry Pi.

Collects the same register set as the GitHub Actions 5-min poller, plus
per-pump speed, current, power, motor temperature (MGE scan), CIM identity,
analog inputs, and slow-changing config written hourly to pump_config_snapshots.

Fast poll (every second):  status, data block, pump blocks, energy kWh
Slow poll (every 60s):     CIM firmware, analog inputs, MGE motor temperatures
Config snapshot (hourly):  all slow data + tuning params -> pump_config_snapshots

Setup:
    mkdir ~/pump_poller && cd ~/pump_poller
    python3 -m venv venv
    venv/bin/pip install pymodbus requests
    wget https://raw.githubusercontent.com/Bidgee182/wwcc-weather-page/main/scripts/pi_pump_poller.py
    venv/bin/python3 pi_pump_poller.py

Run as a systemd service so it starts on boot:
    sudo cp pump-poller.service /etc/systemd/system/
    sudo systemctl enable --now pump-poller
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

try:
    import requests
    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False
    print("WARNING: 'requests' not installed - Supabase upload disabled. Run: pip3 install requests")

from pymodbus.client import ModbusTcpClient

# ── Configuration ──────────────────────────────────────────────────────────────
GRUNDFOS_HOST = os.environ.get("GRUNDFOS_HOST", "bidgee-pumps.duckdns.org")
GRUNDFOS_PORT = int(os.environ.get("GRUNDFOS_PORT", "502"))

SUPABASE_URL = "https://sduzxijjvpbfgvlwcwpp.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNkdXp4aWpqdnBiZmd2bHdjd3BwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY1ODE2NzgsImV4cCI6MjA5MjE1NzY3OH0.fbYf9-F987DUSlsibuGnqGYEQe6tsQsOf7NMmNMrBT8"

POLLER_VERSION = "2.3"   # keep == PUMP_VERSION in pump-station.html; bump on ANY pump page/poller change

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pump_local.db")

POLL_INTERVAL_S   = 1.0
RECONNECT_WAIT    = 5
SLOW_POLL_EVERY   = 60    # polls: CIM + analog inputs + MGE motor temps
CONFIG_SNAP_EVERY = 3600  # polls: hourly config snapshot to Supabase
NA = 65535

PUMP_DEFS = [
    {"id": 1, "label": "P1 - CRNE45", "block": 400, "bit": 0, "energy_idx": 0},
    {"id": 2, "label": "P2 - CRNE45", "block": 410, "bit": 1, "energy_idx": 1},
    {"id": 3, "label": "P3 - CRNE45", "block": 420, "bit": 2, "energy_idx": 2},
    {"id": 4, "label": "Jockey",       "block": 460, "bit": 6, "energy_idx": 6},
]

ALARM_DESCRIPTIONS = {
    4:   "Too many restarts",
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
    190: "Limit 1 exceeded",
    210: "Pressure high",
    214: "Water shortage",
}

CONTROL_MODE_NAMES = {
    1: "Constant differential pressure",
    2: "Constant pressure setpoint",
    3: "Constant head",
    4: "Constant pressure",
    5: "Constant flow",
    6: "Constant temperature",
    7: "Duty-standby",
    8: "Constant level",
}

TEMP_UNIT_CODES = {10, 13, 84, 110}  # analog input unit codes that indicate temperature

# Pressure spike thresholds — events written to pump_events_hires with exact timestamps
PRESSURE_HIGH_BAR = 8.5   # outlet overpressure (setpoint is ~7.8 bar)
PRESSURE_LOW_BAR  = 4.5   # outlet underpressure when system is running
SUCTION_LOW_BAR   = -0.90 # suction near B2 transmitter floor (-1.0 bar min)


# ── SQLite setup ───────────────────────────────────────────────────────────────

def init_db(path):
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id           INTEGER PRIMARY KEY,
            ts           TEXT NOT NULL,
            system_on    INTEGER,
            alarm        INTEGER,
            pressure     REAL,
            inlet        REAL,
            outlet       REAL,
            flow         REAL,
            power_kw     REAL,
            spec_energy  REAL,
            sys_run_hrs  REAL,
            sys_energy   INTEGER,
            volume_m3    REAL,
            tank_ok      INTEGER,
            p1_run       INTEGER DEFAULT 0,
            p2_run       INTEGER DEFAULT 0,
            p3_run       INTEGER DEFAULT 0,
            jk_run       INTEGER DEFAULT 0,
            p1_alarm     INTEGER,
            p2_alarm     INTEGER,
            p3_alarm     INTEGER,
            jk_alarm     INTEGER,
            p1_hours     REAL,
            p2_hours     REAL,
            p3_hours     REAL,
            jk_hours     REAL,
            p1_kw        REAL,
            p2_kw        REAL,
            p3_kw        REAL,
            jk_kw        REAL,
            p1_speed     REAL,
            p2_speed     REAL,
            p3_speed     REAL,
            jk_speed     REAL,
            p1_current   REAL,
            p2_current   REAL,
            p3_current   REAL,
            jk_current   REAL,
            p1_kwh       INTEGER,
            p2_kwh       INTEGER,
            p3_kwh       INTEGER,
            jk_kwh       INTEGER
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts DESC)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS supabase_queue (
            id         INTEGER PRIMARY KEY,
            table_name TEXT NOT NULL,
            payload    TEXT NOT NULL,
            prefer     TEXT DEFAULT 'return=minimal',
            created    TEXT NOT NULL,
            attempts   INTEGER DEFAULT 0
        )
    """)
    con.commit()
    return con


def save_reading(con, ts_iso, state):
    pumps = state.get("pumps", [])

    def pv(i, k):
        return pumps[i].get(k) if i < len(pumps) else None

    con.execute("""INSERT INTO readings
        (ts, system_on, alarm, pressure, inlet, outlet, flow, power_kw,
         spec_energy, sys_run_hrs, sys_energy, volume_m3, tank_ok,
         p1_run, p2_run, p3_run, jk_run,
         p1_alarm, p2_alarm, p3_alarm, jk_alarm,
         p1_hours, p2_hours, p3_hours, jk_hours,
         p1_kw, p2_kw, p3_kw, jk_kw,
         p1_speed, p2_speed, p3_speed, jk_speed,
         p1_current, p2_current, p3_current, jk_current,
         p1_kwh, p2_kwh, p3_kwh, jk_kwh)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?,?,
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        ts_iso,
        1 if state.get("system_on") else 0,
        state.get("alarm_code"),
        state.get("pressure"),
        state.get("inlet"),
        state.get("outlet"),
        state.get("flow"),
        state.get("power_kw"),
        state.get("spec_energy"),
        state.get("sys_run_hours"),
        state.get("sys_energy_kwh"),
        state.get("volume_m3"),
        1 if state.get("tank_ok") else (0 if state.get("tank_ok") is False else None),
        1 if pv(0, "running") else 0,
        1 if pv(1, "running") else 0,
        1 if pv(2, "running") else 0,
        1 if pv(3, "running") else 0,
        pv(0, "alarm_code"), pv(1, "alarm_code"),
        pv(2, "alarm_code"), pv(3, "alarm_code"),
        pv(0, "run_hours"),  pv(1, "run_hours"),
        pv(2, "run_hours"),  pv(3, "run_hours"),
        pv(0, "power_kw"),   pv(1, "power_kw"),
        pv(2, "power_kw"),   pv(3, "power_kw"),
        pv(0, "speed_pct"),  pv(1, "speed_pct"),
        pv(2, "speed_pct"),  pv(3, "speed_pct"),
        pv(0, "current_a"),  pv(1, "current_a"),
        pv(2, "current_a"),  pv(3, "current_a"),
        pv(0, "kwh"),        pv(1, "kwh"),
        pv(2, "kwh"),        pv(3, "kwh"),
    ))
    con.commit()


def prune_old_readings(con):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    con.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
    con.commit()


def queue_event(con, ts_iso, event_type, pump_label=None,
                alarm_code=None, alarm_desc=None, details=None):
    payload = {
        "ts":         ts_iso,
        "event_type": event_type,
        "pump_label": pump_label,
        "alarm_code": alarm_code,
        "alarm_desc": alarm_desc,
        "details":    details,
    }
    con.execute(
        "INSERT INTO supabase_queue (table_name, payload, prefer, created) VALUES (?,?,?,?)",
        ("pump_events_hires", json.dumps(payload),
         "return=minimal", datetime.now(timezone.utc).isoformat())
    )
    con.commit()


def queue_minute_stat(con, stat):
    con.execute(
        "INSERT INTO supabase_queue (table_name, payload, prefer, created) VALUES (?,?,?,?)",
        ("pump_minute_stats", json.dumps(stat),
         "resolution=merge-duplicates,return=minimal",
         datetime.now(timezone.utc).isoformat())
    )
    con.commit()


def queue_config_snapshot(con, ts_iso, state, slow_state):
    """Queue an hourly config snapshot to pump_config_snapshots."""
    mge = slow_state.get("mge_temps") or []
    payload = {
        "ts":               ts_iso,
        "mode":             state.get("mode"),
        "setpoint_bar":     state.get("setpoint"),
        "pid_kp":           state.get("pid_kp"),
        "pid_ti":           state.get("pid_ti"),
        "sensor_max_bar":   state.get("sensor_max_bar"),
        "sys_run_hours":    state.get("sys_run_hours"),
        "sys_powered_hrs":  state.get("sys_powered_hours"),
        "power_ons":        state.get("power_ons"),
        "spec_energy_avg_lt": state.get("spec_energy_avg"),
        "di_bits":          state.get("di_bits"),
        "do_bits":          state.get("do_bits"),
        "cim_unit_family":  slow_state.get("cim_unit_family"),
        "cim_unit_type":    slow_state.get("cim_unit_type"),
        "cim_unit_version": slow_state.get("cim_unit_version"),
        "cim_fw":           slow_state.get("cim_fw"),
        "analog_inputs":    slow_state.get("analog_inputs"),
        "mge_temps": {
            "p1":     mge[0] if len(mge) > 0 else None,
            "p2":     mge[1] if len(mge) > 1 else None,
            "p3":     mge[2] if len(mge) > 2 else None,
            "jockey": mge[3] if len(mge) > 3 else None,
        },
    }
    con.execute(
        "INSERT INTO supabase_queue (table_name, payload, prefer, created) VALUES (?,?,?,?)",
        ("pump_config_snapshots", json.dumps(payload),
         "return=minimal", datetime.now(timezone.utc).isoformat())
    )
    con.commit()


def flush_supabase_queue(con, session):
    if not HAVE_REQUESTS:
        return
    rows = con.execute(
        "SELECT id, table_name, payload, prefer FROM supabase_queue"
        " WHERE attempts < 20 ORDER BY id LIMIT 30"
    ).fetchall()
    for row_id, table, payload_str, prefer in rows:
        try:
            url = f"{SUPABASE_URL}/rest/v1/{table}"
            headers = {
                "apikey":        SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        prefer,
            }
            r = session.post(url, data=payload_str, headers=headers, timeout=10)
            if r.status_code in (200, 201, 204):
                con.execute("DELETE FROM supabase_queue WHERE id=?", (row_id,))
            else:
                con.execute(
                    "UPDATE supabase_queue SET attempts=attempts+1 WHERE id=?", (row_id,))
                print(f"  Supabase {table} {r.status_code}: {r.text[:120]}")
        except Exception as e:
            con.execute(
                "UPDATE supabase_queue SET attempts=attempts+1 WHERE id=?", (row_id,))
            print(f"  Supabase queue error: {e}")
    con.commit()


# ── Modbus helpers ─────────────────────────────────────────────────────────────

def valid(raw):
    return None if raw is None or raw == NA else raw


def hi_lo_32(hi, lo):
    if hi is None or lo is None:
        return None
    return hi * 65536 + lo


def rhr(client, addr, count, device_id=1):
    try:
        r = client.read_holding_registers(addr, count=count, device_id=device_id)
        if r.isError():
            return [None] * count
        return list(r.registers)
    except Exception:
        return [None] * count


def scan_mge_temperatures(client, pump_count=4):
    """Read PumpLiquidTemp from MGE single-pump profiles at device_id 2..pump_count+1.
    Register addr 321 (doc 00322) = PumpLiquidTemp (0.01 K) - requires PT100 configured
    in GO app as Liquid temperature. Valid range 0-120C = 27315-39315 in 0.01K.
    Returns list of temp_c or None per pump."""
    temps = []
    for device_id in range(2, 2 + pump_count):
        try:
            r = client.read_holding_registers(321, count=1, device_id=device_id)
            if r.isError():
                temps.append(None)
                continue
            raw = r.registers[0]
            if raw and raw != NA and 27315 <= raw <= 39315:
                temps.append(round(raw * 0.01 - 273.15, 1))
            else:
                temps.append(None)
        except Exception:
            temps.append(None)
    return temps


# ── Fast poll (every second) ───────────────────────────────────────────────────

def poll_once(client):
    """Read all fast registers. Returns state dict or None on error."""
    try:
        status     = rhr(client, 200, 32)  # addr 200-231: status block + PI params
        if status[0] is None:
            return None
        data       = rhr(client, 300, 64)  # addr 300-363: full data block
        pumps_raw  = rhr(client, 400, 80)  # addr 400-479: pump blocks (P1/P2/P3 + Jockey)
        energy_raw = rhr(client, 480, 8)   # addr 480-487: per-pump lifetime energy kWh
    except Exception as e:
        print(f"  Modbus read error: {e}")
        return None

    # ── Status block (addr 200-231) ────────────────────────────────────────────
    status_bits      = valid(status[0])   # addr 200: status bitfield
    process_fb       = valid(status[1])   # addr 201: ProcessFeedback (0.01% of sensor max)
    control_mode_raw = valid(status[2])   # addr 202: control mode code
    alarm_code       = valid(status[4])   # addr 204
    warning_code     = valid(status[5])   # addr 205
    pumps_present    = valid(status[7])   # addr 207
    pumps_comm_fault = valid(status[10])  # addr 210: per-pump GENIbus comm fault bitmask
    sys_active_funcs = valid(status[11])  # addr 211: emergency run, low-flow stop, etc.
    sensor_unit      = valid(status[19])  # addr 219: 0=bar, 1=mbar, 3=kPa
    sensor_max_raw   = valid(status[21])  # addr 221
    kp_raw           = valid(status[30])  # addr 230: PI controller Kp (scale 0.1)
    ti_raw           = valid(status[31])  # addr 231: PI controller Ti seconds (scale 0.1)

    system_on = bool(status_bits & (1 << 9)) if status_bits is not None else False

    if sensor_max_raw is None:
        sensor_max_mbar = 16000
    elif sensor_unit == 0:
        sensor_max_mbar = sensor_max_raw * 1000
    elif sensor_unit == 3:
        sensor_max_mbar = sensor_max_raw * 10
    else:
        sensor_max_mbar = sensor_max_raw

    # ── Data block (addr 300-363) ──────────────────────────────────────────────
    head_raw       = valid(data[0])    # addr 300: Head (0.001 bar)
    flow_raw       = valid(data[1])    # addr 301: VolumeFlow (0.1 m3/h)
    rel_perf_raw   = valid(data[2])    # addr 302: RelativePerformance (0.01%)
    di_raw         = valid(data[5])    # addr 305: DigitalInput bits
    do_raw         = valid(data[6])    # addr 306: DigitalOutput bits
    setpt_raw      = valid(data[7])    # addr 307: ActualSetpoint (0.01%, incl analog influence)
    usersetpt_raw  = valid(data[42])   # addr 342: UserSetpoint (0.01%, before any modification)
    power_hi       = valid(data[11])   # addr 311: PowerHI
    power_lo       = valid(data[12])   # addr 312: PowerLO
    inlet_raw      = valid(data[14])   # addr 314: InletPressure (0.001 bar, offset -1.0)
    op_time_hi     = valid(data[26])   # addr 326: System OperationTimeHI (hours)
    op_time_lo     = valid(data[27])   # addr 327: System OperationTimeLO
    powered_hi     = valid(data[28])   # addr 328: TotalPoweredTimeHI (hours)
    powered_lo     = valid(data[29])   # addr 329: TotalPoweredTimeLO
    energy_hi      = valid(data[31])   # addr 331: System EnergyHI (kWh)
    energy_lo      = valid(data[32])   # addr 332: System EnergyLO
    outlet_raw     = valid(data[40])   # addr 340: OutletPressure (0.001 bar)
    power_ons_raw  = valid(data[44])   # addr 344: NumberOfPowerOns
    spec_e_raw     = valid(data[45])   # addr 345: SpecificEnergy instantaneous (0.1 Wh/m3)
    spec_e_avg_raw = valid(data[46])   # addr 346: SpecificEnergyAverage long-term (0.1 Wh/m3)
    volume_hi      = valid(data[62])   # addr 362: VolumeHI (0.1 m3)
    volume_lo      = valid(data[63])   # addr 363: VolumeLO

    # Head preferred; fall back to ProcessFeedback if head returns NA (system idle)
    if head_raw is not None:
        pressure = round(head_raw * 0.001, 3)
    elif process_fb is not None and sensor_max_mbar:
        pressure = round(process_fb * 0.0001 * sensor_max_mbar / 1000, 3)
    else:
        pressure = None

    flow_m3h  = round(flow_raw * 0.1, 2)            if flow_raw  is not None else None
    # B2 transmitter range -1.0 to 6.0 bar: raw=0 -> -1.0 bar
    inlet     = round(inlet_raw * 0.001 - 1.0, 3)   if inlet_raw is not None else None
    outlet    = round(outlet_raw * 0.001, 3)         if outlet_raw is not None else None

    power_combined = hi_lo_32(power_hi, power_lo)
    power_kw = round(power_combined / 1000, 2) if power_combined is not None else None

    # raw is in 0.01% of sensor range; divide by 10000 to get fraction, then * sensor_max_bar.
    # Prefer UserSetpoint (342) - the configured target the CU352 shows (e.g. 7.80) - over
    # ActualSetpoint (307), which drifts with analog influence and rounds to 7.81.
    _sp_src   = usersetpt_raw if usersetpt_raw is not None else setpt_raw
    setpoint  = round(_sp_src * 0.0001 * sensor_max_mbar / 1000, 2) \
                if _sp_src is not None and sensor_max_mbar else None

    sys_run_hours     = hi_lo_32(op_time_hi, op_time_lo)
    sys_powered_hours = hi_lo_32(powered_hi, powered_lo)
    sys_energy_kwh    = hi_lo_32(energy_hi, energy_lo)
    volume_combined   = hi_lo_32(volume_hi, volume_lo)

    volume_m3         = round(volume_combined * 0.1, 1)  if volume_combined   is not None else None
    spec_energy       = round(spec_e_raw * 0.1, 1)       if spec_e_raw        is not None else None
    spec_energy_avg   = round(spec_e_avg_raw * 0.1, 1)   if spec_e_avg_raw    is not None else None
    rel_perf_pct      = round(rel_perf_raw * 0.01, 1)    if rel_perf_raw      is not None else None
    pid_kp            = round(kp_raw * 0.1, 1)           if kp_raw            is not None else None
    pid_ti            = round(ti_raw * 0.1, 1)           if ti_raw            is not None else None
    sensor_max_bar    = round(sensor_max_mbar / 1000, 1) if sensor_max_mbar               else None
    mode              = CONTROL_MODE_NAMES.get(control_mode_raw,
                            f"mode {control_mode_raw}") if control_mode_raw else "unknown"

    # DI bit 1 = tank float valve: 1=tank OK, 0=tank low / water shortage
    tank_ok = bool(di_raw & 0x02) if di_raw is not None else None

    # ── Per-pump blocks (addr 400-479) ─────────────────────────────────────────
    pumps = []
    for defn in PUMP_DEFS:
        ofs = defn["block"] - 400

        bits_raw = pumps_raw[ofs]            if ofs     < len(pumps_raw) else None
        alarm_c  = valid(pumps_raw[ofs + 1]) if ofs + 1 < len(pumps_raw) else None
        op_hi    = valid(pumps_raw[ofs + 2]) if ofs + 2 < len(pumps_raw) else None
        op_lo    = valid(pumps_raw[ofs + 3]) if ofs + 3 < len(pumps_raw) else None
        spd_raw  = valid(pumps_raw[ofs + 4]) if ofs + 4 < len(pumps_raw) else None
        cur_raw  = valid(pumps_raw[ofs + 5]) if ofs + 5 < len(pumps_raw) else None
        pwr_raw  = valid(pumps_raw[ofs + 6]) if ofs + 6 < len(pumps_raw) else None

        is_running = bool((bits_raw or 0) & 2)
        is_present = bool((pumps_present or 0) & (1 << defn["bit"]))
        op_hours   = hi_lo_32(op_hi, op_lo)

        eidx     = defn["energy_idx"]
        pump_kwh = valid(energy_raw[eidx]) if eidx < len(energy_raw) else None

        pumps.append({
            "id":        defn["id"],
            "label":     defn["label"],
            "running":   is_running,
            "present":   is_present,
            "alarm_code": alarm_c,
            "run_hours": float(op_hours) if op_hours is not None else None,
            "speed_pct": round(spd_raw * 0.01, 1)      if spd_raw is not None else None,
            "current_a": round(cur_raw * 0.1, 2)        if cur_raw is not None else None,
            "power_kw":  round(pwr_raw * 10 / 1000, 2) if pwr_raw is not None else None,
            "kwh":       pump_kwh,
            "temp_c":    None,  # filled from slow poll MGE scan
        })

    return {
        "system_on":          system_on,
        "alarm_code":         alarm_code or 0,
        "warning_code":       warning_code or 0,
        "pumps_comm_fault":   pumps_comm_fault or 0,
        "sys_active_funcs":   sys_active_funcs or 0,
        "power_ons":          power_ons_raw,
        "pressure":           pressure,
        "setpoint":           setpoint,
        "inlet":              inlet,
        "outlet":             outlet,
        "flow":               flow_m3h,
        "power_kw":           power_kw,
        "spec_energy":        spec_energy,
        "spec_energy_avg":    spec_energy_avg,
        "sys_run_hours":      float(sys_run_hours)     if sys_run_hours     is not None else None,
        "sys_powered_hours":  float(sys_powered_hours) if sys_powered_hours is not None else None,
        "sys_energy_kwh":     sys_energy_kwh,
        "volume_m3":          volume_m3,
        "tank_ok":            tank_ok,
        "di_bits":            di_raw,
        "do_bits":            do_raw,
        "mode_code":          control_mode_raw,
        "mode":               mode,
        "rel_perf_pct":       rel_perf_pct,
        "pid_kp":             pid_kp,
        "pid_ti":             pid_ti,
        "sensor_max_bar":     sensor_max_bar,
        "pumps":              pumps,
    }


# ── Slow poll (every 60 seconds) ──────────────────────────────────────────────

def slow_poll_once(client):
    """Read CIM identity, analog inputs 1-7, and MGE motor temperatures.
    These change slowly so polling every 60s is sufficient."""
    try:
        cim_regs  = rhr(client, 29, 8)    # addr 29-36: CIM unit family/type/ver + FW
        ain_units = rhr(client, 224, 7)   # addr 224-230: AnalogIn1-7 unit codes
        ain_vals  = rhr(client, 375, 7)   # addr 375-381: AnalogIn1-7 values
        mge_temps = scan_mge_temperatures(client, len(PUMP_DEFS))
    except Exception as e:
        print(f"  slow_poll error: {e}")
        return None

    cim_unit_family  = valid(cim_regs[0]) if cim_regs else None  # 21=Hydro MPC/GENIECON
    cim_unit_type    = valid(cim_regs[1]) if cim_regs else None  # 4=GENIECON
    cim_unit_version = valid(cim_regs[2]) if cim_regs else None
    cim_fw_parts     = [valid(cim_regs[i]) for i in range(4, 8)] if cim_regs else []
    cim_fw           = ".".join(str(x) for x in cim_fw_parts if x is not None) or None

    analog_inputs = []
    for i in range(7):
        u_val   = valid(ain_units[i] if ain_units else None)
        v_val   = valid(ain_vals[i]  if ain_vals  else None)
        is_temp = u_val in TEMP_UNIT_CODES
        if is_temp and v_val is not None:
            temp_c = round(v_val * 0.01 - 273.15, 1) if u_val in (13, 84) \
                     else round(v_val * 0.01, 1)
        else:
            temp_c = None
        analog_inputs.append({"index": i + 1, "unit": u_val, "raw": v_val, "temp_c": temp_c})

    return {
        "cim_unit_family":  cim_unit_family,
        "cim_unit_type":    cim_unit_type,
        "cim_unit_version": cim_unit_version,
        "cim_fw":           cim_fw,
        "analog_inputs":    analog_inputs,
        "mge_temps":        mge_temps,
    }


# ── Pressure spike detection (threshold events) ────────────────────────────────

def detect_pressure_spikes(spike_state, state, ts_iso, con):
    """Compare outlet and suction against thresholds every poll.
    Writes to pump_events_hires only on crossing (not every second while above).
    spike_state persists between calls: {'pressure_high': bool, 'pressure_low': bool, 'suction_low': bool}.
    Returns updated spike_state."""
    discharge  = state.get("outlet") if state.get("outlet") is not None else state.get("pressure")
    suction    = state.get("inlet")
    system_on  = state.get("system_on", False)

    # High outlet pressure (always watched, regardless of system_on)
    in_high = spike_state.get("pressure_high", False)
    if discharge is not None:
        if not in_high and discharge >= PRESSURE_HIGH_BAR:
            queue_event(con, ts_iso, "pressure_high", "System",
                        details={"bar": discharge, "threshold": PRESSURE_HIGH_BAR})
            spike_state["pressure_high"] = True
        elif in_high and discharge < (PRESSURE_HIGH_BAR - 0.3):   # 0.3 bar hysteresis
            queue_event(con, ts_iso, "pressure_high_clear", "System",
                        details={"bar": discharge, "threshold": PRESSURE_HIGH_BAR})
            spike_state["pressure_high"] = False

    # Low outlet pressure (only when system running - irrelevant at standby)
    in_low = spike_state.get("pressure_low", False)
    if discharge is not None and system_on:
        if not in_low and discharge <= PRESSURE_LOW_BAR:
            queue_event(con, ts_iso, "pressure_low", "System",
                        details={"bar": discharge, "threshold": PRESSURE_LOW_BAR})
            spike_state["pressure_low"] = True
        elif in_low and discharge > (PRESSURE_LOW_BAR + 0.5):     # 0.5 bar hysteresis
            queue_event(con, ts_iso, "pressure_low_clear", "System",
                        details={"bar": discharge, "threshold": PRESSURE_LOW_BAR})
            spike_state["pressure_low"] = False
    elif not system_on:
        spike_state["pressure_low"] = False   # clear on system stop

    # Low suction (suction near the B2 sensor floor, supply issue)
    in_suc = spike_state.get("suction_low", False)
    if suction is not None:
        if not in_suc and suction <= SUCTION_LOW_BAR:
            queue_event(con, ts_iso, "suction_low", "System",
                        details={"bar": suction, "threshold": SUCTION_LOW_BAR})
            spike_state["suction_low"] = True
        elif in_suc and suction > (SUCTION_LOW_BAR + 0.05):       # tight hysteresis
            queue_event(con, ts_iso, "suction_low_clear", "System",
                        details={"bar": suction, "threshold": SUCTION_LOW_BAR})
            spike_state["suction_low"] = False

    return spike_state


# ── Transition detection ───────────────────────────────────────────────────────

PUMP_BIT_LABELS = {0: "P1", 1: "P2", 2: "P3", 3: "P4", 4: "P5", 5: "P6", 6: "Pilot", 7: "Backup"}

SYS_FUNC_EVENTS = {
    1:  ("Emergency Run",   "Emergency run mode activated"),
    9:  ("Pressure Relief", "Pressure relief active - overpressure protection"),
    11: ("Low-Flow Boost",  "Low-flow boost active"),
    12: ("Low-Flow Stop",   "Low-flow stop active - possible system leak or zero demand"),
}


def detect_transitions(prev, curr, ts_iso, con):
    if prev is None:
        return

    # System alarm transitions
    prev_alarm = prev.get("alarm_code") or 0
    curr_alarm = curr.get("alarm_code") or 0
    if curr_alarm != prev_alarm:
        if curr_alarm:
            label = "System"
            for p in curr.get("pumps", []):
                if (p.get("alarm_code") or 0) == curr_alarm:
                    label = p["label"]
                    break
            queue_event(con, ts_iso, "alarm_on", label, curr_alarm,
                        ALARM_DESCRIPTIONS.get(curr_alarm, f"Code {curr_alarm}"))
        else:
            queue_event(con, ts_iso, "alarm_off", "System", prev_alarm,
                        ALARM_DESCRIPTIONS.get(prev_alarm, f"Code {prev_alarm}"))

    # Warning code transitions
    prev_warn = prev.get("warning_code") or 0
    curr_warn  = curr.get("warning_code") or 0
    if curr_warn != prev_warn:
        if curr_warn:
            queue_event(con, ts_iso, "warning_on", "System", curr_warn,
                        ALARM_DESCRIPTIONS.get(curr_warn, f"Warning code {curr_warn}"))
        else:
            queue_event(con, ts_iso, "warning_off", "System", prev_warn,
                        ALARM_DESCRIPTIONS.get(prev_warn, f"Warning code {prev_warn}"))

    # Per-pump GENIbus comm fault transitions
    prev_cf    = prev.get("pumps_comm_fault") or 0
    curr_cf    = curr.get("pumps_comm_fault") or 0
    changed_cf = prev_cf ^ curr_cf
    if changed_cf:
        for bit, label in PUMP_BIT_LABELS.items():
            if changed_cf & (1 << bit):
                faulted = bool(curr_cf & (1 << bit))
                queue_event(con, ts_iso,
                            "comm_fault" if faulted else "comm_restored",
                            label, 10,
                            f"{label} lost GENIbus comms" if faulted
                            else f"{label} GENIbus comms restored")

    # SystemActiveFunctions bit transitions (emergency run, low-flow stop, etc.)
    prev_saf    = prev.get("sys_active_funcs") or 0
    curr_saf    = curr.get("sys_active_funcs") or 0
    changed_saf = prev_saf ^ curr_saf
    if changed_saf:
        for bit, (name, desc) in SYS_FUNC_EVENTS.items():
            if changed_saf & (1 << bit):
                activated = bool(curr_saf & (1 << bit))
                queue_event(con, ts_iso,
                            f"func_{name.lower().replace(' ', '_')}_{'on' if activated else 'off'}",
                            "System", bit,
                            desc if activated else f"{name} no longer active")

    # Power cycle detection (NumberOfPowerOns change)
    prev_po = prev.get("power_ons")
    curr_po  = curr.get("power_ons")
    if prev_po is not None and curr_po is not None and curr_po != prev_po:
        queue_event(con, ts_iso, "power_cycle", "System",
                    details={"power_ons_before": prev_po, "power_ons_after": curr_po})

    # Suction pressure under-range (below -1.0 bar = transmitter floor)
    prev_inlet = prev.get("inlet")
    curr_inlet  = curr.get("inlet")
    if curr.get("system_on"):
        if prev_inlet is not None and curr_inlet is None:
            queue_event(con, ts_iso, "suction_underrange", "System",
                        details={"desc": "Suction below -1.0 bar - supply failure or sensor fault"})
        elif prev_inlet is None and curr_inlet is not None:
            queue_event(con, ts_iso, "suction_restored", "System",
                        details={"inlet_bar": curr_inlet})

    # Per-pump start/stop and alarm transitions
    prev_pumps = {p["id"]: p for p in prev.get("pumps", [])}
    for p in curr.get("pumps", []):
        pp = prev_pumps.get(p["id"], {})
        was_running = pp.get("running", False)
        is_running  = p.get("running", False)
        if is_running and not was_running:
            queue_event(con, ts_iso, "pump_start", p["label"],
                        details={"run_hours": p.get("run_hours"), "kwh": p.get("kwh")})
        elif not is_running and was_running:
            queue_event(con, ts_iso, "pump_stop", p["label"],
                        details={"run_hours": p.get("run_hours"), "kwh": p.get("kwh")})

        prev_ac = pp.get("alarm_code") or 0
        curr_ac = p.get("alarm_code") or 0
        if curr_ac != prev_ac:
            if curr_ac:
                queue_event(con, ts_iso, "pump_alarm_on", p["label"], curr_ac,
                            ALARM_DESCRIPTIONS.get(curr_ac, f"Code {curr_ac}"))
            else:
                queue_event(con, ts_iso, "pump_alarm_off", p["label"], prev_ac,
                            ALARM_DESCRIPTIONS.get(prev_ac, f"Code {prev_ac}"))


# ── 1-minute aggregation ───────────────────────────────────────────────────────

class MinuteBuffer:
    def __init__(self):
        self.reset()

    def reset(self):
        self.minute_ts          = None
        self.pressures          = []
        self.outlets            = []
        self.inlets             = []
        self.flows              = []
        self.powers             = []
        self.spec_energies      = []
        self.rel_perfs          = []
        self.run_secs           = [0, 0, 0, 0]
        self.starts             = [0, 0, 0, 0]
        # Carry the running state across the minute boundary - resetting it to
        # False made a continuously-running pump look like a fresh start at the
        # top of every minute, inflating the per-minute start count.
        self.last_run           = getattr(self, 'last_run', None) or [False, False, False, False]
        self.last_hours         = [None, None, None, None]
        self.last_kwh           = [None, None, None, None]
        self.pump_powers        = [[], [], [], []]
        self.pump_speeds        = [[], [], [], []]
        self.pump_currents      = [[], [], [], []]
        self.last_sys_energy        = None
        self.last_volume            = None
        self.last_setpoint          = None
        self.last_system_on         = None
        self.last_alarm_code        = None
        self.last_mode              = None
        self.last_sys_run_hours     = None
        self.last_sys_powered_hours = None
        self.last_spec_e_avg        = None
        self.last_tank_ok           = None
        self.last_pid_kp            = None
        self.last_pid_ti            = None
        self.last_sensor_max_bar    = None
        self.last_power_ons         = None
        self.samples            = 0

    def add(self, state, ts_iso):
        ts = datetime.fromisoformat(ts_iso)
        minute_bucket = ts.replace(second=0, microsecond=0)
        if self.minute_ts is None:
            self.minute_ts = minute_bucket
        elif minute_bucket != self.minute_ts:
            return True  # new minute - caller flushes first

        if state.get("pressure")      is not None: self.pressures.append(state["pressure"])
        if state.get("outlet")        is not None: self.outlets.append(state["outlet"])
        if state.get("inlet")         is not None: self.inlets.append(state["inlet"])
        if state.get("flow")          is not None: self.flows.append(state["flow"])
        if state.get("power_kw")      is not None: self.powers.append(state["power_kw"])
        if state.get("spec_energy")   is not None: self.spec_energies.append(state["spec_energy"])
        if state.get("rel_perf_pct")  is not None: self.rel_perfs.append(state["rel_perf_pct"])
        if state.get("sys_energy_kwh")    is not None: self.last_sys_energy        = state["sys_energy_kwh"]
        if state.get("volume_m3")         is not None: self.last_volume             = state["volume_m3"]
        if state.get("setpoint")          is not None: self.last_setpoint           = state["setpoint"]
        if state.get("sys_run_hours")     is not None: self.last_sys_run_hours      = state["sys_run_hours"]
        if state.get("sys_powered_hours") is not None: self.last_sys_powered_hours  = state["sys_powered_hours"]
        if state.get("spec_energy_avg")   is not None: self.last_spec_e_avg         = state["spec_energy_avg"]
        if state.get("tank_ok")           is not None: self.last_tank_ok            = state["tank_ok"]
        if state.get("pid_kp")            is not None: self.last_pid_kp             = state["pid_kp"]
        if state.get("pid_ti")            is not None: self.last_pid_ti             = state["pid_ti"]
        if state.get("sensor_max_bar")    is not None: self.last_sensor_max_bar     = state["sensor_max_bar"]
        if state.get("power_ons")         is not None: self.last_power_ons          = state["power_ons"]

        self.last_system_on  = state.get("system_on")
        self.last_alarm_code = state.get("alarm_code")
        if state.get("mode") is not None:
            self.last_mode = state["mode"]

        for i, p in enumerate(state.get("pumps", [])[:4]):
            running = p.get("running", False)
            if running:
                self.run_secs[i] += 1
            if running and not self.last_run[i]:
                self.starts[i] += 1
            self.last_run[i] = running
            if p.get("run_hours") is not None: self.last_hours[i] = p["run_hours"]
            if p.get("kwh")       is not None: self.last_kwh[i]   = p["kwh"]
            if p.get("power_kw")  is not None: self.pump_powers[i].append(p["power_kw"])
            if p.get("speed_pct") is not None: self.pump_speeds[i].append(p["speed_pct"])
            if p.get("current_a") is not None: self.pump_currents[i].append(p["current_a"])

        self.samples += 1
        return False

    def to_stat(self, mge_temps=None, slow_state=None):
        def avg(lst): return round(sum(lst) / len(lst), 3) if lst else None
        labels = ["p1", "p2", "p3", "jockey"]
        mge    = mge_temps or []
        stat = {
            "ts":                 self.minute_ts.isoformat(),
            "pressure_avg":       avg(self.pressures),
            "pressure_min":       round(min(self.pressures), 3) if self.pressures else None,
            "pressure_max":       round(max(self.pressures), 3) if self.pressures else None,
            "pressure_inlet":     avg(self.inlets),
            "pressure_outlet":    avg(self.outlets),
            "flow_avg":           avg(self.flows),
            "power_avg_kw":       avg(self.powers),
            "spec_energy_avg":    avg(self.spec_energies),
            "spec_energy_avg_lt": self.last_spec_e_avg,
            "sys_energy_kwh":     self.last_sys_energy,
            "volume_m3":          self.last_volume,
            "setpoint_bar":       self.last_setpoint,
            "system_on":          self.last_system_on,
            "alarm_code":         self.last_alarm_code,
            "mode":               self.last_mode,
            "rel_perf_pct":       avg(self.rel_perfs),
            "sys_run_hours":      self.last_sys_run_hours,
            "sys_powered_hours":  self.last_sys_powered_hours,
            "tank_ok":            self.last_tank_ok,
            "pid_kp":             self.last_pid_kp,
            "pid_ti":             self.last_pid_ti,
            "sensor_max_bar":     self.last_sensor_max_bar,
            "power_ons":          self.last_power_ons,
            "cim_unit_family":    slow_state.get("cim_unit_family")  if slow_state else None,
            "cim_unit_type":      slow_state.get("cim_unit_type")    if slow_state else None,
            "cim_unit_version":   slow_state.get("cim_unit_version") if slow_state else None,
            "cim_fw":             slow_state.get("cim_fw")           if slow_state else None,
            "p1_temp_c":          mge[0] if len(mge) > 0 else None,
            "p2_temp_c":          mge[1] if len(mge) > 1 else None,
            "p3_temp_c":          mge[2] if len(mge) > 2 else None,
            "jockey_temp_c":      mge[3] if len(mge) > 3 else None,
            "samples":            self.samples,
        }
        for i, lbl in enumerate(labels):
            stat[f"{lbl}_run_secs"]  = self.run_secs[i]
            stat[f"{lbl}_starts"]    = self.starts[i]
            stat[f"{lbl}_run_hours"] = self.last_hours[i]
            stat[f"{lbl}_kwh"]       = self.last_kwh[i]
            stat[f"{lbl}_power_kw"]  = avg(self.pump_powers[i])
            stat[f"{lbl}_speed_pct"] = avg(self.pump_speeds[i])
            stat[f"{lbl}_current_a"] = avg(self.pump_currents[i])
        return stat


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    print(f"Starting Grundfos poller v{POLLER_VERSION} -> {GRUNDFOS_HOST}:{GRUNDFOS_PORT}")
    print(f"Local DB: {DB_PATH}")
    print(f"Supabase: {SUPABASE_URL}")

    con        = init_db(DB_PATH)
    session    = requests.Session() if HAVE_REQUESTS else None
    buf        = MinuteBuffer()
    client     = None
    prev_state = None
    was_online = False
    poll_count         = 0
    slow_poll_counter  = 0
    config_snap_counter = 0
    slow_state  = {}  # cached slow-poll results (CIM, analog inputs, MGE temps)
    spike_state = {}  # pressure threshold crossing state

    while True:
        loop_start = time.monotonic()

        # ── Connect / reconnect ────────────────────────────────────────────────
        if client is None or not client.connected:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            client = ModbusTcpClient(GRUNDFOS_HOST, port=GRUNDFOS_PORT, timeout=5)
            if not client.connect():
                if was_online:
                    ts_iso = datetime.now(timezone.utc).isoformat()
                    print(f"OFFLINE at {ts_iso}")
                    queue_event(con, ts_iso, "offline", details={"host": GRUNDFOS_HOST})
                    was_online = False
                time.sleep(RECONNECT_WAIT)
                continue

        # ── Poll ───────────────────────────────────────────────────────────────
        ts_iso = datetime.now(timezone.utc).isoformat()
        state  = poll_once(client)

        if state is None:
            client.close()
            if was_online:
                print(f"READ FAIL at {ts_iso} - reconnecting")
                queue_event(con, ts_iso, "offline", details={"host": GRUNDFOS_HOST})
                was_online = False
            time.sleep(RECONNECT_WAIT)
            continue

        if not was_online:
            print(f"ONLINE at {ts_iso}")
            queue_event(con, ts_iso, "online", details={"host": GRUNDFOS_HOST, "version": POLLER_VERSION})
            was_online = True
            prev_state = None
            slow_poll_counter = SLOW_POLL_EVERY  # trigger slow poll immediately on first connect
            # Snapshot any alarms already active at startup
            sys_alarm = state.get("alarm_code") or 0
            if sys_alarm:
                label = "System"
                for p in state.get("pumps", []):
                    if (p.get("alarm_code") or 0) == sys_alarm:
                        label = p["label"]
                        break
                queue_event(con, ts_iso, "alarm_on", label, sys_alarm,
                            ALARM_DESCRIPTIONS.get(sys_alarm, f"Code {sys_alarm}"),
                            details={"initial_state": True})
                print(f"  Initial alarm: code {sys_alarm} on {label}")
            for p in state.get("pumps", []):
                pac = p.get("alarm_code") or 0
                if pac and pac != sys_alarm:
                    queue_event(con, ts_iso, "pump_alarm_on", p["label"], pac,
                                ALARM_DESCRIPTIONS.get(pac, f"Code {pac}"),
                                details={"initial_state": True})
                    print(f"  Initial pump alarm: code {pac} on {p['label']}")

        # ── Slow poll: CIM, analog inputs, MGE motor temps ─────────────────────
        slow_poll_counter += 1
        if slow_poll_counter >= SLOW_POLL_EVERY:
            slow_poll_counter = 0
            sd = slow_poll_once(client)
            if sd:
                slow_state.update(sd)
                mge_str = ", ".join(
                    f"P{i+1}={t}C" for i, t in enumerate(sd["mge_temps"]) if t is not None
                ) or "none"
                print(f"  Slow poll OK: cim_fw={sd.get('cim_fw')} mge=[{mge_str}]")

        # ── Detect transitions ─────────────────────────────────────────────────
        detect_transitions(prev_state, state, ts_iso, con)
        spike_state = detect_pressure_spikes(spike_state, state, ts_iso, con)
        prev_state = state

        # ── Store to SQLite ────────────────────────────────────────────────────
        save_reading(con, ts_iso, state)
        poll_count += 1

        # ── Aggregate into minute buffer ───────────────────────────────────────
        new_minute = buf.add(state, ts_iso)
        if new_minute and buf.samples > 0:
            stat = buf.to_stat(mge_temps=slow_state.get("mge_temps"), slow_state=slow_state)
            queue_minute_stat(con, stat)
            print(f"  Minute stat: p={stat['pressure_avg']} bar "
                  f"sp={stat['setpoint_bar']} bar mode={stat['mode']} "
                  f"flow={stat['flow_avg']} m3/h "
                  f"p1={stat['p1_run_secs']}s p2={stat['p2_run_secs']}s "
                  f"p3={stat['p3_run_secs']}s samples={stat['samples']}")
            buf.reset()
            buf.add(state, ts_iso)

        # ── Hourly config snapshot ─────────────────────────────────────────────
        config_snap_counter += 1
        if config_snap_counter >= CONFIG_SNAP_EVERY:
            config_snap_counter = 0
            queue_config_snapshot(con, ts_iso, state, slow_state)
            print(f"  Config snapshot: mode={state.get('mode')} "
                  f"kp={state.get('pid_kp')} ti={state.get('pid_ti')} "
                  f"cim_fw={slow_state.get('cim_fw')}")

        # ── Flush Supabase queue every 10 polls ────────────────────────────────
        if HAVE_REQUESTS and poll_count % 10 == 0:
            flush_supabase_queue(con, session)

        # ── Hourly maintenance ─────────────────────────────────────────────────
        if poll_count % 3600 == 0:
            prune_old_readings(con)
            qlen = con.execute("SELECT COUNT(*) FROM supabase_queue").fetchone()[0]
            rdgs = con.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
            print(f"  DB: {rdgs} readings, {qlen} queued for Supabase")

        # ── Status line every 60 seconds ───────────────────────────────────────
        if poll_count % 60 == 0:
            n_run = sum(1 for p in state.get("pumps", []) if p.get("running"))
            alm   = state.get("alarm_code") or 0
            pkw   = [p.get("power_kw") for p in state.get("pumps", [])]
            print(f"[{ts_iso[:19]}Z] p={state.get('pressure')} bar "
                  f"sp={state.get('setpoint')} bar mode={state.get('mode')} "
                  f"flow={state.get('flow')} m3/h kw={state.get('power_kw')} "
                  f"pump_kw={pkw[:3]} running={n_run} alarm={alm} "
                  f"vol={state.get('volume_m3')} m3 polls={poll_count}")

        # ── Pace to 1-second interval ──────────────────────────────────────────
        elapsed = time.monotonic() - loop_start
        sleep   = max(0.0, POLL_INTERVAL_S - elapsed)
        if sleep > 0:
            time.sleep(sleep)


if __name__ == "__main__":
    main()
