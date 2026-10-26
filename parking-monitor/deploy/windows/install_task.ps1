# Registers a Task Scheduler task that starts the parking monitor when you log on
# and restarts it if it crashes. Run from the project folder in a normal
# (non-admin) PowerShell:
#
#     powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1
#
# Why "at log on" and not "at startup": iCloud for Windows only syncs while you
# are logged in, so the JSON would not reach your phone before that anyway.
# Set Windows to sign you in automatically if this PC reboots unattended.
#
# Remove with:  Unregister-ScheduledTask -TaskName ParkingMonitor -Confirm:$false

$ErrorActionPreference = "Stop"
$project = (Resolve-Path "$PSScriptRoot\..\..").Path
$pythonw = Join-Path $project ".venv\Scripts\pythonw.exe"   # pythonw = no console window
if (-not (Test-Path $pythonw)) { throw "venv not found: create it first (see README)" }

$action   = New-ScheduledTaskAction -Execute $pythonw `
              -Argument "`"$project\parking_monitor.py`" run" -WorkingDirectory $project
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -ExecutionTimeLimit ([TimeSpan]::Zero) `
              -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
              -StartWhenAvailable

Register-ScheduledTask -TaskName "ParkingMonitor" -Action $action -Trigger $trigger `
    -Settings $settings -Description "ArUco parking spot monitor" -Force | Out-Null
Start-ScheduledTask -TaskName "ParkingMonitor"
Write-Host "Installed and started. Log: $project\logs\parking.log"
