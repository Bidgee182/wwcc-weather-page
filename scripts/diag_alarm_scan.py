"""
One-shot diagnostic: probe every angle for alarm/event log data from CIM500.

Tries:
  1. Event log (addr 6000) on device IDs 1-5
  2. Broader address scan (addr 500-799, 1000-2000) on device_id 1
  3. All known current-alarm registers plus any undocumented neighbours

Results saved to data/diag_alarm_scan.json (overwrite each run).
"""
import json
import os
from datetime import datetime, timezone

from pymodbus.client import ModbusTcpClient

HOST = os.environ.get("GRUNDFOS_HOST", "bidgee-pumps.duckdns.org")
PORT = int(os.environ.get("GRUNDFOS_PORT", "502"))
NA   = 65535

DATA_DIR   = os.path.join(os.path.dirname(__file__), "..", "data")
OUT_FILE   = os.path.join(DATA_DIR, "diag_alarm_scan.json")

EVENT_LOG_ADDR   = 6000
EVENT_LOG_COUNT  = 1       # first read: how many events
REGS_PER_EVENT   = 7       # EventID, Code, Source, DeviceNo, TypeAndCondition, TimestampHI, TimestampLO
MAX_EVENTS       = 40


def rhr_safe(client, addr, count, device_id=1):
    """Read holding registers, returning (values_or_None, error_str_or_None)."""
    try:
        r = client.read_holding_registers(addr, count=count, device_id=device_id)
        if r.isError():
            return None, str(r)
        return r.registers, None
    except Exception as ex:
        return None, str(ex)


def probe_event_log(client, device_id):
    """Try to read the event log on a given device_id. Returns a dict summary."""
    result = {"device_id": device_id, "count_reg": None, "error": None, "events": []}

    # Step 1: read the count register
    vals, err = rhr_safe(client, EVENT_LOG_ADDR, 1, device_id)
    if err:
        result["error"] = f"addr {EVENT_LOG_ADDR}: {err}"
        return result

    count_raw = vals[0]
    result["count_reg"] = count_raw

    if count_raw == NA or count_raw == 0:
        result["error"] = f"count={count_raw} (no events or NA)"
        return result

    n = min(count_raw, MAX_EVENTS)
    # Step 2: read n events * 7 regs
    ev_vals, ev_err = rhr_safe(client, EVENT_LOG_ADDR + 1, n * REGS_PER_EVENT, device_id)
    if ev_err:
        result["error"] = f"event data read failed: {ev_err}"
        return result

    for i in range(n):
        base = i * REGS_PER_EVENT
        chunk = ev_vals[base:base + REGS_PER_EVENT]
        if len(chunk) < REGS_PER_EVENT:
            break
        event_id, code, source, device_no, type_cond, ts_hi, ts_lo = chunk
        ts32 = ts_hi * 65536 + ts_lo if (ts_hi != NA and ts_lo != NA) else None
        ts_iso = datetime.fromtimestamp(ts32, tz=timezone.utc).isoformat() if ts32 and ts32 > 0 else None
        result["events"].append({
            "event_id":   event_id,
            "code":       code,
            "source":     source,
            "device_no":  device_no,
            "type_cond":  type_cond,
            "ts_unix":    ts32,
            "ts_iso":     ts_iso,
        })

    return result


def scan_range(client, start, count, device_id=1):
    """Read a range, return {addr: value} for every non-NA register."""
    found = {}
    remaining = count
    addr = start
    while remaining > 0:
        chunk = min(remaining, 125)
        vals, err = rhr_safe(client, addr, chunk, device_id)
        if vals:
            for i, v in enumerate(vals):
                if v is not None and v != NA:
                    found[addr + i] = v
        addr += chunk
        remaining -= chunk
    return found


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    client = ModbusTcpClient(HOST, port=PORT, timeout=15)
    if not client.connect():
        print(f"OFFLINE: cannot connect to {HOST}:{PORT}")
        return

    results = {"ts": datetime.now(timezone.utc).isoformat(), "findings": {}}

    try:
        # --- 1. Event log probe on device IDs 1-5 ---
        print("\n=== EVENT LOG PROBE (addr 6000 on device_id 1-5) ===")
        event_log_results = []
        for did in range(1, 6):
            r = probe_event_log(client, did)
            event_log_results.append(r)
            status = f"{r['count_reg']} events" if r["count_reg"] is not None else "FAILED"
            print(f"  device_id {did}: {status} | error={r.get('error')}")
            if r["events"]:
                for ev in r["events"][:5]:
                    print(f"    {ev}")
        results["findings"]["event_log"] = event_log_results

        # --- 2. Undocumented ranges scan on device_id 1 ---
        print("\n=== UNDOCUMENTED RANGE SCAN (device_id 1) ===")
        extra_scans = {
            "addr_500_600":   scan_range(client, 500, 100),
            "addr_600_700":   scan_range(client, 600, 100),   # DDD sensor block
            "addr_1000_1100": scan_range(client, 1000, 100),
            "addr_1500_1600": scan_range(client, 1500, 100),
            "addr_2000_2100": scan_range(client, 2000, 100),
            "addr_3000_3100": scan_range(client, 3000, 100),
            "addr_5000_5100": scan_range(client, 5000, 100),
        }
        for name, found in extra_scans.items():
            print(f"  {name}: {len(found)} non-NA regs: {dict(list(found.items())[:10])}")
        results["findings"]["extra_scans"] = {k: v for k, v in extra_scans.items()}

        # --- 3. Per-pump device_id scans (non-standard regs) ---
        print("\n=== PER-PUMP DEVICE_ID SCANS (device_id 2-5, addr 0-500) ===")
        per_pump = {}
        for did in range(2, 6):
            found = scan_range(client, 0, 500, did)
            per_pump[f"did_{did}"] = found
            print(f"  device_id {did}: {len(found)} non-NA regs at addr 0-499")
            if found:
                print(f"    addrs: {sorted(found.keys())[:20]}")
        results["findings"]["per_pump_scans"] = per_pump

        # --- 4. Current alarm state snapshot ---
        print("\n=== CURRENT ALARM STATE ===")
        alarm_state = {}
        for addr, name in [
            (204, "SystemAlarmCode"), (205, "SystemWarningCode"),
            (209, "PumpsAlarm"), (210, "PumpsCommFault"),
        ]:
            v, _ = rhr_safe(client, addr, 1)
            alarm_state[name] = v[0] if v else None
            print(f"  addr {addr} {name}: {v[0] if v else 'FAIL'}")
        # Per-pump alarm codes
        for p_idx, (label, block) in enumerate(
            [("P1",400),("P2",410),("P3",420),("Jockey",460)]
        ):
            v, _ = rhr_safe(client, block + 1, 1)  # offset+1 = AlarmCode
            alarm_state[f"{label}_alarm"] = v[0] if v else None
            print(f"  {label} alarm code (addr {block+1}): {v[0] if v else 'FAIL'}")
        results["findings"]["alarm_state"] = alarm_state

    finally:
        client.close()

    with open(OUT_FILE, "w") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nResults saved to {OUT_FILE}")


if __name__ == "__main__":
    main()
