# Pump poller STANDBY runner for andrew-new (Windows).
#
# Keeps scripts/pi_pump_poller.py running as the STANDBY poller: it sits idle
# while the Raspberry Pi (primary) is feeding Supabase, takes over automatically
# if the Pi's data goes 5+ min stale, and hands back when the Pi is online again.
#
# - Auto-update: every 5 min fetches the latest poller from GitHub; if it has
#   changed, the poller is restarted on the new version (same as the Pi's
#   pi_auto_update.py).
# - Auto-restart: if the poller exits or crashes it is restarted after 10 s.
# - Log: C:\Apps\pump_poller\standby.log (rolled to standby.old.log at 10 MB).
#
# Installed and started at boot by setup_pump_standby.ps1 - do not run by hand
# unless testing.

$ErrorActionPreference = "Continue"
$Dir     = "C:\Apps\pump_poller"
$Script  = Join-Path $Dir "pi_pump_poller.py"
$Py      = Join-Path $Dir "venv\Scripts\python.exe"
$Log     = Join-Path $Dir "standby.log"
$RawUrl  = "https://raw.githubusercontent.com/Bidgee182/wwcc-weather-page/main/scripts/pi_pump_poller.py"
$CheckS  = 300

$env:POLLER_ROLE      = "standby"
$env:POLLER_NAME      = "andrew-new"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

function Write-Log($msg) {
    $line = "[runner $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Add-Content -Path $Log -Value $line -Encoding utf8
}

function Update-Poller {
    # Returns $true when a new version was installed.
    $tmp = "$Script.download"
    try {
        Invoke-WebRequest -Uri ($RawUrl + "?t=" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) `
            -OutFile $tmp -UseBasicParsing -TimeoutSec 60
    } catch {
        Write-Log "update check failed: $($_.Exception.Message)"
        return $false
    }
    if ((Get-Item $tmp).Length -lt 10000) { Remove-Item $tmp -Force; return $false }
    $new = (Get-FileHash $tmp).Hash
    $old = if (Test-Path $Script) { (Get-FileHash $Script).Hash } else { "" }
    if ($new -ne $old) {
        Move-Item $tmp $Script -Force
        Write-Log "poller updated from GitHub ($($new.Substring(0,12)))"
        return $true
    }
    Remove-Item $tmp -Force
    return $false
}

Write-Log "runner starting"
while ($true) {
    if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 10MB) {
        Move-Item $Log (Join-Path $Dir "standby.old.log") -Force
    }
    [void](Update-Poller)
    if (-not (Test-Path $Script)) { Write-Log "no poller script yet - retrying in 60 s"; Start-Sleep 60; continue }

    $cmd  = "`"$Py`" -u `"$Script`" >> `"$Log`" 2>&1"
    $proc = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $cmd `
                -WorkingDirectory $Dir -WindowStyle Hidden -PassThru
    Write-Log "poller started (pid $($proc.Id))"

    $nextCheck = (Get-Date).AddSeconds($CheckS)
    while (-not $proc.HasExited) {
        Start-Sleep 5
        if ((Get-Date) -ge $nextCheck) {
            $nextCheck = (Get-Date).AddSeconds($CheckS)
            if (Update-Poller) {
                Write-Log "restarting poller on the new version"
                & taskkill.exe /PID $proc.Id /T /F | Out-Null
                break
            }
        }
    }
    Write-Log "poller exited - restarting in 10 s"
    Start-Sleep 10
}
