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
    +2: OperationTimeHI, +3: OperationTimeLO (1 h, 32-bit) - confirmed live: P1=5148h, System=40552h
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
STARTS_FILE        = os.path.join(DATA_DIR, "pump_starts.json")
RAW_ARCHIVE_DIR    = os.path.join(DATA_DIR, "raw")

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


def signed16(raw):
    """Interpret a Modbus holding register as signed 16-bit (two's complement).

    Pressure registers that can go negative (e.g. InletPressure under vacuum)
    are stored as signed values by the CU352. pymodbus returns them as unsigned,
    so values > 32767 must be adjusted: 65196 raw = -340 = -0.340 bar.
    """
    if raw is None or raw == NA:
        return None
    return raw - 65536 if raw > 32767 else raw


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


# Ranges to sweep for the raw register archive.
# addr 6000+ causes IO exception on GENIECON booster profile - excluded.
RAW_SCAN_RANGES = [
    (0,   156),   # CIM config, diagnostics, control registers
    (200, 288),   # status + data block + pump blocks + per-pump energy
    (700, 100),   # alarm simulation block + user registers (750-799)
]

MAX_ALARM_HISTORY = 200
COMMANDS_FILE     = os.path.join(DATA_DIR, "pump_commands.json")

# Pump control Modbus addresses (FC06 write): P1=104, P2=105, P3=106, Jockey=116
# Write 0=Auto, 1=Force start, 2=Force stop
PUMP_CTRL_ADDRS = [104, 105, 106, 116]


# Pump index → label for comm fault bit decoding
PUMP_BIT_LABELS = {0: "P1", 1: "P2", 2: "P3", 3: "P4", 4: "P5", 5: "P6", 6: "Pilot", 7: "Backup"}

# SystemActiveFunctions bits worth surfacing as events: {bit: (name, type_code, description)}
SYS_FUNC_EVENTS = {
    1:  ("Emergency Run",     1, "Emergency run mode activated"),
    9:  ("Pressure Relief",   3, "Pressure relief active - overpressure protection"),
    11: ("Low-Flow Boost",    3, "Low-flow boost active"),
    12: ("Low-Flow Stop",     3, "Low-flow stop active - possible system leak or zero demand"),
}


