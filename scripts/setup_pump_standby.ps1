# One-time setup: make andrew-new the STANDBY pump poller.
#
# Run ONCE on andrew-new from an elevated PowerShell (Run as administrator),
# from this repo's scripts folder:
#     .\setup_pump_standby.ps1
#
# What it does:
#   1. Creates C:\Apps\pump_poller with a Python venv (pymodbus + requests).
#   2. Copies pump_standby_runner.ps1 there.
#   3. Registers the "Pump poller standby" scheduled task: runs at boot as
#      SYSTEM (no login needed), restarts itself if it ever stops.
#   4. Starts it now.
#
# The Raspberry Pi stays the PRIMARY. andrew-new sits idle (no Modbus traffic,
# no SIM data) until the Pi's data is 5+ min stale, then takes over, and hands
# back when the Pi is polling again. You get an email both ways.
#
# Check it:   Get-Content C:\Apps\pump_poller\standby.log -Tail 30 -Wait
# Remove:     Unregister-ScheduledTask -TaskName "Pump poller standby" -Confirm:$false
#             then stop python.exe running from C:\Apps\pump_poller (or reboot).
$ErrorActionPreference = "Stop"
$taskName = "Pump poller standby"
$Dir      = "C:\Apps\pump_poller"

# ── Python ────────────────────────────────────────────────────────────────────
$py = $null
foreach ($c in @("py -3.12", "py -3.11", "py -3", "python")) {
    $exe, $arg = $c.Split(" ", 2)
    try {
        $v = if ($arg) { & $exe $arg --version 2>&1 } else { & $exe --version 2>&1 }
        if ("$v" -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 9) { $py = $c; break }
    } catch {}
}
if (-not $py) { throw "Python 3.9+ not found. Install it from python.org (tick 'Add to PATH'), then re-run." }
Write-Host "Using $py" -ForegroundColor Cyan

New-Item -ItemType Directory -Force $Dir | Out-Null
if (-not (Test-Path "$Dir\venv\Scripts\python.exe")) {
    $exe, $arg = $py.Split(" ", 2)
    if ($arg) { & $exe $arg -m venv "$Dir\venv" } else { & $exe -m venv "$Dir\venv" }
}
& "$Dir\venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$Dir\venv\Scripts\python.exe" -m pip install --quiet "pymodbus==3.14.0" requests
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# ── Stop any previous install (safe to re-run this script to upgrade) ────────
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Write-Host "Stopped the existing '$taskName' task." -ForegroundColor Cyan
}
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='powershell.exe'" |
    Where-Object { $_.CommandLine -match [regex]::Escape($Dir) -and $_.ProcessId -ne $PID } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# ── Runner ────────────────────────────────────────────────────────────────────
$runnerSrc = Join-Path $PSScriptRoot "pump_standby_runner.ps1"
if (-not (Test-Path $runnerSrc)) { throw "not found: $runnerSrc" }
Copy-Item $runnerSrc "$Dir\pump_standby_runner.ps1" -Force
Copy-Item (Join-Path $PSScriptRoot "pi_pump_poller.py") "$Dir\pi_pump_poller.py" -Force

# ── Scheduled task (boot, SYSTEM, auto-restart, no time limit) ───────────────
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Dir\pump_standby_runner.ps1`"" `
    -WorkingDirectory $Dir
$trigger   = New-ScheduledTaskTrigger -AtStartup
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

Write-Host "Installed and started '$taskName' on $([Environment]::MachineName)." -ForegroundColor Green
Write-Host "Watch it:  Get-Content $Dir\standby.log -Tail 30 -Wait" -ForegroundColor Cyan
Write-Host "Expect: 'role=standby' on start, then quiet while the Pi is healthy." -ForegroundColor Cyan
Write-Host "IMPORTANT: make sure this PC never sleeps (Settings > System > Power > Sleep: Never)," -ForegroundColor Yellow
Write-Host "otherwise it cannot take over when the Pi goes down." -ForegroundColor Yellow
