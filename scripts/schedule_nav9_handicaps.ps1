# Register the Thursday 06:30 Navigate 9s kiosk handicap job on andrew-new.
#
# Run ONCE from an elevated PowerShell on andrew-new:
#     .\schedule_nav9_handicaps.ps1
# Remove with:
#     Unregister-ScheduledTask -TaskName "Nav9 kiosk handicaps" -Confirm:$false
#
# Prereqs on andrew-new:
#   - this repo (wwcc-weather-page) cloned here with push access - the job commits
#     and pushes data/nav9_handicaps.json. (git credentials are already set up for
#     the Navigate 9s jobs, same GitHub account.)
#   - the Navigate 9s repo .env present for the service key. Default path is
#     C:\Apps\Navigate9\.env; if it lives elsewhere, edit the -Argument below to add
#     -Nav9EnvPath "D:\path\to\.env".
$ErrorActionPreference = "Stop"
$taskName = "Nav9 kiosk handicaps"
$script = Join-Path $PSScriptRoot "nav9_fetch_handicaps.ps1"
if (-not (Test-Path $script)) { throw "not found: $script" }
$me = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
    -WorkingDirectory (Split-Path $script)

# Thursday 06:30 - after Matt closes the week (Tue/Wed) and the Wed handicap emails.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Thursday -At "06:30"

# Wake a sleeping PC for the run; still run if the 06:30 slot was missed (PC off).
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -User $me -RunLevel Highest -Force | Out-Null

Write-Host "Scheduled '$taskName' for Thursday 06:30 on $([Environment]::MachineName)." -ForegroundColor Green
Write-Host "Test the fetch now (writes nothing):  .\nav9_fetch_handicaps.ps1 -WhatIf" -ForegroundColor Cyan