def update_alarm_history(pump_data, prev_pumps, alarm_code, warning_code, prev_sys,
                         now_iso, pumps_comm_fault=None, sys_active_funcs=None,
                         inlet_bar=None, power_ons=None, system_on=False):
    """Detect state transitions vs previous poll and append to persistent alarm history.

    Tracks: pump alarm codes, system alarm/warning codes, per-pump GENIbus comm faults,
    key SystemActiveFunctions bits (emergency run, low-flow stop, pressure relief),
    suction under-range (below -1.0 bar transmitter floor), and power cycles.

    The CU352 event log at addr 6000 is inaccessible on GENIECON - confirmed by full
    diagnostic scan of all device IDs. Local transition tracking is the only viable approach.
    """
    history = load_json(ALARM_HISTORY_FILE, {"events": []})
    events  = history.get("events", [])
    new_events = []
    ps = prev_sys or {}

    def evt(tc, tn, code, desc, label, source):
        return {"timestamp_iso": now_iso, "type_code": tc, "type_name": tn,
                "code": code, "description": desc, "pump_label": label, "source_name": source}

    # --- Per-pump alarm code transitions ---
    for i, pump in enumerate(pump_data):
        curr_code = pump.get("alarm_code") or 0
        prev_code = (prev_pumps[i].get("alarm_code") or 0) if i < len(prev_pumps) and prev_pumps[i] else 0
        if curr_code == prev_code:
            continue
        # A direct code-to-code change (e.g. 190 -> 4 on 7 Aug 2026) must CLOSE
        # the old code, or it stays 'active' forever in the history and the
        # alert emails nag about it daily.
        if prev_code and curr_code and prev_code != curr_code:
            new_events.append(evt(2, "Alarm cleared", prev_code,
                                  ALARM_DESCRIPTIONS.get(prev_code, f"Alarm code {prev_code}"),
                                  pump["label"], "Pump"))
            tc, tn, code_to_log = 1, "Alarm appeared", curr_code
        elif curr_code and not prev_code:
            tc, tn, code_to_log = 1, "Alarm appeared", curr_code
        else:
            tc, tn, code_to_log = 2, "Alarm cleared", prev_code
        new_events.append(evt(tc, tn, code_to_log,
                              ALARM_DESCRIPTIONS.get(code_to_log, f"Alarm code {code_to_log}"),
                              pump["label"], "Pump"))

    # --- System alarm transition ---
    prev_alarm = ps.get("alarm_code") or 0
    curr_alarm = alarm_code or 0
    if curr_alarm != prev_alarm:
        if prev_alarm and curr_alarm:
            # superseded code (e.g. 4 <-> 210 flapping) is closed explicitly
            new_events.append(evt(2, "Alarm cleared", prev_alarm,
                                  ALARM_DESCRIPTIONS.get(prev_alarm, f"Alarm code {prev_alarm}"),
                                  "System", "System"))
        if curr_alarm:
            # Cross-reference per-pump alarm registers to identify specific pump
            sys_pump_label = "System"
            for pump in pump_data:
                if (pump.get("alarm_code") or 0) == curr_alarm:
                    sys_pump_label = pump["label"]
                    break
            new_events.append(evt(1, "Alarm appeared", curr_alarm,
                                  ALARM_DESCRIPTIONS.get(curr_alarm, f"Alarm code {curr_alarm}"),
                                  sys_pump_label, "System"))
        elif prev_alarm:
            new_events.append(evt(2, "Alarm cleared", prev_alarm,
                                  ALARM_DESCRIPTIONS.get(prev_alarm, f"Alarm code {prev_alarm}"),
                                  "System", "System"))

    # --- System warning transition ---
    prev_warn = ps.get("warning_code") or 0
    curr_warn  = warning_code or 0
    if curr_warn != prev_warn:
        if curr_warn:
            new_events.append(evt(3, "Warning appeared", curr_warn,
                                  ALARM_DESCRIPTIONS.get(curr_warn, f"Warning code {curr_warn}"),
                                  "System", "System"))
        elif prev_warn:
            new_events.append(evt(4, "Warning cleared", prev_warn,
                                  ALARM_DESCRIPTIONS.get(prev_warn, f"Warning code {prev_warn}"),
                                  "System", "System"))

    # --- Per-pump GENIbus comm fault transitions (addr 210) ---
    prev_cf = ps.get("pumps_comm_fault") or 0
    curr_cf  = pumps_comm_fault or 0
    changed_cf = prev_cf ^ curr_cf
    if changed_cf:
        for bit, label in PUMP_BIT_LABELS.items():
            if changed_cf & (1 << bit):
                faulted = bool(curr_cf & (1 << bit))
                new_events.append(evt(
                    1 if faulted else 2,
                    "GENIbus comm fault" if faulted else "GENIbus comm restored",
                    10,  # alarm code 10 = GENIbus comms fault
                    f"{label} lost GENIbus communication with CU352" if faulted
                    else f"{label} GENIbus communication restored",
                    label, "GENIbus"))

    # --- SystemActiveFunctions bit transitions (addr 211) ---
    prev_saf = ps.get("sys_active_funcs") or 0
    curr_saf  = sys_active_funcs or 0
    changed_saf = prev_saf ^ curr_saf
    if changed_saf:
        for bit, (name, tc_on, desc_on) in SYS_FUNC_EVENTS.items():
            if changed_saf & (1 << bit):
                activated = bool(curr_saf & (1 << bit))
                new_events.append(evt(
                    tc_on if activated else 4,
                    f"{name} activated" if activated else f"{name} deactivated",
                    bit,
                    desc_on if activated else f"{name} no longer active",
                    "System", "SystemFunction"))

    # --- Suction pressure under-range transitions ---
    prev_suction = ps.get("pressure_suction_bar")
    curr_suction  = inlet_bar
    if system_on:
        if prev_suction is not None and curr_suction is None:
            new_events.append(evt(3, "Suction under-range", 57,
                                  "Suction pressure below -1.0 bar (transmitter floor) - extreme vacuum or supply failure",
                                  "System", "Suction"))
        elif prev_suction is None and curr_suction is not None:
            new_events.append(evt(4, "Suction pressure restored", 57,
                                  f"Suction pressure recovered to {curr_suction:.3f} bar",
                                  "System", "Suction"))

    # --- Power cycle detection (addr 344 NumberOfPowerOns) ---
    prev_po = ps.get("power_ons")
    curr_po  = power_ons
    if prev_po is not None and curr_po is not None and curr_po != prev_po:
        new_events.append(evt(3, "Power cycle", 0,
                              f"System power-on count changed {prev_po} -> {curr_po} "
                              f"(controller was restarted or powered off)",
                              "System", "System"))

    combined = (new_events + events)[:MAX_ALARM_HISTORY]
    write_json(ALARM_HISTORY_FILE, {"events": combined})
    return combined[:40]


