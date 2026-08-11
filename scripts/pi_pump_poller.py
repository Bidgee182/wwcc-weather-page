#!/usr/bin/env python3
"""
Grundfos 1-second Modbus poller for Raspberry Pi.

What it does:
- Polls Grundfos CU352 via Modbus TCP every 1 second
- Detects pump start/stop and alarm transitions instantly
- Stores every reading to local SQLite (30 days)
- Pushes state-change events to Supabase immediately
- Pushes 1-minute aggregate stats to Supabase each minute

Setup:
    pip3 install pymodbus requests
    # Edit GRUNDFOS_HOST below to the CIM500's local IP (faster than DuckDNS)
    python3 pi_pump_poller.py

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
# Use the CIM500's local IP for lowest latency (check USR modem DHCP table).
# DuckDNS also works if the router supports hairpin NAT.
GRUNDFOS_HOST = os.environ.get("GRUNDFOS_HOST", "bidgee-pumps.duckdns.org")
GRUNDFOS_PORT = int(os.environ.get("GRUNDFOS_PORT", "502"))

SUPABASE_URL = "https://sduzxijjvpbfgvlwcwpp.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNkdXp4aWpqdnBiZmd2bHdjd3BwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzY1ODE2NzgsImV4cCI6MjA5MjE1NzY3OH0.fbYf9-F987DUSlsibuGnqGYEQe6tsQsOf7NMmNMrBT8"

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pump_local.db")

POLL_INTERVAL_S = 1.0   # target seconds between polls
MINUTE_STATS_S  = 60    # push aggregate to Supabase every N seconds
RECONNECT_WAIT  = 5     # seconds to wait before reconnecting after disconnect
NA = 65535

# ── Static config ──────────────────────────────────────────────────────────────
PUMP_DEFS = [
    {"id": 1, "label": "P1 - CRNE45", "block": 400, "bit": 0},
    {"id": 2, "label": "P2 - CRNE45", "block": 410, "bit": 1},
    {"id": 3, "label": "P3 - CRNE45", "block": 420, "bit": 2},
    {"id": 4, "label": "Jockey",       "block": 460, "bit": 6},
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


# ── SQLite helpers ─────────────────────────────────────────────────────────────

def init_db(path):
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id        INTEGER PRIMARY KEY,
            ts        TEXT NOT NULL,
            system_on INTEGER,
            alarm     INTEGER,
            pressure  REAL,
            inlet     REAL,
            flow      REAL,
            power_kw  REAL,
            p1_run    INTEGER DEFAULT 0,
            p2_run    INTEGER DEFAULT 0,
            p3_run    INTEGER DEFAULT 0,
            jk_run    INTEGER DEFAULT 0,
            p1_alarm  INTEGER,
            p2_alarm  INTEGER,
            p3_alarm  INTEGER,
            jk_alarm  INTEGER,
            p1_hours  REAL,
            p2_hours  REAL,
            p3_hours  REAL,
            jk_hours  REAL
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
        (ts, system_on, alarm, pressure, inlet, flow, power_kw,
         p1_run, p2_run, p3_run, jk_run,
         p1_alarm, p2_alarm, p3_alarm, jk_alarm,
         p1_hours, p2_hours, p3_hours, jk_hours)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        ts_iso,
        1 if state.get("system_on") else 0,
        state.get("alarm_code"),
        state.get("pressure"),
        state.get("inlet"),
        state.get("flow"),
        state.get("power_kw"),
        1 if pv(0, "running") else 0,
        1 if pv(1, "running") else 0,
        1 if pv(2, "running") else 0,
        1 if pv(3, "running") else 0,
        pv(0, "alarm_code"), pv(1, "alarm_code"),
        pv(2, "alarm_code"), pv(3, "alarm_code"),
        pv(0, "run_hours"),  pv(1, "run_hours"),
        pv(2, "run_hours"),  pv(3, "run_hours"),
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


def rhr(client, addr, count):
    try:
        r = client.read_holding_registers(addr, count=count, device_id=1)
        if r.isError():
            return [None] * count
        return list(r.registers)
    except Exception:
        return [None] * count


def poll_once(client):
    """Read all essential registers. Returns state dict, or None on Modbus failure."""
    try:
        # Status block: addr 200-221 (22 registers covers sensor_max at addr 221)
        status = rhr(client, 200, 22)
        if status[0] is None:
            return None  # no data = connection issue

        # Data block: addr 300-315 (head, flow, setpoint, power, inlet)
        data = rhr(client, 300, 16)

        # Pump blocks: addr 400-479 (80 registers covers P1/P2/P3 + Jockey at 460)
        pumps_raw = rhr(client, 400, 80)

    except Exception as e:
        print(f"  Modbus read error: {e}")
        return None

    # Status block
    status_bits    = valid(status[0])   # addr 200
    alarm_code     = valid(status[4])   # addr 204
    warning_code   = valid(status[5])   # addr 205
    pumps_present  = valid(status[7])   # addr 207
    sensor_unit    = valid(status[19])  # addr 219: 0=bar, 1=mbar, 3=kPa
    sensor_max_raw = valid(status[21])  # addr 221

    system_on = bool(status_bits & (1 << 9)) if status_bits is not None else False

    if sensor_max_raw is None:
        sensor_max_mbar = 16000
    elif sensor_unit == 0:
        sensor_max_mbar = sensor_max_raw * 1000
    elif sensor_unit == 3:
        sensor_max_mbar = sensor_max_raw * 10
    else:
        sensor_max_mbar = sensor_max_raw

    # Data block
    head_raw    = valid(data[0])   # addr 300: Head (0.001 bar)
    flow_raw    = valid(data[1])   # addr 301: VolumeFlow (0.1 m3/h)
    setpt_raw   = valid(data[7])   # addr 307: ActualSetpoint (0.01%)
    power_hi    = valid(data[11])  # addr 311
    power_lo    = valid(data[12])  # addr 312
    inlet_raw   = valid(data[14])  # addr 314: InletPressure (0.001 bar from -1.0 bar min)

    pressure = round(head_raw * 0.001, 3) if head_raw is not None else None
    flow_m3h = round(flow_raw * 0.1, 2)  if flow_raw is not None else None
    inlet    = round(inlet_raw * 0.001 - 1.0, 3) if inlet_raw is not None else None

    power_combined = hi_lo_32(power_hi, power_lo)
    power_kw = round(power_combined / 1000, 2) if power_combined is not None else None

    setpt_pct = setpt_raw * 0.01 if setpt_raw is not None else None
    setpoint  = round(setpt_pct * 0.0001 * sensor_max_mbar / 1000, 2) \
                if setpt_pct is not None and sensor_max_mbar else None

    # Per-pump blocks
    pumps = []
    for defn in PUMP_DEFS:
        ofs = defn["block"] - 400   # jockey: 460-400=60
        bits_raw = pumps_raw[ofs]     if ofs < len(pumps_raw) else None
        alarm_c  = valid(pumps_raw[ofs + 1]) if ofs + 1 < len(pumps_raw) else None
        op_hi    = valid(pumps_raw[ofs + 2]) if ofs + 2 < len(pumps_raw) else None
        op_lo    = valid(pumps_raw[ofs + 3]) if ofs + 3 < len(pumps_raw) else None

        is_running = bool((bits_raw or 0) & 2)
        is_present = bool((pumps_present or 0) & (1 << defn["bit"]))
        op_hours   = hi_lo_32(op_hi, op_lo)

        pumps.append({
            "id":        defn["id"],
            "label":     defn["label"],
            "running":   is_running,
            "present":   is_present,
            "alarm_code": alarm_c,
            "run_hours": float(op_hours) if op_hours is not None else None,
        })

    return {
        "system_on":    system_on,
        "alarm_code":   alarm_code or 0,
        "warning_code": warning_code or 0,
        "pressure":     pressure,
        "setpoint":     setpoint,
        "inlet":        inlet,
        "flow":         flow_m3h,
        "power_kw":     power_kw,
        "pumps":        pumps,
    }


# ── Transition detection ───────────────────────────────────────────────────────

def detect_transitions(prev, curr, ts_iso, con):
    """Compare previous and current state; queue events for any changes."""
    if prev is None:
        return

    # System online/offline handled by caller
    # Alarm code changes
    prev_alarm = prev.get("alarm_code") or 0
    curr_alarm = curr.get("alarm_code") or 0
    if curr_alarm != prev_alarm:
        if curr_alarm:
            # Find which pump has this alarm
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

    # Per-pump start/stop transitions
    prev_pumps = {p["id"]: p for p in prev.get("pumps", [])}
    for p in curr.get("pumps", []):
        pp = prev_pumps.get(p["id"], {})
        was_running = pp.get("running", False)
        is_running  = p.get("running", False)
        if is_running and not was_running:
            queue_event(con, ts_iso, "pump_start", p["label"],
                        details={"run_hours": p.get("run_hours")})
        elif not is_running and was_running:
            queue_event(con, ts_iso, "pump_stop", p["label"],
                        details={"run_hours": p.get("run_hours")})

        # Per-pump alarm changes
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
        self.minute_ts  = None
        self.pressures  = []
        self.inlets     = []
        self.flows      = []
        self.powers     = []
        self.run_secs   = [0, 0, 0, 0]   # P1 P2 P3 Jockey
        self.starts     = [0, 0, 0, 0]
        self.last_run   = [False, False, False, False]
        self.last_hours = [None, None, None, None]
        self.samples    = 0

    def add(self, state, ts_iso):
        # Determine which minute bucket this belongs to
        ts = datetime.fromisoformat(ts_iso)
        minute_bucket = ts.replace(second=0, microsecond=0)

        if self.minute_ts is None:
            self.minute_ts = minute_bucket
        elif minute_bucket != self.minute_ts:
            return True   # signal: new minute, caller should flush first

        if state.get("pressure") is not None:
            self.pressures.append(state["pressure"])
        if state.get("inlet") is not None:
            self.inlets.append(state["inlet"])
        if state.get("flow") is not None:
            self.flows.append(state["flow"])
        if state.get("power_kw") is not None:
            self.powers.append(state["power_kw"])

        for i, p in enumerate(state.get("pumps", [])[:4]):
            running = p.get("running", False)
            if running:
                self.run_secs[i] += 1
            if running and not self.last_run[i]:
                self.starts[i] += 1
            self.last_run[i] = running
            if p.get("run_hours") is not None:
                self.last_hours[i] = p["run_hours"]

        self.samples += 1
        return False

    def to_stat(self):
        def avg(lst): return round(sum(lst) / len(lst), 3) if lst else None
        return {
            "ts":               self.minute_ts.isoformat(),
            "pressure_avg":     avg(self.pressures),
            "pressure_min":     round(min(self.pressures), 3) if self.pressures else None,
            "pressure_max":     round(max(self.pressures), 3) if self.pressures else None,
            "pressure_inlet":   avg(self.inlets),
            "flow_avg":         avg(self.flows),
            "power_avg_kw":     avg(self.powers),
            "samples":          self.samples,
            "p1_run_secs":      self.run_secs[0],
            "p2_run_secs":      self.run_secs[1],
            "p3_run_secs":      self.run_secs[2],
            "jockey_run_secs":  self.run_secs[3],
            "p1_starts":        self.starts[0],
            "p2_starts":        self.starts[1],
            "p3_starts":        self.starts[2],
            "jockey_starts":    self.starts[3],
            "p1_run_hours":     self.last_hours[0],
            "p2_run_hours":     self.last_hours[1],
            "p3_run_hours":     self.last_hours[2],
            "jockey_run_hours": self.last_hours[3],
        }


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    print(f"Starting Grundfos poller -> {GRUNDFOS_HOST}:{GRUNDFOS_PORT}")
    print(f"Local DB: {DB_PATH}")
    print(f"Supabase: {SUPABASE_URL}")

    con     = init_db(DB_PATH)
    session = requests.Session() if HAVE_REQUESTS else None
    buf     = MinuteBuffer()
    client  = None
    prev_state  = None
    was_online  = False
    poll_count  = 0
    prune_every = 3600  # prune SQLite once per hour

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
            connected = client.connect()
            if not connected:
                if was_online:
                    ts_iso = datetime.now(timezone.utc).isoformat()
                    print(f"OFFLINE at {ts_iso}")
                    queue_event(con, ts_iso, "offline",
                                details={"host": GRUNDFOS_HOST})
                    was_online = False
                time.sleep(RECONNECT_WAIT)
                continue

        # ── Poll ───────────────────────────────────────────────────────────────
        ts_iso = datetime.now(timezone.utc).isoformat()
        state  = poll_once(client)

        if state is None:
            # Modbus read failed - mark client as disconnected for reconnect
            client.close()
            if was_online:
                print(f"READ FAIL at {ts_iso} - reconnecting")
                queue_event(con, ts_iso, "offline", details={"host": GRUNDFOS_HOST})
                was_online = False
            time.sleep(RECONNECT_WAIT)
            continue

        # ── Back online ────────────────────────────────────────────────────────
        if not was_online:
            print(f"ONLINE at {ts_iso}")
            queue_event(con, ts_iso, "online", details={"host": GRUNDFOS_HOST})
            was_online = True
            prev_state = None   # don't diff across outage

        # ── Detect transitions ─────────────────────────────────────────────────
        detect_transitions(prev_state, state, ts_iso, con)
        prev_state = state

        # ── Store to SQLite ────────────────────────────────────────────────────
        save_reading(con, ts_iso, state)
        poll_count += 1

        # ── Aggregate into minute buffer ───────────────────────────────────────
        new_minute = buf.add(state, ts_iso)
        if new_minute and buf.samples > 0:
            stat = buf.to_stat()
            queue_minute_stat(con, stat)
            n_run = sum(1 for p in state.get("pumps", []) if p.get("running"))
            print(f"  Minute stat: p={stat['pressure_avg']} bar "
                  f"flow={stat['flow_avg']} m3/h "
                  f"run_secs={stat['p1_run_secs']}/{stat['p2_run_secs']}/{stat['p3_run_secs']} "
                  f"samples={stat['samples']}")
            buf.reset()
            buf.add(state, ts_iso)  # add current reading to new minute

        # ── Flush Supabase queue ───────────────────────────────────────────────
        if HAVE_REQUESTS and (poll_count % 10 == 0):
            flush_supabase_queue(con, session)

        # ── Prune old SQLite readings hourly ───────────────────────────────────
        if poll_count % prune_every == 0:
            prune_old_readings(con)
            qlen = con.execute("SELECT COUNT(*) FROM supabase_queue").fetchone()[0]
            rdgs = con.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
            print(f"  DB: {rdgs} readings stored, {qlen} items queued for Supabase")

        # ── Status line every 60 seconds ───────────────────────────────────────
        if poll_count % 60 == 0:
            n_run = sum(1 for p in state.get("pumps", []) if p.get("running"))
            alm   = state.get("alarm_code") or 0
            print(f"[{ts_iso[:19]}Z] p={state.get('pressure')} bar "
                  f"flow={state.get('flow')} m3/h "
                  f"pumps_running={n_run} "
                  f"alarm={alm} "
                  f"polls={poll_count}")

        # ── Pace to 1-second interval ──────────────────────────────────────────
        elapsed = time.monotonic() - loop_start
        sleep   = max(0.0, POLL_INTERVAL_S - elapsed)
        if sleep > 0:
            time.sleep(sleep)


if __name__ == "__main__":
    main()
