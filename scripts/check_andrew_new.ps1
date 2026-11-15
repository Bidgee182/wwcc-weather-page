# Read-only readiness check for the pump standby poller on andrew-new.
# Changes nothing. Run from the repo's scripts folder:
#     .\check_andrew_new.ps1
"=== MACHINE ==="
$env:COMPUTERNAME
(Get-CimInstance Win32_OperatingSystem).Caption

"=== PYTHON ==="
foreach ($c in @("py -0p", "python --version", "py -3 --version")) {
    try   { "$c -> " + ((Invoke-Expression "$c 2>&1") | Out-String).Trim() }
    catch { "$c -> not found" }
}

"=== GIT ==="
try { git --version } catch { "git not found" }

"=== REPO ==="
$repo = Split-Path $PSScriptRoot
$repo
git -C $repo log -1 --format="  last commit %ci %s"
git -C $repo status -sb | Select-Object -First 1

"=== C:\Apps ==="
Get-ChildItem C:\Apps -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name

"=== TASKS ==="
Get-ScheduledTask | Where-Object { $_.TaskName -match "pump|nav9|poll|wwcc" } |
    ForEach-Object { "$($_.TaskName)  [$($_.State)]  runs as $($_.Principal.UserId)" }

"=== SLEEP WHEN PLUGGED IN ==="
powercfg /q SCHEME_CURRENT SUB_SLEEP STANDBYIDLE | Select-String "Current AC"
"(0x00000000 = never sleeps)"

"=== PUMPS REACHABLE? ==="
$t = Test-NetConnection bidgee-pumps.duckdns.org -Port 502 -WarningAction SilentlyContinue
"address $($t.RemoteAddress)  port 502 open: $($t.TcpTestSucceeded)"