def raw_scan(client):
    """Read all plausibly useful Modbus registers and return as a flat dict {str(addr): value}.

    Only non-NA (not 65535) values are included so the archive stays compact.
    Called inside the existing try/finally block so the client is still open.
    """
    result = {}
    for start, count in RAW_SCAN_RANGES:
        vals = rhr(client, start, count)
        for i, v in enumerate(vals):
            if v is not None and v != NA:
                result[str(start + i)] = v
    return result


def append_raw(ts_iso, registers):
    """Append one poll's raw register snapshot to the daily JSONL archive file."""
    os.makedirs(RAW_ARCHIVE_DIR, exist_ok=True)
    dt_aest = datetime.fromisoformat(ts_iso.replace("Z", "+00:00")) + timedelta(hours=10)
    fname = os.path.join(RAW_ARCHIVE_DIR, f"{dt_aest.strftime('%Y-%m-%d')}.jsonl")
    with open(fname, "a") as fh:
        fh.write(json.dumps({"ts": ts_iso, "r": registers}) + "\n")
    # Prune files older than 90 days
    cutoff = (dt_aest - timedelta(days=90)).date()
    try:
        for f in os.listdir(RAW_ARCHIVE_DIR):
            if f.endswith(".jsonl") and len(f) == 15:
                try:
                    if datetime.strptime(f[:10], "%Y-%m-%d").date() < cutoff:
                        os.remove(os.path.join(RAW_ARCHIVE_DIR, f))
                except ValueError:
                    pass
    except OSError:
        pass


