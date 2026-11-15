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

param(
    [string]$Dir = "C:\Apps\pump_poller",
    [string]$Py  = ""
)
$ErrorActionPreference = "Continue"
$Script  = Join-Path $Dir "pi_pump_poller.py"
if (-not $Py) { $Py = Join-Path $Dir "venv\Scripts\python.exe" }
$Out     = Join-Path $Dir "poller.out.log"
$Err     = Join-Path $Dir "poller.err.log"
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

function Sync-Output([int]$shown) {
    # Copy any new poller stdout lines into the main log; returns lines copied so far.
    if (-not (Test-Path $Out)) { return $shown }
    $lines = @(Get-Content $Out -ErrorAction SilentlyContinue)
    if ($lines.Count -gt $shown) {
        Add-Content -Path $Log -Value ($lines[$shown..($lines.Count - 1)]) -Encoding utf8
    }
    return [Math]::Max($shown, $lines.Count)
}

Write-Log "runner starting"
while ($true) {
    if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 10MB) {
        Move-Item $Log (Join-Path $Dir "standby.old.log") -Force
    }
    [void](Update-Poller)
    if (-not (Test-Path $Script)) { Write-Log "no poller script yet - retrying in 60 s"; Start-Sleep 60; continue }

    # Launch python directly (no cmd.exe - its quote-stripping broke the 16 Nov
    # first install). Output goes to per-run files that are appended to the
    # main log when the run ends, so crash tracebacks are never lost.
    $proc = Start-Process -FilePath $Py -ArgumentList @("-u", "`"$Script`"") `
                -WorkingDirectory $Dir -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput $Out -RedirectStandardError $Err
    Write-Log "poller started (pid $($proc.Id))"

    $nextCheck = (Get-Date).AddSeconds($CheckS)
    $shown = 0
    while (-not $proc.HasExited) {
        Start-Sleep 5
        $shown = Sync-Output $shown
        if ((Get-Date) -ge $nextCheck) {
            $nextCheck = (Get-Date).AddSeconds($CheckS)
            if (Update-Poller) {
                Write-Log "restarting poller on the new version"
                & taskkill.exe /PID $proc.Id /T /F | Out-Null
                break
            }
        }
    }
    Start-Sleep 1
    [void](Sync-Output $shown)
    if (Test-Path $Err) { $e = Get-Content $Err -Raw -ErrorAction SilentlyContinue; if ($e) { Add-Content -Path $Log -Value $e.TrimEnd() -Encoding utf8 } }
    Write-Log "poller exited (code $($proc.ExitCode)) - restarting in 10 s"
    Start-Sleep 10
}
