# Pump Pi watchdogs - one-time install (7 Sep 2026)

Two layers, run these on the Pi over SSH. Layer 1 reboots on a 15-minute
network outage; layer 2 (hardware watchdog) reboots on a full OS hang, which
is what a network script cannot survive.

## 1. Network watchdog (WiFi drop -> bounce at 10 min, reboot at 15 min)

    cd /home/andrew/pump_poller
    wget -O pi_net_watchdog.sh https://raw.githubusercontent.com/Bidgee182/wwcc-weather-page/main/scripts/pi_net_watchdog.sh
    chmod +x pi_net_watchdog.sh
    sudo wget -O /etc/systemd/system/net-watchdog.service https://raw.githubusercontent.com/Bidgee182/wwcc-weather-page/main/scripts/net-watchdog.service
    sudo wget -O /etc/systemd/system/net-watchdog.timer   https://raw.githubusercontent.com/Bidgee182/wwcc-weather-page/main/scripts/net-watchdog.timer
    sudo systemctl daemon-reload
    sudo systemctl enable --now net-watchdog.timer

Check it is ticking:    systemctl list-timers | grep net-watchdog
Watch its decisions:    journalctl -t net-watchdog -n 20
If the Pi is on Ethernet instead of WiFi, no change needed - the bounce step
just does nothing useful and the 15-minute reboot still applies.

## 2. Hardware watchdog (reboots a completely hung Pi)

    echo 'dtparam=watchdog=on' | sudo tee -a /boot/firmware/config.txt
    sudo sed -i 's/^#\?RuntimeWatchdogSec=.*/RuntimeWatchdogSec=15/' /etc/systemd/system.conf
    sudo reboot

(On older Pi OS the config file is /boot/config.txt.) After the reboot,
verify with:    cat /proc/sys/kernel/watchdog_thresh 2>/dev/null; dmesg | grep -i watchdog | head -3
From then on, if the OS freezes for 15 seconds the chip power-cycles it.

## What each layer covers

| Failure | Caught by |
|---|---|
| WiFi/router drop, Pi otherwise fine | Network watchdog (bounce, then reboot) |
| Poller crash | systemd Restart=always (already in place) |
| Full OS hang (like 7 Sep 2026, 6 h silent) | Hardware watchdog |
| Pi power loss | Nothing on the Pi can help - the server-side feed-freshness email is the net |