def execute_pending_command(client):
    """Execute one pending command from pump_commands.json, update its status.

    Commands are written by the pump-station.html Control tab via GitHub API.
    Prerequisite: HMI -> Settings -> Communication -> Bus/Cloud control -> On.
    Without that setting, Modbus writes to addr 100+ are ignored by the CU352.
    """
    try:
        cmds = load_json(COMMANDS_FILE, {"pending": None, "log": []})
    except Exception:
        return

    pending = cmds.get("pending")
    if not pending or pending.get("status") != "pending":
        return

    act     = pending.get("action")
    val     = pending.get("value")
    now_iso = datetime.now(timezone.utc).isoformat()
    result  = None
    success = False

    try:
        def wr(addr, value):
            r = client.write_register(addr, value, device_id=1)
            return r

        def rhr1(addr):
            """Read one holding register; return int value or None on error."""
            try:
                r = client.read_holding_registers(addr, 1, device_id=1)
                return None if r.isError() else r.registers[0]
            except Exception:
                return None

        if act == "set_setpoint":
            bar     = float(val)
            reg_val = max(0, min(10000, int(round(bar * 625))))
            # Read current setpoint for log (addr 307 = ActualSetpoint, 0.0016 bar/unit)
            before_reg = rhr1(307)
            before_bar = round(before_reg * 0.0016, 2) if before_reg is not None else None
            # Write ONLY the setpoint register (addr 103). Do NOT write addr 100
            # (control word) - writing it would risk activating Remote mode and
            # loading wrong CU352 stored settings (this is what caused the mode change incident).
            r2 = wr(103, reg_val)
            if not r2.isError():
                after_reg = rhr1(307)
                after_bar = round(after_reg * 0.0016, 2) if after_reg is not None else None
                success = True
                result  = f"Setpoint set to {bar:.2f} bar (reg={reg_val}); was {before_bar} bar, now {after_bar} bar"
            else:
                result = f"Write failed: Setpoint addr 103={r2}"

        elif act == "system_stop":
            # system_stop/start legitimately control addr 100 (on/off bits only)
            r = wr(100, 0x0001)    # remote ON + stop
            success = not r.isError()
            result  = "Stop command sent" if success else f"Write failed: {r}"

        elif act == "system_start":
            r = wr(100, 0x0003)    # remote ON + start
            success = not r.isError()
            result  = "Start command sent" if success else f"Write failed: {r}"

        elif act == "set_mode":
            MODE_MAP = {
                "auto": 3, "constant head": 3, "constant pressure": 4,
                "constant flow": 5, "constant level": 8,
                "constant differential pressure": 1, "constant pressure setpoint": 2,
            }
            code = MODE_MAP.get(str(val).lower().strip())
            if code:
                before_code = rhr1(202)
                # Write ONLY the mode register (addr 101). Do NOT write addr 100.
                r2 = wr(101, code)
                after_code  = rhr1(202)
                success = not r2.isError()
                changed = after_code == code if after_code is not None else None
                result  = (f"Mode write addr 101={code}; was {before_code}, now {after_code}"
                           + (" (accepted)" if changed else " (WARNING: CU352 did not apply change)")) if success else "Write failed"
            else:
                result = f"Unknown mode '{val}'"

        elif act in ("start_pump", "stop_pump", "auto_pump"):
            pump_id  = int(val)   # 1-based (1=P1, 2=P2, 3=P3, 4=Jockey)
            pump_idx = pump_id - 1
            if 0 <= pump_idx < len(PUMP_CTRL_ADDRS):
                ctrl_addr = PUMP_CTRL_ADDRS[pump_idx]
                # 0 = Auto (release to controller), 1 = force Start, 2 = force Stop
                state_val = 1 if act == "start_pump" else 2 if act == "stop_pump" else 0
                # Write ONLY the pump's individual control register (addr 104-116).
                # Do NOT write addr 100 - that risks loading wrong CU352 remote settings.
                r2 = wr(ctrl_addr, state_val)
                success = not r2.isError()
                action_lbl = ("started" if act == "start_pump"
                              else "stopped" if act == "stop_pump" else "released to auto")
                result = (f"Pump {pump_id} {action_lbl} (addr {ctrl_addr}={state_val})"
                          if success else f"Write failed addr {ctrl_addr}")
            else:
                result = f"Invalid pump id {pump_id}"

        elif act == "alarm_reset":
            # alarm_reset MUST write addr 100 (alarm-reset bit 2 lives there).
            # Read mode AND setpoint first so we can restore them immediately after
            # - enabling Remote mode (bit 0) can cause CU352 to load wrong stored settings.
            mode_before = rhr1(202) or 3   # default constant head
            sp_before   = rhr1(307)        # ActualSetpoint raw register value
            # Write alarm reset (Remote+Start+AlarmReset)
            r = wr(100, 0x0007)
            if not r.isError():
                wr(100, 0x0003)           # clear alarm-reset bit, keep Remote+Start
                # Restore setpoint immediately (setpoint writes are reliable)
                if sp_before is not None:
                    wr(103, sp_before)
                # Attempt mode restore (CU352 may ignore addr 101 writes)
                wr(101, mode_before)
                sp_bar = round(sp_before * 0.0016, 2) if sp_before is not None else 'unknown'
                success = True
                result  = (f"Alarm reset sent; restored setpoint={sp_bar} bar "
                           f"and attempted mode={mode_before}")
            else:
                result = f"Write failed: {r}"

    except Exception as ex:
        result = f"Exception: {ex}"

    # Update log entry status
    for cmd in cmds.get("log", []):
        if cmd.get("id") == pending.get("id"):
            cmd["status"]      = "executed" if success else "failed"
            cmd["executed_at"] = now_iso
            cmd["result"]      = result
            break

    cmds["pending"] = None
    write_json(COMMANDS_FILE, cmds)
    print(f"CMD {act}={val}: {'OK' if success else 'FAIL'} - {result}")


# ── Offline diagnosis ─────────────────────────────────────────────────────────
# Our router is a USR IoT cellular gateway (LuCI/OpenWrt, hostname
# "Bidgee_Gateway"). If the DuckDNS name resolves to an IP where a DIFFERENT
# device answers on 80/443, Telstra has reassigned our IP and DuckDNS is
# stale - the single most common cause of "poller down".
ROUTER_FINGERPRINTS = ("luci", "usr", "bidgee", "openwrt")


def _http_title(url, timeout=6):
    """Fetch url and return its <title> text (lower-case) or None."""
    import re as _re
    import ssl as _ssl
    import urllib.request as _ur
    try:
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        req = _ur.Request(url, headers={"User-Agent": "wwcc-pump-poller/diag"})
        with _ur.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read(20000).decode("utf-8", "ignore")
        m = _re.search(r"<title>(.*?)</title>", body, _re.I | _re.S)
        return (m.group(1).strip().lower() if m else "") or "(untitled page)"
    except Exception:
        return None


