# Navigate 9s kiosk handicaps - Thursday 06:30 fetch, run on andrew-new.
#
# Pulls this week's PLAYING handicaps from the Navigate 9s app's read-only feed
# (/api/board-handicaps, engine-computed, the same figure as the handicap emails)
# and commits them to data/nav9_handicaps.json so the TV-kiosk Navigate 9s board
# scores gross -> nett correctly. The handicaps change every week, so this keeps
# the board current instead of stuck on week-1 starting handicaps.
#
# The Navigate 9s app itself is never written to; this only reads one endpoint.
# If the fetch fails or looks empty, the existing file is LEFT ALONE (the board
# never blanks). Safe to run more than once: it only commits when the file
# actually changes.
#
#   .\nav9_fetch_handicaps.ps1            # do it
#   .\nav9_fetch_handicaps.ps1 -WhatIf   # show what it would do, write nothing
#
# Paths assume andrew-new's layout; override with the params if they differ.
param(
    [switch]$WhatIf,
    [string]$SiteUrl,
    [string]$Nav9EnvPath = "C:\Apps\Navigate9\.env"
)
$ErrorActionPreference = "Continue"

# repo root = the folder above this script
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$outFile = Join-Path $repo "data\nav9_handicaps.json"
$log = Join-Path $PSScriptRoot "out\nav9_fetch_handicaps.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
function Say([string]$m) { $line = "$(Get-Date -Format s) $m"; $line | Add-Content -Encoding UTF8 $log; Write-Host $line }

Say "===== Navigate 9s kiosk handicaps on $([Environment]::MachineName) (WhatIf: $WhatIf)"

# ---- the Navigate 9s app's service key + site, read from its .env ----
if (-not (Test-Path $Nav9EnvPath)) { Say "no Navigate 9s .env at $Nav9EnvPath - stop"; exit 2 }
$cfg = @{}
foreach ($l in Get-Content $Nav9EnvPath) { if ($l -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $cfg[$matches[1]] = $matches[2].Trim() } }
$key = $cfg["SUPABASE_SERVICE_ROLE_KEY"]
if (-not $key) { Say "SUPABASE_SERVICE_ROLE_KEY not in $Nav9EnvPath - stop"; exit 2 }
if (-not $SiteUrl) { $SiteUrl = if ($cfg["NAV9_SITE_URL"]) { $cfg["NAV9_SITE_URL"] } else { "https://navigate9s-web.vercel.app" } }
$uri = "$SiteUrl/api/board-handicaps"

# ---- stay current so the commit applies cleanly ----
try {
    $pull = (& git -C $repo pull --ff-only --quiet 2>&1 | Out-String).Trim()
    if ($pull) { Say "git pull: $pull" }
} catch { Say "git pull skipped: $($_.Exception.Message)" }

# ---- fetch the feed (raw body, so we store the app's exact JSON) ----
$body = $null
try {
    $resp = Invoke-WebRequest -Method Get -Headers @{ "x-service-key" = $key } -Uri $uri -TimeoutSec 120 -UseBasicParsing
    $body = $resp.Content
} catch {
    Say "fetch failed: $($_.Exception.Message) - leaving $outFile unchanged"
    exit 1
}

# ---- validate before trusting it ----
try { $data = $body | ConvertFrom-Json } catch { Say "response was not JSON - leaving file unchanged"; exit 1 }
$week = [int]($data.week)
$count = [int]($data.count)
if ($week -lt 1 -or $count -lt 1 -or -not $data.players) {
    Say "feed looks empty (week=$week count=$count) - leaving $outFile unchanged"
    exit 1
}
Say "feed OK: season $($data.season), week $week, $count players ($($data.basis))"

if ($WhatIf) { Say "WhatIf: would write $outFile and commit if changed"; exit 0 }

# ---- write only if the handicaps/week/course actually changed. Compare with the
#      generatedAt timestamp removed, since it moves on every fetch and would
#      otherwise force a commit every run. ----
function Normalize([string]$json) {
    if (-not $json) { return "" }
    try {
        $o = $json | ConvertFrom-Json
        if ($o.PSObject.Properties.Name -contains 'generatedAt') { $o.PSObject.Properties.Remove('generatedAt') }
        return ($o | ConvertTo-Json -Depth 30 -Compress)
    } catch { return $json }
}
$old = if (Test-Path $outFile) { Get-Content -Raw -Encoding UTF8 $outFile } else { "" }
if ((Normalize $old) -eq (Normalize $body)) { Say "handicaps unchanged since last run - nothing to commit"; exit 0 }
Set-Content -Path $outFile -Value $body -Encoding UTF8 -NoNewline

# ---- commit + push, with a short rebase-retry for the leaderboard poller ----
$msg = "Nav9 kiosk: week $week playing handicaps ($count players)"
& git -C $repo add -- data/nav9_handicaps.json 2>&1 | Out-Null
$committed = $false
& git -C $repo commit -q -m $msg 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) { $committed = $true } else { Say "nothing staged to commit"; exit 0 }
for ($i = 1; $i -le 5; $i++) {
    & git -C $repo pull --rebase --quiet 2>&1 | Out-Null
    & git -C $repo push 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Say "pushed: $msg"; break }
    Say "push attempt $i failed, retrying"; Start-Sleep -Seconds 3
}
Say "===== finished"
