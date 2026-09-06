#!/bin/bash
# Network watchdog for the pump-poller Pi.
#
# Runs every minute (net-watchdog.timer). If no connectivity for 10 straight
# minutes it bounces the WiFi interface; at 15 minutes it reboots the Pi.
# Counter lives in /run so it resets to zero on every boot.
#
# Built 7 Sep 2026 after the Pi silently stopped feeding Supabase for 6 hours
# overnight. Pair with the hardware watchdog (see pi-watchdog-install.md) -
# this script handles network drops, the hardware watchdog handles full hangs
# where cron/systemd can no longer run at all.

STATE=/run/net-watchdog.fails
LIMIT=15      # minutes of no connectivity before reboot
BOUNCE_AT=10  # minutes at which to try a WiFi interface bounce first
IFACE="${NET_WATCHDOG_IFACE:-wlan0}"

ok=0
ping -c1 -W5 1.1.1.1 >/dev/null 2>&1 && ok=1
[ $ok -eq 0 ] && ping -c1 -W5 8.8.8.8 >/dev/null 2>&1 && ok=1
[ $ok -eq 0 ] && curl -s -m 8 -o /dev/null https://sduzxijjvpbfgvlwcwpp.supabase.co/rest/v1/ && ok=1

if [ $ok -eq 1 ]; then
    echo 0 > "$STATE"
    exit 0
fi

n=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STATE"
logger -t net-watchdog "connectivity check failed ($n/$LIMIT)"

if [ "$n" -eq "$BOUNCE_AT" ]; then
    logger -t net-watchdog "bouncing $IFACE"
    ip link set "$IFACE" down
    sleep 5
    ip link set "$IFACE" up
    command -v wpa_cli >/dev/null 2>&1 && wpa_cli -i "$IFACE" reconnect >/dev/null 2>&1
fi

if [ "$n" -ge "$LIMIT" ]; then
    logger -t net-watchdog "no network for $LIMIT minutes - rebooting"
    echo 0 > "$STATE"
    /sbin/reboot
fi