def _tcp_open(ip, port, timeout=5):
    import socket as _sock
    try:
        with _sock.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def diagnose_offline(host, port):
    """Return {code, dns_ip, detail, summary} explaining a failed connection.

    code is one of:
      dns_failed       - hostname does not resolve at all
      dns_private      - resolves to a private/CGNAT address (DuckDNS wrong)
      ip_reassigned    - another device answers at that IP (DuckDNS stale)
      router_no_modbus - our router answers on 80/443 but :502 is closed
                         (port-forward lost or CIM500 unreachable on the LAN)
      unreachable      - nothing answers at that IP (site/modem down, or IP
                         released and not yet reissued)
    """
    import socket as _sock
    d = {"code": "unknown", "dns_ip": None, "detail": "", "summary": ""}
    try:
        resolved = list({r[4][0] for r in _sock.getaddrinfo(host, port, _sock.AF_INET)})
    except Exception as e:
        d.update(code="dns_failed", detail=str(e),
                 summary=f"{host} does not resolve: {e}")
        return d
    ip = resolved[0] if resolved else None
    d["dns_ip"] = ip
    if not ip:
        d.update(code="dns_failed", summary=f"{host} returned no A record")
        return d

    if any(ip.startswith(pfx) for pfx in ("10.", "192.168.", "172.", "100.")):
        d.update(code="dns_private",
                 summary=f"{host} -> {ip} is a PRIVATE/CGNAT address - DuckDNS is wrong")
        return d

    web_open = _tcp_open(ip, 80) or _tcp_open(ip, 443)
    if not web_open:
        d.update(code="unreachable",
                 summary=f"{host} -> {ip}: nothing answers on 80/443/502 - "
                         f"modem offline or IP released")
        return d

    title = ""
    for url in (f"https://{ip}/", f"http://{ip}/", f"https://{ip}/", f"http://{ip}/"):
        title = _http_title(url) or ""          # link can be lossy - retry once each
        if title:
            break
    d["detail"] = title
    if title and any(fp in title for fp in ROUTER_FINGERPRINTS):
        d.update(code="router_no_modbus",
                 summary=f"{host} -> {ip}: our router answers ('{title}') but :502 "
                         f"is closed - port-forward or CIM500 LAN link lost")
    else:
        d.update(code="ip_reassigned",
                 summary=f"{host} -> {ip} now answers as '{title or 'unknown device'}' "
                         f"- NOT our router. Telstra reassigned the IP and DuckDNS is stale")
    return d


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    # Load previous state before connecting - needed for alarm transition detection
    prev = load_json(LATEST_FILE, {"history": []})

    # The cellular link drops packets now and then; one failed TCP handshake
    # must not mark the station offline (23 Aug 2026: false OFFLINE at 21:38
    # while the on-site Pi stayed connected). Try up to 3 times, 4 s apart.
    import time as _time
    client = ModbusTcpClient(HOST, port=PORT, timeout=10)
    connected = False
    for attempt in range(3):
        connected = client.connect()
        if connected:
            break
        print(f"connect attempt {attempt + 1}/3 failed")
        _time.sleep(4)
    now_iso = datetime.now(timezone.utc).isoformat()

    if not connected:
        prev["connected"] = False
        prev["last_seen"]  = prev.get("timestamp")
        prev["timestamp"]  = now_iso
        # Diagnose WHY we are offline so the alert email and the self-heal
        # step can act on it (stale DuckDNS vs site genuinely down).
        diag = diagnose_offline(HOST, PORT)
        diag["checked_at"] = now_iso
        prev["offline_diagnosis"] = diag
        write_json(LATEST_FILE, prev)
        print(f"OFFLINE: {diag.get('summary')}")
        sys.exit(0)

    try:
        # Execute any pending command BEFORE reading state (so next poll sees new state)
        execute_pending_command(client)

        status_regs   = rhr(client, 200, 32)   # regs 00201-00232 (incl Kp addr230, Ti addr231)
        data_regs     = rhr(client, 300, 64)   # regs 00301-00364 (extended from 50 to 64)
        pump_regs     = rhr(client, 400, 80)   # regs 00401-00480 (8 pump blocks x 10)
        energy_regs   = rhr(client, 480, 8)    # per-pump energy kWh (00481-00488)
        ain_unit_regs = rhr(client, 224, 7)    # regs 00225-00231: AnalogIn1-7 unit codes
        ain_val_regs  = rhr(client, 375, 7)    # regs 00376-00382: AnalogIn1-7 values
        cim_regs      = rhr(client, 29, 8)     # regs 00030-00037: UnitFamily/Type/Ver + FW version
        mge_temps     = scan_mge_temperatures(client, len(PUMP_DEFS))
        raw_regs      = raw_scan(client)
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
    status_bits      = valid(status_regs[0])   # 00201
    process_fb       = valid(status_regs[1])   # 00202: ProcessFeedback
    control_mode     = valid(status_regs[2])   # 00203
    alarm_code       = valid(status_regs[4])   # 00205
    warning_code     = valid(status_regs[5])   # 00206
    pumps_present    = valid(status_regs[7])   # 00208
    pumps_running    = valid(status_regs[8])   # 00209
    pumps_comm_fault = valid(status_regs[10])  # 00211: per-pump GENIbus comm fault bits
    sys_active_funcs = valid(status_regs[11])  # 00212: active function bits (emergency run, low-flow stop, etc.)
    kp_raw           = valid(status_regs[30])  # 00231: PI controller Kp (scale 0.1)
    ti_raw           = valid(status_regs[31])  # 00232: PI controller Ti in seconds (scale 0.1)

    system_on  = bool(status_bits & (1 << 9))  if status_bits is not None else False
    alarm_act  = bool(status_bits & (1 << 10)) if status_bits is not None else False

    # -- Data block --------------------------------------------------------------
    head_raw     = valid(data_regs[0])   # 00301: Head (0.001 bar)
    flow_raw     = valid(data_regs[1])   # 00302: VolumeFlow (0.1 m3/h)
    rel_perf_raw = valid(data_regs[2])   # 00303: RelativePerformance (0.01%)
    di_raw       = valid(data_regs[5])   # 00306: DigitalInput bits
    do_raw       = valid(data_regs[6])   # 00307: DigitalOutput bits
    setpoint_raw = valid(data_regs[7])   # 00308: ActualSetpoint (0.01%, incl analog influence)
    usersetpt_raw = valid(data_regs[42]) # 00343: UserSetpoint (0.01%, before any modification)
    power_hi     = valid(data_regs[11])  # 00312: PowerHI
    power_lo     = valid(data_regs[12])  # 00313: PowerLO
    inlet_raw    = valid(data_regs[14])   # 00315: InletPressure (0.001 bar from transmitter min)
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
    # Prefer UserSetpoint (342) over ActualSetpoint (307). The register can't hold 7.80
    # exactly (quantises to ~7.8112), so snap to the nearest 0.05 bar to recover the clean
    # value the CU352 displays (7.8112 -> 7.80).
    _sp_raw      = pct_to_bar(usersetpt_raw if usersetpt_raw is not None else setpoint_raw, sensor_max_mbar)
    setpoint_bar = round(round(_sp_raw / 0.05) * 0.05, 2) if _sp_raw is not None else None
    flow_m3h     = round(flow_raw * 0.1, 2) if flow_raw is not None else None
    # B2 transmitter range -1.0 to 6.0 bar: CU352 stores offset from transmitter min.
    # raw=0 → -1.0 bar, raw=1000 → 0.0 bar, raw=680 → -0.32 bar (confirmed from CU352 photo).
    inlet_bar    = round(inlet_raw * 0.001 - 1.0, 3) if inlet_raw is not None else None
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
    pid_kp    = round(kp_raw * 0.1, 1) if kp_raw is not None else None
    pid_ti    = round(ti_raw * 0.1, 1) if ti_raw is not None else None
    sensor_max_bar = round(sensor_max_mbar / 1000, 1) if sensor_max_mbar else None

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
            "run_hours":    float(op_combined) if op_combined is not None else None,
            "starts_today": 0,   # placeholder; updated by track_pump_starts() after parse
            "temp_c":       temp_c,
            "alarm_code":   alarm_c,
        }

    pump_data = [parse_pump(pd, i) for i, pd in enumerate(PUMP_DEFS)]

    # -- Inferred pump starts (transition-based; no Modbus register exists) --------
    now_aest_date = (datetime.now(timezone.utc) + timedelta(hours=10)).strftime("%Y-%m-%d")
    today_starts = track_pump_starts(pump_data, now_aest_date)
    for i, p in enumerate(pump_data):
        p["starts_today"] = today_starts[i] if i < len(today_starts) else 0

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
        "st": [p["starts_today"] for p in pump_data],
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
        # DI2 (terminals 12,13) = Water Shortage / tank float valve
        # bit1=1: float closed (jumper or float OK) = tank OK
        # bit1=0: float open (tank low) = water shortage / tank empty
        "tank_ok":                   bool(di_raw & 0x02) if di_raw is not None else None,
        # CIM identity & firmware (addr 29-36 = doc 00030-00037)
        "cim_unit_family":  valid(cim_regs[0]) if cim_regs else None,  # 21 = Hydro MPC/GENIECON
        "cim_unit_type":    valid(cim_regs[1]) if cim_regs else None,  # 4 = GENIECON
        "cim_unit_version": valid(cim_regs[2]) if cim_regs else None,
        "cim_fw_ver1":      valid(cim_regs[4]) if cim_regs else None,  # firmware version regs
        "cim_fw_ver2":      valid(cim_regs[5]) if cim_regs else None,
        "cim_fw_ver3":      valid(cim_regs[6]) if cim_regs else None,
        "cim_fw_ver4":      valid(cim_regs[7]) if cim_regs else None,
        "alarm_code":                alarm_code or 0,
        "warning_code":              warning_code or 0,
        "pumps_comm_fault":          pumps_comm_fault or 0,
        "sys_active_funcs":          sys_active_funcs or 0,
        "pid_kp":                    pid_kp,
        "pid_ti":                    pid_ti,
        "sensor_max_bar":            sensor_max_bar,
    }

    # -- Alarm history (local transition tracking - CU352 addr 6000 not supported on GENIECON)
    event_log = update_alarm_history(
        pump_data,
        prev.get("pumps", []),
        alarm_code or 0,
        warning_code or 0,
        prev.get("system"),
        now_iso,
        pumps_comm_fault=pumps_comm_fault,
        sys_active_funcs=sys_active_funcs,
        inlet_bar=inlet_bar,
        power_ons=power_ons_raw,
        system_on=system_on,
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
        "last_connected":  now_iso,
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

    append_raw(now_iso, raw_regs)

    n_run = sum(1 for p in pump_data if p["running"])
    mge_str = ", ".join(f"P{i+1}={t}C" for i, t in enumerate(mge_temps) if t is not None) or "none"
    print(f"OK: discharge={actual_bar} bar, setpoint={setpoint_bar} bar, "
          f"flow={flow_m3h} m3/h, running={n_run}/{len(pump_data)}, "
          f"spec_energy={spec_energy} Wh/m3, mge_temps=[{mge_str}], "
          f"alarm_history={len(event_log)} events, raw_regs={len(raw_regs)}")


def _offline_pump(defn):
    return {**defn, "running": False, "fault": False, "standby": False,
            "speed_pct": None, "power_kw": None, "current_a": None,
            "run_hours": None, "starts_today": 0, "temp_c": None, "alarm_code": None}


def track_pump_starts(pump_data, now_aest_date):
    """Infer pump starts from running-state transitions (no Modbus register available).
    Counts reset daily at AEST midnight. Returns list of today's start counts per pump."""
    saved = load_json(STARTS_FILE, {})

    if saved.get("date") != now_aest_date:
        # New day - reset counts; keep last running state to avoid a phantom start at rollover
        counts   = [0] * len(pump_data)
        prev_run = saved.get("running", [False] * len(pump_data))
    else:
        counts   = saved.get("counts",  [0]     * len(pump_data))
        prev_run = saved.get("running", [False]  * len(pump_data))

    # Pad in case pump count changed
    while len(counts)   < len(pump_data): counts.append(0)
    while len(prev_run) < len(pump_data): prev_run.append(False)

    curr_run = []
    for i, p in enumerate(pump_data):
        is_running = bool(p.get("running", False))
        was_running = bool(prev_run[i])
        if is_running and not was_running:
            counts[i] += 1
        curr_run.append(is_running)

    write_json(STARTS_FILE, {"date": now_aest_date, "counts": counts, "running": curr_run})
    return counts


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

            sts.append(0)  # not in 90d history; daily starts tracked live in pump_starts.json

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
