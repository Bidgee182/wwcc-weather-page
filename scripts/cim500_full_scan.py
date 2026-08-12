#!/usr/bin/env python3
"""
Full CIM500 Modbus TCP scan - find temperature registers.

Scans device_ids 1-10 across all useful register ranges.
Flags any value that looks like a temperature (0.01K scale or raw degC).
Saves JSON results to data/cim500_full_scan.json and prints a summary.

Run on the Pi with the poller running (non-destructive reads only):
    python3 scripts/cim500_full_scan.py

Or against a specific host:
    GRUNDFOS_HOST=192.168.1.2 python3 scripts/cim500_full_scan.py
"""

import json
import os
import sys
import time

try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    from pymodbus.client.sync import ModbusTcpClient

HOST = os.environ.get("GRUNDFOS_HOST", "bidgee-pumps.duckdns.org")
PORT = int(os.environ.get("GRUNDFOS_PORT", "502"))
NA   = 65535

# Register ranges to scan per device (start, count) -- reads in 100-reg chunks
RANGES = [
    (0,   200),   # CIM config, identity
    (200, 200),   # Status block + data block
    (400, 200),   # Pump blocks + energy
    (700, 100),   # Simulation block
    (7200, 200),  # Data log series
]

# Specific temperature registers by name (doc register = addr+1)
NAMED_REGS = {
    # CU352 GENIECON booster profile (device_id=1)
    319:  "RemoteTemp1 (0.01K, needs ext sensor)",
    336:  "AmbientTemp (0.01K, needs ext sensor)",
    337:  "InletTemp (0.01K, needs ext sensor)",
    338:  "OutletTemp (0.01K, needs ext sensor)",
    339:  "TempDifference (0.01K)",
    407:  "P1.MotorTemperature (0.01K)",
    417:  "P2.MotorTemperature (0.01K)",
    427:  "P3.MotorTemperature (0.01K)",
    437:  "P4.MotorTemperature (0.01K)",
    467:  "Jockey.MotorTemperature (0.01K)",
    # Single-pump CIM profile temperature registers
    211:  "MotorTemp? (was polled, 0.01K)",
    317:  "PowerElectronicTemp (0.01K)",
    318:  "MotorTemp (0.01K)",
    319:  "RemoteTemp1 (0.01K)",
    321:  "PumpLiquidTemp (0.01K) <-- TARGET",
    322:  "BearingTempDE (0.01K)",
    323:  "BearingTempNDE (0.01K)",
}


def looks_like_temp(raw):
    """Return (is_temp, temp_c, scale) if the value looks temperature-like."""
    if raw is None or raw == NA or raw == 0:
        return False, None, None
    # 0.01K scale: 0-120C = 27315-39315
    if 27000 <= raw <= 40000:
        c = round(raw * 0.01 - 273.15, 1)
        return True, c, "0.01K"
    # Raw degC (some alternate scales): 0-150
    if 0 < raw <= 1500:
        return True, raw, "raw?"
    return False, None, None


def scan_device(client, device_id):
    """Scan all register ranges for a single device_id. Returns dict of addr->raw."""
    results = {}
    for start, count in RANGES:
        addr = start
        while addr < start + count:
            chunk = min(100, start + count - addr)
            try:
                r = client.read_holding_registers(addr, count=chunk, slave=device_id)
                if not r.isError():
                    for i, val in enumerate(r.registers):
                        results[addr + i] = val
            except Exception:
                pass
            addr += chunk
            time.sleep(0.05)  # gentle pacing
    return results


def main():
    out_path = os.path.join(os.path.dirname(__file__), "../data/cim500_full_scan.json")
    print(f"Connecting to {HOST}:{PORT} ...")

    client = ModbusTcpClient(HOST, port=PORT, timeout=8)
    if not client.connect():
        print("ERROR: Could not connect to CIM500")
        sys.exit(1)
    print("Connected.\n")

    all_results = {}
    temp_hits   = []

    for device_id in range(1, 11):
        print(f"Scanning device_id={device_id} ...", end=" ", flush=True)
        regs = scan_device(client, device_id)

        # Check if device responded at all (has any non-NA values)
        live = {a: v for a, v in regs.items() if v != NA and v != 0}
        if not live:
            print("no response")
            continue

        print(f"{len(live)} live registers")
        all_results[device_id] = regs

        # Report named temp registers specifically
        for addr, name in NAMED_REGS.items():
            raw = regs.get(addr)
            if raw is None:
                continue
            is_t, c, scale = looks_like_temp(raw)
            flag = f"  *** TEMP {c}C ({scale}) ***" if is_t else ""
            print(f"  device={device_id} addr={addr:4d}  raw={raw:6d}  [{name}]{flag}")
            if is_t:
                temp_hits.append({"device_id": device_id, "addr": addr,
                                  "raw": raw, "temp_c": c, "scale": scale,
                                  "name": name})

        # Sweep everything else for temp-like values not in NAMED_REGS
        for addr, raw in sorted(live.items()):
            if addr in NAMED_REGS:
                continue
            is_t, c, scale = looks_like_temp(raw)
            if is_t:
                name = f"UNKNOWN addr {addr}"
                print(f"  device={device_id} addr={addr:4d}  raw={raw:6d}  *** TEMP {c}C ({scale}) *** [{name}]")
                temp_hits.append({"device_id": device_id, "addr": addr,
                                  "raw": raw, "temp_c": c, "scale": scale,
                                  "name": name})

    client.close()

    # Write full results
    output = {
        "host":      HOST,
        "port":      PORT,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "temp_hits": temp_hits,
        "registers": {str(did): regs for did, regs in all_results.items()},
    }
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nFull results -> {os.path.abspath(out_path)}")

    print("\n=== TEMPERATURE SUMMARY ===")
    if temp_hits:
        for h in temp_hits:
            print(f"  device_id={h['device_id']}  addr={h['addr']:4d}  {h['temp_c']}C  [{h['name']}]")
    else:
        print("  No temperature values found anywhere.")

    print("\nDone.")


if __name__ == "__main__":
    main()
